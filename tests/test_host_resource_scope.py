"""Hosted resource authorization denies shared resources before execution."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_agent_tools.connectors.mcp_bridge import import_mcp
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import DataKind, EnergyResult, Site, Tool, Toolkit, schema
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


@pytest.mark.asyncio
async def test_hosted_unknown_and_operator_tools_are_hidden_and_denied_before_io(tmp_path: Path):
    calls = []
    registry = Registry()
    for toolkit in ["private", "unknown", "calculator"]:
        registry.add_toolkit(
            Toolkit(
                id=toolkit, name=toolkit, description=toolkit, runtime="python", status="stable"
            )
        )

    async def private_handler(args, context):
        calls.append(context.session.access_mode)
        return EnergyResult(
            data={"value": 314.15}, kind=DataKind.METERED, unit="kWh", source="fake"
        )

    async def calculator(args, context):
        return EnergyResult(data={"value": 6}, kind=DataKind.CALCULATED, unit="kWh", source="fake")

    for name, scope, handler in [
        ("private.read", "operator", private_handler),
        ("unknown.read", "unclassified", private_handler),
        ("calculator.energy", "session", calculator),
    ]:
        registry.add(
            Tool(
                name=name,
                toolkit=name.split(".")[0],
                resource_scope=scope,
                description="Energy consumption",
                input_schema=schema({}, []),
                capabilities=["get_energy_consumption"],
            ),
            handler,
        )
    agent = EnergyAgent(
        registry,
        tmp_path / "agent",
        sites=[Site(id="home", user_id="tenant", name="Home", timezone="UTC")],
    )
    host = create_host(agent, {"tenant": Principal("tenant", {"home"}, token_digest("fixture"))})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host),
            base_url="http://127.0.0.1:8000",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            created = await client.post(
                "/sessions", json={"site_id": "home", "access_mode": "local"}
            )
            assert created.status_code == 400
            created = await client.post("/sessions", json={"site_id": "home"})
            prefix = "/sessions/" + created.json()["session_id"]
            catalogue = (await client.get(prefix + "/toolkits")).json()["toolkits"]
            assert [item["id"] for item in catalogue] == ["calculator"]
            search = await client.post(prefix + "/search", json={"query": "energy consumption"})
            assert [item["name"] for item in search.json()["tools"]] == ["calculator.energy"]
            for name in ["private.read", "unknown.read"]:
                result = await client.post(
                    prefix + "/execute", json={"tool": name, "arguments": {}}
                )
                assert result.json()["ok"] is False
                assert result.json()["error"]["code"] == "tool_forbidden"
                assert "314.15" not in result.text
                resolved = await client.post(
                    prefix + "/resolve", json={"capability": "get_energy_consumption", "tool": name}
                )
                assert "314.15" not in resolved.text and name not in resolved.text
            assert calls == []
            allowed = await client.post(
                prefix + "/execute", json={"tool": "calculator.energy", "arguments": {}}
            )
            assert allowed.json()["ok"] is True
        local = await agent.execute(agent.session("tenant", "home"), "private.read", {})
        assert local["ok"] is True and calls == ["local"]
    finally:
        await agent.close()


class _Lifespan:
    """Minimal ASGI lifespan driver for httpx's ASGI transport."""

    def __init__(self, app: Any):
        self.app = app
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_Lifespan":
        async def receive() -> dict[str, Any]:
            return await self.events.get()

        async def send(message: dict[str, Any]) -> None:
            await self.messages.put(message)

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "scope": {}}
        self.task = asyncio.create_task(self.app(scope, receive, send))
        await self.events.put({"type": "lifespan.startup"})
        event = await self.messages.get()
        assert event["type"] == "lifespan.startup.complete", event
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.events.put({"type": "lifespan.shutdown"})
        event = await self.messages.get()
        assert event["type"] == "lifespan.shutdown.complete", event
        assert self.task is not None
        await self.task


@pytest.mark.asyncio
async def test_imported_mcp_is_denied_through_initialized_hosted_mcp(tmp_path: Path):
    registry = Registry()
    await import_mcp(
        registry,
        "private_mcp",
        command=sys.executable,
        args=[str(Path(__file__).parent / "fixtures/mcp_server.py")],
        tool_metadata={
            "energy_echo": {
                "action": "read-only",
                "capabilities": ["private_energy"],
                "kind": "metered",
                "unit": "kWh",
                "reviewed": True,
            }
        },
    )
    assert registry.get("private_mcp.energy_echo").resource_scope == "operator"
    calls = []
    original = registry.handlers["private_mcp.energy_echo"]

    async def observed_handler(arguments, context):
        calls.append(context.session.access_mode)
        return await original(arguments, context)

    registry.handlers["private_mcp.energy_echo"] = observed_handler
    agent = EnergyAgent(
        registry,
        tmp_path / "agent",
        sites=[Site(id="home", user_id="tenant", name="Home", timezone="UTC")],
    )
    host = create_host(agent, {"tenant": Principal("tenant", {"home"}, token_digest("fixture"))})
    try:
        async with _Lifespan(host):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host),
                base_url="http://127.0.0.1:8000",
                headers={
                    "Authorization": "Bearer fixture",
                    "Accept": "application/json, text/event-stream",
                },
            ) as client:
                initialized = await client.post(
                    "/mcp/home",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "resource-test", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200
                headers = {"Mcp-Session-Id": initialized.headers["mcp-session-id"]}
                await client.post(
                    "/mcp/home",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    },
                )
                for index, name, arguments in [
                    (2, "ENERGY_GET_TOOL", {"name": "private_mcp.energy_echo"}),
                    (
                        3,
                        "ENERGY_MULTI_EXECUTE_TOOL",
                        {
                            "calls": [
                                {
                                    "tool": "private_mcp.energy_echo",
                                    "arguments": {"value": 314.15},
                                }
                            ]
                        },
                    ),
                ]:
                    denied = await client.post(
                        "/mcp/home",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": index,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    )
                    assert denied.status_code == 200
                    assert "tool_forbidden" in denied.text and "314.15" not in denied.text
                assert calls == []
        local = await agent.execute(
            agent.session("tenant", "home"), "private_mcp.energy_echo", {"value": 2}
        )
        assert local["ok"] is True and calls == ["local"]
    finally:
        await agent.close()
