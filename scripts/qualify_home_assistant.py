"""Qualify a real Home Assistant HTTP provider in an isolated Docker run.

The run deliberately creates a project-controlled development owner and a
synthetic state.  It proves the authentication and provider boundary, local
credential storage, reviewed scope, freshness, and provenance contracts.  It
does not claim that a physical meter or a real household is connected.

The script is intended for a Linux CI runner with Docker and enough memory for
Home Assistant.  It never prints container logs, HTTP bodies, passwords, or
tokens.  Cleanup targets only the unique container and volume created by this
invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from energy_agent_tools.app import build_agent
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.onboarding import LocalProfile

HOME_ASSISTANT_IMAGE = (
    "ghcr.io/home-assistant/home-assistant@"
    "sha256:3e6710a7ab2a61311d9d899b719f6c3657791c63e8f4942cec4ebc42401d6b76"
)
HOME_ASSISTANT_VERSION = "2026.9.4"
ENTITY_ID = "sensor.eat_qualification_power"
USER_ID = "qualification-user"
SITE_ID = "qualification-site"
ASSET_ID = "qualification-power-sensor"
ACCOUNT_ID = "ha-qualification"
CLIENT_ID = "http://localhost/"
STARTUP_TIMEOUT_SECONDS = 180
REQUEST_TIMEOUT_SECONDS = 15
MAX_DOCKER_COMMAND_SECONDS = 300
MAX_HTTP_RESPONSE_BYTES = 64 * 1024

MINIMAL_CONFIGURATION = """\
homeassistant:
  name: Energy Agent Tools qualification
  unit_system: metric
  time_zone: UTC
  country: GB
api:
onboarding:
http:
  server_port: 8123
"""


class QualificationFailure(RuntimeError):
    """A safe, user-facing qualification failure with no provider payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_name(prefix: str) -> str:
    """Return a unique Docker name and keep its character set constrained."""

    name = f"{prefix}-{uuid4().hex}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,127}", name):
        raise QualificationFailure(
            "invalid_resource_name", "Generated Docker resource name is invalid."
        )
    return name


def docker(*args: str, timeout: float = MAX_DOCKER_COMMAND_SECONDS) -> str:
    """Run one bounded Docker command without exposing stderr or stdout details."""

    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualificationFailure(
            "docker_unavailable", "Docker did not complete the requested operation."
        ) from exc
    if completed.returncode:
        operation = args[0] if args else "command"
        raise QualificationFailure("docker_failed", f"Docker {operation} failed.")
    return completed.stdout.strip()


def _docker_remove(command: Sequence[str]) -> bool:
    """Remove exactly one owned resource; missing resources count as clean."""

    try:
        completed = subprocess.run(
            ["docker", *command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode == 0:
        return True
    # A failed run can leave no container or volume at all.  We do not need a
    # broad prune operation to handle that case, and we never inspect or print
    # daemon error text because it is outside the qualification evidence.
    stderr = completed.stderr.lower()
    return "no such" in stderr or "not found" in stderr


def cleanup_owned_resources(container_name: str, volume_name: str, init_name: str) -> bool:
    """Delete only this run's helper container, HA container, and volume."""

    if not all(
        re.fullmatch(r"eat-ha-qualification-[a-f0-9]{32}(?:-init)?", value)
        for value in (container_name, volume_name, init_name)
    ):
        raise QualificationFailure(
            "invalid_resource_name", "Refusing to clean an unowned Docker resource."
        )
    init_clean = _docker_remove(("rm", "--force", init_name))
    container_clean = _docker_remove(("rm", "--force", container_name))
    volume_clean = _docker_remove(("volume", "rm", "--force", volume_name))
    return init_clean and container_clean and volume_clean


def _configuration_command(image: str, volume_name: str, init_name: str) -> list[str]:
    """Build the helper command that writes only the minimal HA config."""

    encoded = base64.b64encode(MINIMAL_CONFIGURATION.encode("utf-8")).decode("ascii")
    return [
        "run",
        "--rm",
        "--name",
        init_name,
        "--mount",
        f"type=volume,source={volume_name},target=/config",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        f"printf '%s' '{encoded}' | base64 -d > /config/configuration.yaml",
    ]


def _published_port(container_name: str) -> int:
    raw = docker("port", container_name, "8123/tcp", timeout=30)
    match = re.search(r":([0-9]{1,5})\s*$", raw)
    if match is None:
        raise QualificationFailure(
            "docker_port_missing", "Home Assistant did not publish a host port."
        )
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        raise QualificationFailure(
            "docker_port_invalid", "Home Assistant published an invalid host port."
        )
    return port


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
    **kwargs: Any,
) -> Any:
    """Make a bounded request and turn all HTTP details into safe failures."""

    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise QualificationFailure(
            "home_assistant_unavailable", "Home Assistant did not answer the request."
        ) from exc
    if response.status_code != expected_status:
        raise QualificationFailure(
            "home_assistant_protocol",
            f"Home Assistant returned HTTP {response.status_code} for {method} {path}.",
        )
    if len(response.content) > MAX_HTTP_RESPONSE_BYTES:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned too much data."
        )
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned invalid JSON."
        ) from exc


def _require_string(payload: Any, key: str) -> str:
    if not isinstance(payload, Mapping):
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid response."
        )
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid response."
        )
    return value


async def wait_until_ready(client: httpx.AsyncClient, timeout_seconds: int) -> None:
    """Wait for unauthenticated onboarding; the API status endpoint requires auth."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = await client.get("/api/onboarding")
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, list) and any(
                    isinstance(step, Mapping)
                    and step.get("step") == "user"
                    and step.get("done") is False
                    for step in payload
                ):
                    return
        except (httpx.HTTPError, TypeError, ValueError):
            pass
        await asyncio.sleep(1)
    raise QualificationFailure(
        "home_assistant_startup_timeout", "Home Assistant did not become ready in time."
    )


async def onboard_and_seed(client: httpx.AsyncClient) -> str:
    """Create the project-owned dev user, exchange its code, and seed one state."""

    steps = await _request_json(client, "GET", "/api/onboarding")
    if not isinstance(steps, list):
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant onboarding state is invalid."
        )

    username = f"eat_qualification_{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(32)
    created = await _request_json(
        client,
        "POST",
        "/api/onboarding/users",
        json={
            "name": "Energy Agent Tools qualification",
            "username": username,
            "password": password,
            "client_id": CLIENT_ID,
            "language": "en",
        },
    )
    auth_code = _require_string(created, "auth_code")
    token_payload = await _request_json(
        client,
        "POST",
        "/auth/token",
        data={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID},
    )
    access_token = _require_string(token_payload, "access_token")
    _require_string(token_payload, "refresh_token")
    headers = {"Authorization": f"Bearer {access_token}"}
    configuration = await _request_json(client, "GET", "/api/config", headers=headers)
    if (
        not isinstance(configuration, Mapping)
        or configuration.get("version") != HOME_ASSISTANT_VERSION
    ):
        raise QualificationFailure(
            "version_mismatch",
            "Home Assistant version differs from the pinned qualification version.",
        )
    seeded = await _request_json(
        client,
        "POST",
        f"/api/states/{ENTITY_ID}",
        expected_status=201,
        headers=headers,
        json={
            "state": "1.75",
            "attributes": {
                "unit_of_measurement": "kW",
                "friendly_name": "Synthetic qualification power",
            },
        },
    )
    if not isinstance(seeded, Mapping) or seeded.get("entity_id") != ENTITY_ID:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant did not accept the synthetic state."
        )
    attributes = seeded.get("attributes")
    if not isinstance(attributes, Mapping) or attributes.get("unit_of_measurement") != "kW":
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned invalid state metadata."
        )
    if "state_class" in attributes:
        raise QualificationFailure(
            "home_assistant_protocol", "Synthetic state unexpectedly has a state class."
        )
    return access_token


def _reviewed_binding() -> dict[str, Any]:
    """Return the explicit synthetic binding used only by this qualification."""

    return {
        "capability": "get_current_power",
        "tool": "home_assistant.get_state",
        "asset_id": ASSET_ID,
        "kind": "estimated",
        "unit": "kW",
        "quantity_shape": "instantaneous",
        "fixed_arguments": {"entity_id": ENTITY_ID},
        "reviewed": True,
        "quality": "qualification-synthetic-estimated",
        "version": "1.0.0",
    }


async def qualify_profile(
    client: httpx.AsyncClient,
    base_url: str,
    profile_root: Path,
    access_token: str,
) -> dict[str, Any]:
    """Exercise encrypted onboarding and a scoped, fresh capability read."""

    profile = LocalProfile(profile_root, http=client)
    profile.create_site("Home Assistant qualification", "UTC", user_id=USER_ID, site_id=SITE_ID)
    profile.create_asset(
        SITE_ID,
        "telemetry-sensor",
        "Synthetic power sensor",
        asset_id=ASSET_ID,
        account_ids=[ACCOUNT_ID],
        metadata={"source": "home-assistant-qualification"},
    )
    outcome = await profile.connect(
        "home_assistant",
        credential=access_token,
        user_id=USER_ID,
        site_id=SITE_ID,
        connection_id=ACCOUNT_ID,
        metadata={
            "base_url": base_url,
            "entity_id": ENTITY_ID,
            "telemetry_role": "current_power",
            "measurement_kind": "estimated",
            "unit": "kW",
            "quantity_shape": "instantaneous",
            # The entity intentionally has no state_class, so it is not
            # advertised as a physical or metered reading.
            "capability_bindings": [_reviewed_binding()],
        },
    )
    if not outcome.get("ok") or outcome.get("health", {}).get("status") != "healthy":
        raise QualificationFailure(
            "onboarding_failed", "Local Home Assistant connection verification failed."
        )
    connection = outcome.get("account")
    if not isinstance(connection, Mapping) or connection.get("id") != ACCOUNT_ID:
        raise QualificationFailure(
            "onboarding_failed", "Local Home Assistant account scope is invalid."
        )

    profile_text = (profile_root / "profile.json").read_text(encoding="utf-8")
    vault_bytes = (profile_root / "vault" / "auth.sqlite3").read_bytes()
    if access_token in profile_text or access_token.encode("utf-8") in vault_bytes:
        raise QualificationFailure(
            "secret_storage_failed", "Credential storage was not encrypted safely."
        )
    config = profile.config()
    configured = next(
        (item for item in config.get("accounts", []) if item.get("id") == ACCOUNT_ID), None
    )
    if not isinstance(configured, Mapping):
        raise QualificationFailure(
            "onboarding_failed", "Encrypted connection metadata was not persisted."
        )
    bindings = configured.get("settings", {}).get("capability_bindings", [])
    if not isinstance(bindings, list) or _reviewed_binding() not in bindings:
        raise QualificationFailure(
            "onboarding_failed", "Reviewed Home Assistant binding was not persisted."
        )
    profile.close()

    agent = build_agent(profile_root)
    await agent.http.aclose()
    agent.http = client
    agent._owns_http = False
    session = agent.session(USER_ID, SITE_ID)
    request = CapabilityRequest(
        capability="get_current_power",
        asset_id=ASSET_ID,
        max_age_seconds=300,
    )
    try:
        resolution = agent.resolver.resolve(session, request)
        if resolution.get("status") != "resolved":
            raise QualificationFailure(
                "capability_unresolved", "Reviewed Home Assistant capability did not resolve."
            )
        selected = resolution.get("selected")
        if not isinstance(selected, Mapping) or selected.get("account_id") != ACCOUNT_ID:
            raise QualificationFailure(
                "capability_scope_failed",
                "Capability resolved outside the configured account scope.",
            )
        result = await agent.resolver.execute(session, request)
    finally:
        await agent.close()
    if not result.get("ok"):
        raise QualificationFailure(
            "capability_failed", "Home Assistant current power execution failed."
        )
    envelope = result.get("result")
    if not isinstance(envelope, Mapping):
        raise QualificationFailure(
            "capability_failed", "Home Assistant returned no result envelope."
        )
    if (
        envelope.get("kind") != "estimated"
        or envelope.get("unit") != "kW"
        or envelope.get("quantity_shape") != "instantaneous"
        or envelope.get("site_id") != SITE_ID
        or envelope.get("asset_id") != ASSET_ID
    ):
        raise QualificationFailure(
            "semantic_contract_failed", "Home Assistant result semantics were not preserved."
        )
    data = envelope.get("data")
    if (
        not isinstance(data, Mapping)
        or data.get("state") != "1.75"
        or data.get("kind") != "estimated"
    ):
        raise QualificationFailure(
            "semantic_contract_failed", "Synthetic Home Assistant value was not preserved."
        )
    provenance = envelope.get("provenance")
    if not isinstance(provenance, list) or not any(
        isinstance(item, Mapping) and "observation_age_seconds" in item for item in provenance
    ):
        raise QualificationFailure(
            "freshness_failed", "Home Assistant observation freshness was not recorded."
        )
    return {
        "ok": True,
        "provider": "home-assistant",
        "home_assistant_version": HOME_ASSISTANT_VERSION,
        "image": HOME_ASSISTANT_IMAGE,
        "protocol": {
            "onboarding_steps_read": True,
            "owner_created": True,
            "authorization_code_exchanged": True,
            "state_seeded": True,
        },
        "scope": {
            "user_id": USER_ID,
            "site_id": SITE_ID,
            "asset_id": ASSET_ID,
            "account_id": ACCOUNT_ID,
        },
        "synthetic_reading": {
            "entity_id": ENTITY_ID,
            "value": 1.75,
            "unit": "kW",
            "kind": "estimated",
            "quantity_shape": "instantaneous",
            "state_class": None,
        },
        "binding": {
            "reviewed": True,
            "tool": "home_assistant.get_state",
            "scope": "account + site + asset",
        },
        "freshness": {
            "max_age_seconds": 300,
            "observation_timestamp_required": True,
            "provenance_recorded": True,
        },
        "credential_storage": {
            "encrypted_vault": True,
            "profile_excludes_credential": True,
            "output_excludes_credential": True,
        },
        "qualification_limit": "Synthetic Home Assistant API state; no physical meter or device control claim.",
    }


def run_qualification(image: str, startup_timeout: int) -> dict[str, Any]:
    """Create, qualify, and remove one isolated Home Assistant deployment."""

    container_name = _safe_name("eat-ha-qualification")
    volume_name = container_name
    init_name = f"{container_name}-init"
    if not 30 <= startup_timeout <= 300:
        raise QualificationFailure(
            "invalid_timeout", "Startup timeout must be between 30 and 300 seconds."
        )
    try:
        docker("pull", image)
        docker("volume", "create", volume_name)
        docker(*_configuration_command(image, volume_name, init_name), timeout=60)
        docker(
            "run",
            "--detach",
            "--name",
            container_name,
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--publish",
            "127.0.0.1::8123",
            "--mount",
            f"type=volume,source={volume_name},target=/config",
            image,
        )
        port = _published_port(container_name)

        async def run() -> dict[str, Any]:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                follow_redirects=False,
            ) as client:
                await wait_until_ready(client, startup_timeout)
                access_token = await onboard_and_seed(client)
                with tempfile.TemporaryDirectory(prefix="eat-ha-profile-") as profile:
                    return await qualify_profile(
                        client, f"http://127.0.0.1:{port}", Path(profile), access_token
                    )

        return asyncio.run(run())
    finally:
        if not cleanup_owned_resources(container_name, volume_name, init_name):
            raise QualificationFailure(
                "cleanup_failed",
                "Owned Home Assistant Docker resources could not be removed safely.",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=HOME_ASSISTANT_IMAGE)
    parser.add_argument("--startup-timeout", type=int, default=STARTUP_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        result = run_qualification(args.image, args.startup_timeout)
    except QualificationFailure as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": exc.message}}, sort_keys=True
            )
        )
        raise SystemExit(1) from None
    except Exception:
        # Keep unexpected HTTP/client details, which can include request
        # context, out of CI logs. The process still fails for diagnosis from
        # the bounded code path and test evidence.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "qualification_failed",
                        "message": "Home Assistant qualification failed safely.",
                    },
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
