from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from energy_agent_tools.connectors.mcp_bridge import import_mcp
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


@pytest.mark.parametrize("authenticated", [False, True])
async def test_remote_mcp_discovery_and_call_through_gateway(tmp_path, monkeypatch, authenticated):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    secret = "remote-fixture-private"
    monkeypatch.setenv("REMOTE_FIXTURE_SECRET", secret)
    extra = ["--require-auth"] if authenticated else []
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fixture),
        "--http",
        "--port",
        str(port),
        *extra,
        env=dict(os.environ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(timeout=0.3) as client:
            for _ in range(80):
                if process.returncode is not None:
                    raise AssertionError("Fixture exited before serving HTTP")
                try:
                    await client.get(f"http://127.0.0.1:{port}/mcp")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            else:
                raise AssertionError("HTTP fixture did not become ready")
        registry = Registry()
        auth = AuthConfig(scheme="bearer", credential_env="REMOTE_FIXTURE_SECRET")
        if authenticated:
            with pytest.raises(EnergyError):
                await import_mcp(registry, "denied", url=f"http://127.0.0.1:{port}/mcp")
            assert not registry.toolkits
        await import_mcp(
            registry,
            "remote",
            url=f"http://127.0.0.1:{port}/mcp",
            credential_env="REMOTE_FIXTURE_SECRET" if authenticated else None,
            discovery_auth=auth if authenticated else None,
            tool_metadata={
                "energy_sum": {"actions": ["calculation"], "kind": "calculated", "unit": "kWh"}
            },
        )
        assert registry.toolkits["remote"].runtime == "mcp-remote"
        accounts = (
            [ConnectedAccount(id="remote-account", toolkit="remote", user_id="u", auth=auth)]
            if authenticated
            else []
        )
        agent = EnergyAgent(registry, tmp_path, accounts=accounts)
        try:
            result = await agent.execute(
                agent.session("u"), "remote.energy_sum", {"left": 2, "right": 3}
            )
            assert result["ok"], result
            assert result["result"]["data"]["sum"] == 5
            assert secret not in str(result)
            assert secret not in str(agent.catalogue(agent.session("u")))
            denied = await agent.execute(agent.session("u"), "remote.energy_echo", {"value": 1})
            assert denied["error"]["code"] == "policy_denied"
        finally:
            await agent.close()
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)
