from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from test_host_private_tools import _create_session, _Lifespan
from test_managed_mcp import _serve

from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.hosting import create_host
from energy_agent_tools.models import EnergyError

pytest_plugins = ["test_host_private_tools"]


@pytest.mark.parametrize("auth_scheme", ["none", "bearer"])
async def test_owner_http_review_map_execute_health_recover_and_disconnect(
    private_tools, auth_scheme
):
    fixture = private_tools
    provider_credential = "owned-provider-credential" if auth_scheme == "bearer" else None
    expected_auth = f"Bearer {provider_credential}" if provider_credential else ""
    remote = FastMCP("owned-host-mcp")
    captured_requests: dict[str, list[str]] = {}

    @remote.tool()
    def read_energy(value: str) -> dict[str, str]:
        return {"value": value, "authorization": captured_requests["headers"][-1]}

    @remote.tool()
    def unselected_control() -> dict[str, str]:
        return {"state": "changed"}

    async with _serve(remote, {expected_auth}) as (url, requests, _):
        captured_requests["headers"] = requests

        async def approve(workspace_id, requested_url):
            if workspace_id != fixture.owner.workspace.id or requested_url != url:
                raise EnergyError("mcp_target_invalid", "MCP target is not approved.")
            return await approve_mcp_target(url, allow_private=True)

        fixture.host._managed_mcp_target_approver = approve
        body = {"url": url, "auth_scheme": auth_scheme, "credential": provider_credential}
        inspected = await fixture.client.post(
            "/workspace/mcp/inspect", headers=fixture.owner_auth, json=body
        )
        assert inspected.status_code == 200, inspected.text
        inspection = inspected.json()["inspection"]
        assert inspection["annotations_untrusted"] is True
        assert {tool["name"] for tool in inspection["tools"]} == {
            "read_energy",
            "unselected_control",
        }
        before = len(requests)
        forbidden = await fixture.client.post(
            "/workspace/mcp/inspect", headers=fixture.foreign_auth, json=body
        )
        assert forbidden.status_code >= 400
        assert len(requests) == before
        malformed = await fixture.client.post(
            "/workspace/mcp/inspect",
            headers=fixture.owner_auth,
            json={**body, "allow_private": True},
        )
        assert malformed.status_code == 400
        assert len(requests) == before
        staged = await fixture.client.post(
            "/workspace/mcp/stage",
            headers=fixture.owner_auth,
            json={
                **body,
                "display_name": "Owned synthetic energy reader",
                "schema_digest": inspection["schema_digest"],
                "reviews": [
                    {
                        "name": "read_energy",
                        "reviewed": True,
                        "actions": ["read-only"],
                        "kind": "metered",
                        "unit": "kWh",
                    }
                ],
            },
        )
        assert staged.status_code == 201, staged.text
        account = staged.json()["account"]
        connection_id = account["id"]
        tool_name = f"{connection_id}.read_energy"
        assert account["state"] == "pending_mapping"
        assert not account["enabled"]
        assert connection_id not in fixture.agent.registry.toolkits
        mapped = await fixture.client.post(
            f"/workspace/connections/{connection_id}/map",
            headers=fixture.owner_auth,
            json={"site_id": fixture.owner_site.id},
        )
        assert mapped.status_code == 200, mapped.text
        assert mapped.json()["health"]["provider"] == "custom_mcp"
        assert mapped.json()["health"]["probe"] == "mcp-schema"
        assert fixture.agent.registry.tools[tool_name].account_scope.account_id == connection_id
        assert f"{connection_id}.unselected_control" not in fixture.agent.registry.tools
        owner_session = await _create_session(fixture, fixture.owner_auth, fixture.owner_site.id)
        foreign_session = await _create_session(
            fixture, fixture.foreign_auth, fixture.foreign_site.id
        )
        before = len(requests)
        denied = await fixture.client.post(
            f"/sessions/{foreign_session}/execute",
            headers=fixture.foreign_auth,
            json={"tool": tool_name, "arguments": {"value": "3.5"}},
        )
        assert denied.json()["error"]["code"] == "tool_forbidden"
        assert len(requests) == before
        executed = await fixture.client.post(
            f"/sessions/{owner_session}/execute",
            headers=fixture.owner_auth,
            json={"tool": tool_name, "arguments": {"value": "3.5"}},
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["ok"] is True, executed.text
        assert executed.json()["result"]["kind"] == "metered"
        assert executed.json()["result"]["unit"] == "kWh"
        if provider_credential:
            assert provider_credential not in executed.text
            assert "[REDACTED]" in executed.text
        assert set(requests) == {expected_auth}
        checked = await fixture.client.post(
            f"/workspace/connections/{connection_id}/verify", headers=fixture.owner_auth, json={}
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["health"]["status"] == "healthy"
        fixture.agent.registry.remove_account_toolkit(
            connection_id, account_scope=fixture.agent.registry.get(tool_name).account_scope
        )
        restarted = create_host(
            fixture.agent,
            {},
            control_store=fixture.control,
            managed_workspaces=True,
            managed_mcp_target_approver=approve,
            close_agent_on_shutdown=False,
        )
        async with _Lifespan(restarted):
            assert tool_name in fixture.agent.registry.tools
        recovered = await fixture.client.post(
            "/workspace/mcp/recover", headers=fixture.owner_auth, json={}
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["connections"] == [
            {"connection_id": connection_id, "status": "ready"}
        ]
        assert tool_name in fixture.agent.registry.tools
        disconnected = await fixture.client.post(
            f"/workspace/connections/{connection_id}/disconnect",
            headers=fixture.owner_auth,
            json={},
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["account"]["state"] == "revoked"
        assert connection_id not in fixture.agent.registry.toolkits
        assert tool_name not in fixture.agent.registry.tools
