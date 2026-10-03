"""Production managed-workspace REST host for the TypeScript SDK acceptance test.

The fixture uses the real host, control store, encrypted auth store, and agent
builder. Its Octopus transport is synthetic and never opens a provider socket.
The one-time management key is written to a private temporary file for the
Node test and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.managed_oauth import HomeAssistantOAuthConfiguration

MAX_REVOCATION_FAILURES = 3


def write_private_token(path: Path, token: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(token)


def create_app(
    state_dir: Path,
    token_file: Path,
    other_token_file: Path | None = None,
    *,
    web_origin: str | None = None,
    revocation_failures: int = 0,
):
    if (
        type(revocation_failures) is not int
        or not 0 <= revocation_failures <= MAX_REVOCATION_FAILURES
    ):
        raise ValueError(f"revocation_failures must be between 0 and {MAX_REVOCATION_FAILURES}")
    state_dir.mkdir(parents=True, exist_ok=True)
    control = ControlStore(state_dir / "control")
    bootstrap = control.bootstrap_workspace("SDK fixture owner", "SDK fixture workspace")
    control.create_user("sdk-member", "SDK member")
    write_private_token(token_file, bootstrap.key.token)

    if other_token_file is not None:
        other_workspace = control.create_workspace(
            bootstrap.user.id, "SDK fixture other workspace", mode="managed"
        )
        other_key = control.create_key(
            bootstrap.user.id,
            other_workspace.id,
            "Other workspace manager",
            access=ManageKeyAccess(),
        )
        write_private_token(other_token_file, other_key.token)

    client_id = web_origin.rstrip("/") + "/" if web_origin else "http://127.0.0.1:18123/"
    redirect_uri = (
        web_origin.rstrip("/") + "/api/workspace/oauth/callback"
        if web_origin
        else "http://127.0.0.1:18123/oauth/callback"
    )
    agent = build_agent(state_dir / "agent")
    authorization_code = "fixture-authorization-code-private"
    access_token = "fixture-access-token-private"
    refreshed_access_token = "fixture-refreshed-access-token-private"
    refresh_token = "fixture-refresh-token-private"
    provider_state = {"revoked": False, "revocation_failures_remaining": revocation_failures}
    observations = {"auth_code": 0, "refresh": 0, "state_reads": 0, "revocations": 0}
    observations_file = state_dir / "observations.json"

    def save_observations() -> None:
        observations_file.write_text(json.dumps(observations), encoding="utf-8")

    save_observations()

    async def synthetic_octopus(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            if form.get("grant_type") == ["authorization_code"]:
                observations["auth_code"] += 1
                save_observations()
                if form != {
                    "grant_type": ["authorization_code"],
                    "code": [authorization_code],
                    "client_id": [client_id],
                }:
                    return httpx.Response(400, json={"error": "invalid_grant"}, request=request)
                return httpx.Response(
                    200,
                    json={
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 30,
                    },
                    request=request,
                )
            if form.get("grant_type") == ["refresh_token"]:
                observations["refresh"] += 1
                save_observations()
                if provider_state["revoked"] or form.get("refresh_token") != [refresh_token]:
                    return httpx.Response(400, json={"error": "invalid_grant"}, request=request)
                return httpx.Response(
                    200,
                    json={
                        "access_token": refreshed_access_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                    request=request,
                )
            return httpx.Response(400, json={"error": "unsupported_grant_type"}, request=request)

        if request.url.path == "/auth/revoke" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            token = form.get("token", [""])[0]
            if token not in {access_token, refreshed_access_token, refresh_token}:
                return httpx.Response(400, json={"error": "invalid_token"}, request=request)
            observations["revocations"] += 1
            save_observations()
            if provider_state["revocation_failures_remaining"] > 0:
                provider_state["revocation_failures_remaining"] -= 1
                return httpx.Response(
                    503, json={"error": "temporarily_unavailable"}, request=request
                )
            provider_state["revoked"] = True
            return httpx.Response(200, request=request)

        if request.url.path.startswith("/api/states/") and request.method == "GET":
            observations["state_reads"] += 1
            save_observations()
            bearer = request.headers.get("authorization")
            if provider_state["revoked"] or bearer not in {
                f"Bearer {access_token}",
                f"Bearer {refreshed_access_token}",
            }:
                return httpx.Response(401, json={"message": "Unauthorized"}, request=request)
            if request.url.path != "/api/states/sensor.power":
                return httpx.Response(404, json={"message": "Entity not found."}, request=request)
            return httpx.Response(
                200,
                json={
                    "entity_id": "sensor.power",
                    "state": "1.75",
                    "attributes": {
                        "unit_of_measurement": "kW",
                        "state_class": "measurement",
                    },
                    "last_updated": datetime.now(UTC).isoformat(),
                },
                request=request,
            )

        if request.url.host != "api.octopus.energy" or not request.url.path.endswith(
            "/consumption/"
        ):
            return httpx.Response(404, json={"detail": "Synthetic provider route not found."})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "consumption": 1.25,
                        "interval_start": "2026-10-02T00:00:00Z",
                        "interval_end": "2026-10-02T00:30:00Z",
                    }
                ]
            },
            request=request,
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(synthetic_octopus))
    asyncio.run(agent.http.aclose())
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(state_dir / "vault", Fernet.generate_key(), http=upstream)
    agent.auth_store = vault
    oauth_configuration = HomeAssistantOAuthConfiguration(
        id="home-assistant",
        name="Local Home Assistant fixture",
        base_url="http://127.0.0.1:18123",
        client_id=client_id,
        redirect_uri=redirect_uri,
    )
    host = create_host(
        agent,
        {},
        close_agent_on_shutdown=True,
        control_store=control,
        managed_workspaces=True,
        managed_oauth_configurations=(oauth_configuration,),
        max_requests_per_minute=200,
    )
    return host, control, agent, upstream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--other-token-file", type=Path)
    parser.add_argument("--web-origin")
    parser.add_argument(
        "--revocation-failures",
        type=int,
        choices=range(MAX_REVOCATION_FAILURES + 1),
        default=0,
        help="Fail this many initial synthetic token revocations with HTTP 503.",
    )
    args = parser.parse_args()

    import uvicorn

    host, control, agent, upstream = create_app(
        args.state_dir,
        args.token_file,
        args.other_token_file,
        web_origin=args.web_origin,
        revocation_failures=args.revocation_failures,
    )
    try:
        uvicorn.run(
            host,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        asyncio.run(agent.close())
        asyncio.run(upstream.aclose())
        control.close()


if __name__ == "__main__":
    main()
