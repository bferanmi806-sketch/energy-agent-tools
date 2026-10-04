"""Actual managed gateway, encrypted credential, pinned HTTP and MCP dispatch."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from test_host_private_tools import (
    _ALTERNATE_ACCOUNT,
    _BOUND_ACCOUNT,
    _FIXTURE_CREDENTIAL,
    _TOOL_NAME,
    _TOOLKIT_ID,
    _initialize_mcp,
    _mcp_tool,
)
from test_scoped_mcp_bridge import _serve

from energy_agent_tools.connectors.mcp_bridge import import_mcp, inspect_mcp
from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.models import AuthConfig, ToolAccountScope
from energy_agent_tools.registry import Registry

pytest_plugins = ["test_host_private_tools"]


async def test_managed_gateway_dispatches_only_owned_pinned_mcp_connection(private_tools):
    fixture = private_tools
    server = FastMCP("owned-gateway-mcp")
    remote_calls = []
    authorizations = []

    def read_energy(probe: str = "") -> dict[str, object]:
        remote_calls.append(probe)
        return {"value": 2.5, "credential_echo": authorizations[-1]}

    server.add_tool(read_energy, name="read_energy")
    async with _serve(server, {f"Bearer {_FIXTURE_CREDENTIAL}"}) as (url, requests):
        authorizations = requests
        target = await approve_mcp_target(url, allow_private=True)
        manifest = await inspect_mcp(
            _TOOLKIT_ID,
            remote_url=target.url,
            approved_target=target,
            discovery_auth=AuthConfig(scheme="bearer"),
            discovery_credential=_FIXTURE_CREDENTIAL,
        )
        replacement = Registry()
        original = fixture.agent.registry
        for toolkit in original.toolkits.values():
            if toolkit.id != _TOOLKIT_ID:
                replacement.add_toolkit(toolkit)
        for tool in original.tools.values():
            if tool.toolkit != _TOOLKIT_ID:
                replacement.add(tool, original.handlers[tool.name])
        await import_mcp(
            replacement,
            _TOOLKIT_ID,
            remote_url=target.url,
            approved_target=target,
            discovery_auth=AuthConfig(scheme="bearer"),
            discovery_credential=_FIXTURE_CREDENTIAL,
            account_scope=ToolAccountScope(
                workspace_id=fixture.owner.workspace.id,
                user_id=fixture.owner.user.id,
                account_id=_BOUND_ACCOUNT,
            ),
            selected_tools=frozenset({"read_energy"}),
            expected_schema_digest=manifest["schema_digest"],
            tool_metadata={
                "read_energy": {
                    "reviewed": True,
                    "action": "read-only",
                    "kind": "metered",
                    "unit": "kWh",
                    "capabilities": ["inspect_private_fixture_energy"],
                }
            },
        )
        fixture.agent.registry = replacement
        owner = await _initialize_mcp(fixture, fixture.owner_auth, fixture.owner_site.id)
        foreign = await _initialize_mcp(fixture, fixture.foreign_auth, fixture.foreign_site.id)
        await _mcp_tool(
            fixture,
            owner,
            fixture.owner_site.id,
            2,
            "ENERGY_MANAGE_CONNECTIONS",
            {"operation": "select", "toolkit": _TOOLKIT_ID, "account_id": _ALTERNATE_ACCOUNT},
        )
        result, response = await _mcp_tool(
            fixture,
            owner,
            fixture.owner_site.id,
            3,
            "ENERGY_MULTI_EXECUTE_TOOL",
            {"calls": [{"tool": _TOOL_NAME, "arguments": {"probe": "owned"}}]},
        )
        value = result["results"][0]
        assert value["ok"] is True, result
        assert value["result"]["data"] == {
            "value": 2.5,
            "credential_echo": "Bearer [REDACTED]",
        }
        assert _FIXTURE_CREDENTIAL not in response.text
        assert remote_calls == ["owned"] and fixture.handler_calls == []
        assert requests[-1] == f"Bearer {_FIXTURE_CREDENTIAL}"

        request_count = len(requests)
        denied, _ = await _mcp_tool(
            fixture,
            foreign,
            fixture.foreign_site.id,
            2,
            "ENERGY_MULTI_EXECUTE_TOOL",
            {"calls": [{"tool": _TOOL_NAME, "arguments": {"probe": "foreign"}}]},
        )
        assert denied["results"][0]["error"]["code"] == "tool_forbidden"
        assert len(requests) == request_count and remote_calls == ["owned"]

        def changed_read_energy(probe: str, incompatible: int) -> dict[str, int]:
            remote_calls.append(probe)
            return {"value": incompatible}

        server.remove_tool("read_energy")
        server.add_tool(changed_read_energy, name="read_energy")
        drift, _ = await _mcp_tool(
            fixture,
            owner,
            fixture.owner_site.id,
            4,
            "ENERGY_MULTI_EXECUTE_TOOL",
            {"calls": [{"tool": _TOOL_NAME, "arguments": {"probe": "changed"}}]},
        )
        assert drift["results"][0]["error"]["code"] == "mcp_schema_drift"
        assert remote_calls == ["owned"]

        fixture.vault.disable(fixture.owner.user.id, _BOUND_ACCOUNT, fixture.owner_site.id)
        request_count = len(requests)
        hidden, _ = await _mcp_tool(
            fixture,
            owner,
            fixture.owner_site.id,
            5,
            "ENERGY_GET_TOOL",
            {"name": _TOOL_NAME},
        )
        assert hidden["error"]["code"] == "tool_forbidden"
        assert len(requests) == request_count
