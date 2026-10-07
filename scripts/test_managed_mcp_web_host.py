"""Synthetic managed MCP gateway and upstream for production web acceptance tests.

Both listeners are bound to loopback. The gateway trusts exactly one upstream
URL for exactly one managed workspace, and provider observations record only
boolean credential matches rather than secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from energy_agent_tools.app import build_agent
from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.models import EnergyError


def _write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    path.chmod(0o600)


def _initialize_fixture(state_dir: Path) -> dict[str, str]:
    metadata_path = state_dir / "fixture.json"
    if metadata_path.exists():
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "owner_user_id",
            "owner_workspace_id",
            "site_id",
            "other_site_id",
        }:
            raise RuntimeError("Managed MCP acceptance fixture metadata is invalid.")
        return {key: str(item) for key, item in value.items()}

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    control = ControlStore(state_dir / "control")
    owner = control.bootstrap_workspace("MCP acceptance owner", "MCP acceptance workspace")
    site = control.create_site(
        owner.user.id,
        owner.workspace.id,
        name="MCP acceptance site",
        timezone="Europe/London",
    )
    other_site = control.create_site(
        owner.user.id,
        owner.workspace.id,
        name="Unmapped acceptance site",
        timezone="Europe/London",
    )
    foreign = control.bootstrap_workspace("Other MCP owner", "Other workspace")
    _write_private(state_dir / "manager.token", owner.key.token)
    _write_private(state_dir / "foreign-manager.token", foreign.key.token)
    value = {
        "owner_user_id": owner.user.id,
        "owner_workspace_id": owner.workspace.id,
        "site_id": site.id,
        "other_site_id": other_site.id,
    }
    _write_private(metadata_path, json.dumps(value, separators=(",", ":")))
    observations = state_dir / "provider-observations.jsonl"
    if not observations.exists():
        _write_private(observations, "")
    key_path = state_dir / "agent" / "vault.key"
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(key_path, Fernet.generate_key().decode("ascii"))
    control.close()
    return value


class _ProviderAuthProbe:
    def __init__(
        self,
        app: Any,
        *,
        expected_authorization: str,
        management_authorization: str,
        observations_path: Path,
    ) -> None:
        self.app = app
        self.expected_authorization = expected_authorization
        self.management_authorization = management_authorization
        self.observations_path = observations_path
        self.lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            observation = {
                "path": str(scope.get("path", "")),
                "provider_auth": hmac.compare_digest(authorization, self.expected_authorization),
                "management_auth": hmac.compare_digest(
                    authorization, self.management_authorization
                ),
            }
            with self.lock:
                with self.observations_path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(observation, separators=(",", ":")) + "\n")
            if not observation["provider_auth"]:
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _start_upstream(
    port: int,
    *,
    provider_credential: str,
    management_token: str,
    observations_path: Path,
) -> tuple[uvicorn.Server, threading.Thread]:
    server = FastMCP("managed-mcp-web-acceptance")

    def read_energy() -> dict[str, Any]:
        return {
            "value": 1.25,
            "unit": "kWh",
            "authorization": f"Bearer {provider_credential}",
        }

    def unselected_control() -> dict[str, str]:
        return {"state": "synthetic-only"}

    server.add_tool(read_energy, name="read_energy", description="Read a fixture value")
    server.add_tool(unselected_control, name="unselected_control")
    app = server.streamable_http_app()
    app.add_middleware(
        _ProviderAuthProbe,
        expected_authorization=f"Bearer {provider_credential}",
        management_authorization=f"Bearer {management_token}",
        observations_path=observations_path,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
    )
    upstream = uvicorn.Server(config)
    thread = threading.Thread(target=upstream.run, name="managed-mcp-upstream", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not upstream.started:
        if not thread.is_alive():
            raise RuntimeError("Synthetic MCP upstream could not start.")
        if time.monotonic() >= deadline:
            upstream.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("Synthetic MCP upstream startup timed out.")
        time.sleep(0.025)
    return upstream, thread


def _create_app(state_dir: Path, upstream_url: str):
    metadata = _initialize_fixture(state_dir)
    control = ControlStore(state_dir / "control")
    agent = build_agent(
        state_dir / "agent",
        {"vault": {"master_key_file": "vault.key"}},
    )

    async def approve_target(workspace_id: str, requested_url: str):
        if not hmac.compare_digest(
            workspace_id, metadata["owner_workspace_id"]
        ) or not hmac.compare_digest(requested_url, upstream_url):
            raise EnergyError("mcp_target_invalid", "MCP target is not approved.")
        return await approve_mcp_target(requested_url, allow_private=True)

    host = create_host(
        agent,
        {},
        control_store=control,
        managed_workspaces=True,
        managed_mcp_target_approver=approve_target,
        max_requests_per_minute=300,
        close_agent_on_shutdown=True,
    )
    return host, agent, control


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="Managed gateway loopback port")
    parser.add_argument(
        "--upstream-port", type=int, required=True, help="Synthetic MCP loopback port"
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()

    provider_credential = os.environ.get("ENERGY_MCP_WEB_TEST_PROVIDER_CREDENTIAL", "")
    if not provider_credential:
        raise RuntimeError("Synthetic MCP provider credential is required.")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _initialize_fixture(args.state_dir)
    upstream_url = f"http://127.0.0.1:{args.upstream_port}/mcp"
    management_token = (args.state_dir / "manager.token").read_text(encoding="ascii")
    observations = args.state_dir / "provider-observations.jsonl"
    upstream, thread = _start_upstream(
        args.upstream_port,
        provider_credential=provider_credential,
        management_token=management_token,
        observations_path=observations,
    )
    host, agent, control = _create_app(args.state_dir, upstream_url)
    try:
        uvicorn.run(
            host,
            host="127.0.0.1",
            port=args.port,
            log_level="critical",
            access_log=False,
        )
    finally:
        upstream.should_exit = True
        thread.join(timeout=10)
        asyncio.run(agent.close())
        control.close()


if __name__ == "__main__":
    main()
