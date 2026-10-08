"""Synthetic cloud OAuth gateway used by production web route acceptance tests.

The gateway and its Tesla/Enphase provider transports run locally. Provider
observations record resource IDs and counters, never credentials or tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import uvicorn
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.cloud_oauth import CloudEnergyOAuthConfiguration
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host

TESLA_ACCESS_TOKEN = "cloud-web-fixture-tesla-access-token"
TESLA_REFRESH_TOKEN = "cloud-web-fixture-tesla-refresh-token"
ENPHASE_ACCESS_TOKEN = "cloud-web-fixture-enphase-access-token"
ENPHASE_REFRESH_TOKEN = "cloud-web-fixture-enphase-refresh-token"
TESLA_RESOURCE_ID = "1234567890"
ENPHASE_RESOURCE_ID = "698910067"


def _write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    path.chmod(0o600)


def _save_observations(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _token_response(provider: str, request: httpx.Request) -> httpx.Response:
    form = parse_qs(request.content.decode("utf-8"))
    if form.get("grant_type") != ["authorization_code"]:
        return httpx.Response(400, json={"error": "unsupported_grant_type"}, request=request)

    if provider == "tesla":
        expected_secret = os.environ["CLOUD_OAUTH_TEST_TESLA_CLIENT_SECRET"]
        if (
            form.get("client_id") != ["cloud-web-tesla-client"]
            or form.get("client_secret") != [expected_secret]
            or form.get("audience") != ["https://fleet-api.prd.eu.vn.cloud.tesla.com"]
        ):
            return httpx.Response(401, json={"error": "invalid_client"}, request=request)
        return httpx.Response(
            200,
            json={
                "access_token": TESLA_ACCESS_TOKEN,
                "refresh_token": TESLA_REFRESH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            request=request,
        )

    client_id = "cloud-web-enphase-client"
    client_secret = os.environ["CLOUD_OAUTH_TEST_ENPHASE_CLIENT_SECRET"]
    expected_auth = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode(
        "ascii"
    )
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected_auth) or form.get(
        "client_id"
    ) != [client_id]:
        return httpx.Response(401, json={"error": "invalid_client"}, request=request)
    return httpx.Response(
        200,
        json={
            "access_token": ENPHASE_ACCESS_TOKEN,
            "refresh_token": ENPHASE_REFRESH_TOKEN,
            "token_type": "Bearer",
            "expires_in": 3600,
        },
        request=request,
    )


def _tesla_site_info(resource_id: str) -> dict[str, object]:
    return {
        "response": {
            "id": resource_id,
            "site_name": "Synthetic Tesla Home",
            "installation_time_zone": "Europe/London",
            "nameplate_power": 10000,
            "nameplate_energy": 27000,
            "components": {"solar": True, "battery": True, "grid": True},
        }
    }


def _enphase_summary(system_id: int) -> dict[str, object]:
    return {
        "system_id": system_id,
        "current_power": 4200,
        "energy_lifetime": 15872000,
        "energy_today": 19200,
        "last_interval_end_at": 1791460500,
        "last_report_at": 1791460530,
        "modules": 30,
        "operational_at": 1557400231,
        "size_w": 12500,
        "source": "meter",
        "status": "normal",
        "summary_date": "2026-10-08",
    }


def create_app(state_dir: Path, web_origin: str):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    redirect_uri = web_origin.rstrip("/") + "/api/workspace/oauth/callback"
    configurations = (
        CloudEnergyOAuthConfiguration(
            id="tesla-home",
            name="Tesla Energy application",
            provider="tesla",
            client_id="cloud-web-tesla-client",
            client_secret_env="CLOUD_OAUTH_TEST_TESLA_CLIENT_SECRET",
            redirect_uri=redirect_uri,
            region="eu",
        ),
        CloudEnergyOAuthConfiguration(
            id="enphase-home",
            name="Enphase Energy application",
            provider="enphase",
            client_id="cloud-web-enphase-client",
            client_secret_env="CLOUD_OAUTH_TEST_ENPHASE_CLIENT_SECRET",
            redirect_uri=redirect_uri,
            api_key_env="CLOUD_OAUTH_TEST_ENPHASE_API_KEY",
        ),
    )

    control = ControlStore(state_dir / "control")
    owner = control.bootstrap_workspace("Cloud web owner", "Cloud web workspace")
    foreign = control.bootstrap_workspace("Foreign cloud owner", "Foreign cloud workspace")
    foreign_site = control.create_site(
        foreign.user.id,
        foreign.workspace.id,
        name="Foreign site",
        timezone="UTC",
    )
    _write_private(state_dir / "manager.token", owner.key.token)
    _write_private(state_dir / "foreign-manager.token", foreign.key.token)
    _write_private(state_dir / "foreign-site.id", foreign_site.id)

    observations: dict[str, object] = {
        "token_exchanges": {"tesla": 0, "enphase": 0},
        "provider_reads": {"tesla": 0, "enphase": 0},
        "resources": [],
    }
    observations_path = state_dir / "observations.json"
    _save_observations(observations_path, observations)

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fleet-auth.prd.vn.cloud.tesla.com":
            if request.url.path != "/oauth2/v3/token":
                return httpx.Response(404, request=request)
            exchanges = observations["token_exchanges"]
            assert isinstance(exchanges, dict)
            exchanges["tesla"] = int(exchanges["tesla"]) + 1
            _save_observations(observations_path, observations)
            return _token_response("tesla", request)

        if request.url.host == "api.enphaseenergy.com" and request.url.path == "/oauth/token":
            exchanges = observations["token_exchanges"]
            assert isinstance(exchanges, dict)
            exchanges["enphase"] = int(exchanges["enphase"]) + 1
            _save_observations(observations_path, observations)
            return _token_response("enphase", request)

        if request.url.host == "fleet-api.prd.eu.vn.cloud.tesla.com":
            segments = request.url.path.split("/")
            if (
                request.method != "GET"
                or len(segments) != 6
                or segments[1:4] != ["api", "1", "energy_sites"]
                or segments[5] != "site_info"
                or request.headers.get("authorization") != f"Bearer {TESLA_ACCESS_TOKEN}"
            ):
                return httpx.Response(401, json={"error": "invalid_request"}, request=request)
            resource_id = segments[4]
            reads = observations["provider_reads"]
            assert isinstance(reads, dict)
            reads["tesla"] = int(reads["tesla"]) + 1
            resources = observations["resources"]
            assert isinstance(resources, list)
            resources.append({"provider": "tesla", "resource_id": resource_id})
            _save_observations(observations_path, observations)
            return httpx.Response(200, json=_tesla_site_info(resource_id), request=request)

        if request.url.host == "api.enphaseenergy.com" and request.url.path.startswith(
            "/api/v4/systems/"
        ):
            segments = request.url.path.split("/")
            if (
                request.method != "GET"
                or len(segments) != 6
                or segments[1:4] != ["api", "v4", "systems"]
                or segments[5:] != ["summary"]
                or request.headers.get("authorization") != f"Bearer {ENPHASE_ACCESS_TOKEN}"
                or request.url.params.get("key") != os.environ["CLOUD_OAUTH_TEST_ENPHASE_API_KEY"]
            ):
                return httpx.Response(401, json={"error": "invalid_request"}, request=request)
            resource_id = segments[4]
            try:
                system_id = int(resource_id)
            except ValueError:
                return httpx.Response(404, json={"error": "not_found"}, request=request)
            reads = observations["provider_reads"]
            assert isinstance(reads, dict)
            reads["enphase"] = int(reads["enphase"]) + 1
            resources = observations["resources"]
            assert isinstance(resources, list)
            resources.append({"provider": "enphase", "resource_id": resource_id})
            _save_observations(observations_path, observations)
            return httpx.Response(200, json=_enphase_summary(system_id), request=request)

        return httpx.Response(
            404, json={"error": "synthetic_provider_route_not_found"}, request=request
        )

    agent = build_agent(state_dir / "agent")
    asyncio.run(agent.http.aclose())
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    agent.auth_store = AuthStore(state_dir / "vault", Fernet.generate_key(), http=upstream)
    return create_host(
        agent,
        {},
        close_agent_on_shutdown=True,
        control_store=control,
        managed_workspaces=True,
        managed_oauth_configurations=configurations,
        max_requests_per_minute=300,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--web-origin", required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.state_dir, args.web_origin),
        host="127.0.0.1",
        port=args.port,
        log_level="critical",
        access_log=False,
    )


if __name__ == "__main__":
    main()
