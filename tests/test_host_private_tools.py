from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from test_host_connection_onboarding import _rpc
from test_hosting import _Lifespan

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.models import (
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyResult,
    ExecutionContext,
    Tool,
    ToolAccountScope,
    Toolkit,
    schema,
)

_TOOLKIT_ID = "private-fixture-native"
_TOOL_NAME = f"{_TOOLKIT_ID}.read_energy"
_BOUND_ACCOUNT = "fixture-bound"
_ALTERNATE_ACCOUNT = "fixture-alternate"
_FIXTURE_CREDENTIAL = "private-fixture-credential"


@pytest.fixture
async def private_tools(tmp_path):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Private tools")
    foreign = control.bootstrap_workspace("Foreign", "Other workspace")
    member = control.create_user("member", "Member")
    owner_site = control.create_site(
        owner.user.id, owner.workspace.id, name="Owner home", timezone="UTC"
    )
    foreign_site = control.create_site(
        foreign.user.id, foreign.workspace.id, name="Foreign home", timezone="UTC"
    )
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    verified_at = datetime.now(UTC)
    for account_id in (_BOUND_ACCOUNT, _ALTERNATE_ACCOUNT):
        vault.stage_managed(
            ConnectedAccount(
                id=account_id,
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                toolkit=_TOOLKIT_ID,
                state="pending_mapping",
                enabled=False,
                last_verified_at=verified_at,
                auth=AuthConfig(scheme="bearer"),
            ),
            _FIXTURE_CREDENTIAL if account_id == _BOUND_ACCOUNT else "alternate-fixture-secret",
        )
        _, revision = vault.managed_snapshot(owner.user.id, owner.workspace.id, account_id)
        vault.activate_managed(
            owner.user.id,
            owner.workspace.id,
            account_id,
            site=owner_site,
            expected_version=revision,
            verified_at=verified_at,
        )

    agent = build_agent(tmp_path / "agent")
    agent.auth_store = vault
    agent.registry.add_toolkit(
        Toolkit(
            id=_TOOLKIT_ID,
            name="Private native fixture",
            description="Test-only deterministic native fixture; it does not call a provider.",
            runtime="native",
            status="experimental",
            auth_required=True,
        )
    )
    handler_calls: list[dict[str, str | None]] = []

    async def read_energy(arguments: dict[str, Any], context: ExecutionContext) -> EnergyResult:
        account_id = context.account.id if context.account is not None else None
        handler_calls.append({"account_id": account_id, "actor_id": context.session.user_id})
        return EnergyResult(
            data={
                "account_id": account_id,
                "actor_id": context.session.user_id,
                "probe": arguments.get("probe"),
                "credential_echo": context.credential,
            },
            kind=DataKind.METERED,
            unit="kWh",
            source="native-fixture",
            site_id=context.site_id,
        )

    agent.registry.add(
        Tool(
            name=_TOOL_NAME,
            toolkit=_TOOLKIT_ID,
            resource_scope="account",
            account_scope=ToolAccountScope(
                workspace_id=owner.workspace.id,
                user_id=owner.user.id,
                account_id=_BOUND_ACCOUNT,
            ),
            description="Read private fixture energy consumption using a mapped meter.",
            input_schema=schema({"probe": {"type": "string"}}, []),
            capabilities=["inspect_private_fixture_energy"],
        ),
        read_energy,
    )
    host = create_host(
        agent, {}, control_store=control, managed_workspaces=True, max_requests_per_minute=500
    )
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            yield SimpleNamespace(
                client=client,
                host=host,
                agent=agent,
                control=control,
                vault=vault,
                owner=owner,
                owner_site=owner_site,
                foreign=foreign,
                foreign_site=foreign_site,
                member=member,
                owner_auth={"Authorization": f"Bearer {owner.key.token}"},
                foreign_auth={"Authorization": f"Bearer {foreign.key.token}"},
                handler_calls=handler_calls,
            )
    finally:
        await agent.close()
        vault.close()
        control.close()


async def _create_session(fixture, auth: dict[str, str], site_id: str) -> str:
    response = await fixture.client.post("/sessions", headers=auth, json={"site_id": site_id})
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


async def _initialize_mcp(fixture, auth: dict[str, str], site_id: str) -> dict[str, str]:
    headers = {**auth, "Accept": "application/json, text/event-stream"}
    response = await fixture.client.post(
        f"/mcp/{site_id}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "private-tool-fixture", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    initialized = await fixture.client.post(
        f"/mcp/{site_id}",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code == 202, initialized.text
    return headers


async def _mcp_tool(
    fixture,
    headers: dict[str, str],
    site_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], httpx.Response]:
    response = await fixture.client.post(
        f"/mcp/{site_id}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    payload = _rpc(response)
    result = payload.get("result", {})
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, response
    text_content = next(
        (item.get("text") for item in result.get("content", []) if item.get("type") == "text"),
        None,
    )
    assert isinstance(text_content, str), response.text
    parsed = json.loads(text_content)
    assert isinstance(parsed, dict), response.text
    return parsed, response


async def test_private_tool_visibility_execution_and_bound_account_are_hosted(private_tools):
    fixture = private_tools
    owner_session = await _create_session(fixture, fixture.owner_auth, fixture.owner_site.id)
    foreign_session = await _create_session(fixture, fixture.foreign_auth, fixture.foreign_site.id)

    owner_catalogue = await fixture.client.get("/workspace/toolkits", headers=fixture.owner_auth)
    assert owner_catalogue.status_code == 200, owner_catalogue.text
    assert _TOOLKIT_ID in {item["id"] for item in owner_catalogue.json()["toolkits"]}
    foreign_catalogue = await fixture.client.get(
        "/workspace/toolkits", headers=fixture.foreign_auth
    )
    assert foreign_catalogue.status_code == 200, foreign_catalogue.text
    assert _TOOLKIT_ID not in {item["id"] for item in foreign_catalogue.json()["toolkits"]}

    owner_search = await fixture.client.post(
        f"/sessions/{owner_session}/search",
        headers=fixture.owner_auth,
        json={"query": "private fixture energy meter"},
    )
    assert owner_search.status_code == 200, owner_search.text
    assert _TOOL_NAME in {item["name"] for item in owner_search.json()["tools"]}
    foreign_search = await fixture.client.post(
        f"/sessions/{foreign_session}/search",
        headers=fixture.foreign_auth,
        json={"query": "private fixture energy meter"},
    )
    assert foreign_search.status_code == 200, foreign_search.text
    assert _TOOL_NAME not in foreign_search.text

    foreign_execute = await fixture.client.post(
        f"/sessions/{foreign_session}/execute",
        headers=fixture.foreign_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "foreign"}},
    )
    assert foreign_execute.status_code == 200, foreign_execute.text
    assert foreign_execute.json()["error"]["code"] == "tool_forbidden"
    assert fixture.handler_calls == []

    foreign_resolution = await fixture.client.post(
        f"/sessions/{foreign_session}/resolve",
        headers=fixture.foreign_auth,
        json={"capability": "inspect_private_fixture_energy", "tool": _TOOL_NAME},
    )
    assert foreign_resolution.status_code == 200, foreign_resolution.text
    assert foreign_resolution.json()["selected"] is None
    assert foreign_resolution.json()["candidates"] == []

    owner_mcp = await _initialize_mcp(fixture, fixture.owner_auth, fixture.owner_site.id)
    foreign_mcp = await _initialize_mcp(fixture, fixture.foreign_auth, fixture.foreign_site.id)
    owner_schema, owner_schema_response = await _mcp_tool(
        fixture, owner_mcp, fixture.owner_site.id, 2, "ENERGY_GET_TOOL", {"name": _TOOL_NAME}
    )
    assert owner_schema["name"] == _TOOL_NAME
    assert "account_scope" not in owner_schema
    foreign_schema, foreign_schema_response = await _mcp_tool(
        fixture, foreign_mcp, fixture.foreign_site.id, 2, "ENERGY_GET_TOOL", {"name": _TOOL_NAME}
    )
    assert foreign_schema["error"]["code"] == "tool_forbidden"
    assert _TOOL_NAME not in foreign_schema_response.text

    selected, _ = await _mcp_tool(
        fixture,
        owner_mcp,
        fixture.owner_site.id,
        3,
        "ENERGY_MANAGE_CONNECTIONS",
        {
            "operation": "select",
            "toolkit": _TOOLKIT_ID,
            "account_id": _ALTERNATE_ACCOUNT,
        },
    )
    assert selected["selected"]["id"] == _ALTERNATE_ACCOUNT
    executed, execution_response = await _mcp_tool(
        fixture,
        owner_mcp,
        fixture.owner_site.id,
        4,
        "ENERGY_MULTI_EXECUTE_TOOL",
        {"calls": [{"tool": _TOOL_NAME, "arguments": {"probe": "owner"}}]},
    )
    result = executed["results"][0]
    assert result["ok"] is True, executed
    assert result["result"]["data"] == {
        "account_id": _BOUND_ACCOUNT,
        "actor_id": fixture.owner.user.id,
        "credential_echo": "[REDACTED]",
        "probe": "owner",
    }
    assert _FIXTURE_CREDENTIAL not in execution_response.text
    assert fixture.handler_calls == [
        {"account_id": _BOUND_ACCOUNT, "actor_id": fixture.owner.user.id}
    ]

    retargeted, _ = await _mcp_tool(
        fixture,
        owner_mcp,
        fixture.owner_site.id,
        5,
        "ENERGY_MULTI_EXECUTE_TOOL",
        {
            "calls": [
                {
                    "tool": _TOOL_NAME,
                    "account_id": _ALTERNATE_ACCOUNT,
                    "arguments": {"probe": "retarget"},
                }
            ]
        },
    )
    assert retargeted["results"][0]["error"]["code"] == "account_forbidden"
    assert len(fixture.handler_calls) == 1
    assert _FIXTURE_CREDENTIAL not in owner_schema_response.text


async def test_member_needs_grants_and_live_authorization_precedes_execution(private_tools):
    fixture = private_tools
    added = await fixture.client.post(
        "/workspace/members",
        headers=fixture.owner_auth,
        json={"user_id": fixture.member.id},
    )
    assert added.status_code == 201, added.text

    no_grant_key = await fixture.client.post(
        f"/workspace/members/{fixture.member.id}/keys",
        headers=fixture.owner_auth,
        json={"name": "Premature key", "site_ids": [fixture.owner_site.id]},
    )
    assert no_grant_key.status_code >= 400, no_grant_key.text

    granted = await fixture.client.patch(
        f"/workspace/members/{fixture.member.id}",
        headers=fixture.owner_auth,
        json={
            "site_ids": [fixture.owner_site.id],
            "connection_ids": [_BOUND_ACCOUNT],
        },
    )
    assert granted.status_code == 200, granted.text
    issued = await fixture.client.post(
        f"/workspace/members/{fixture.member.id}/keys",
        headers=fixture.owner_auth,
        json={"name": "Member fixture key", "site_ids": [fixture.owner_site.id]},
    )
    assert issued.status_code == 201, issued.text
    member_auth = {"Authorization": f"Bearer {issued.json()['token']}"}
    member_session = await _create_session(fixture, member_auth, fixture.owner_site.id)

    connections = await fixture.client.get(
        f"/sessions/{member_session}/connections", headers=member_auth
    )
    assert connections.status_code == 200, connections.text
    assert {item["id"] for item in connections.json()["connections"]} == {_BOUND_ACCOUNT}
    execution = await fixture.client.post(
        f"/sessions/{member_session}/execute",
        headers=member_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "member"}},
    )
    assert execution.status_code == 200, execution.text
    assert execution.json()["ok"] is True, execution.text
    assert execution.json()["result"]["data"]["actor_id"] == fixture.member.id
    assert execution.json()["result"]["data"]["account_id"] == _BOUND_ACCOUNT
    assert execution.json()["result"]["data"]["credential_echo"] == "[REDACTED]"
    assert _FIXTURE_CREDENTIAL not in execution.text
    assert fixture.handler_calls == [{"account_id": _BOUND_ACCOUNT, "actor_id": fixture.member.id}]

    removed_grant = await fixture.client.patch(
        f"/workspace/members/{fixture.member.id}",
        headers=fixture.owner_auth,
        json={"site_ids": [fixture.owner_site.id], "connection_ids": []},
    )
    assert removed_grant.status_code == 200, removed_grant.text
    stale_session = await fixture.client.post(
        f"/sessions/{member_session}/execute",
        headers=member_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "stale"}},
    )
    assert stale_session.status_code == 403, stale_session.text
    assert stale_session.json()["error"]["code"] == "workspace_forbidden"

    fresh_session = await _create_session(fixture, member_auth, fixture.owner_site.id)
    denied = await fixture.client.post(
        f"/sessions/{fresh_session}/execute",
        headers=member_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "no-grant"}},
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["error"]["code"] == "tool_forbidden"
    assert len(fixture.handler_calls) == 1

    revoked = await fixture.client.delete(
        f"/workspace/keys/{issued.json()['key']['id']}", headers=fixture.owner_auth
    )
    assert revoked.status_code == 200, revoked.text
    assert (await fixture.client.get("/me", headers=member_auth)).status_code == 401
    revoked_session = await fixture.client.post(
        f"/sessions/{fresh_session}/execute",
        headers=member_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "revoked-key"}},
    )
    assert revoked_session.status_code == 401, revoked_session.text
    assert len(fixture.handler_calls) == 1


@pytest.mark.parametrize("lifecycle", ["disable", "revoke"])
async def test_connection_lifecycle_refreshes_scope_before_catalogue_and_execution(
    private_tools, lifecycle: str
):
    fixture = private_tools
    session_id = await _create_session(fixture, fixture.owner_auth, fixture.owner_site.id)
    active_catalogue = await fixture.client.get("/workspace/toolkits", headers=fixture.owner_auth)
    assert active_catalogue.status_code == 200, active_catalogue.text
    assert _TOOLKIT_ID in {item["id"] for item in active_catalogue.json()["toolkits"]}
    active_execution = await fixture.client.post(
        f"/sessions/{session_id}/execute",
        headers=fixture.owner_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "before-lifecycle"}},
    )
    assert active_execution.json()["ok"] is True, active_execution.text
    assert len(fixture.handler_calls) == 1

    updated = getattr(fixture.vault, lifecycle)(
        fixture.owner.user.id, _BOUND_ACCOUNT, fixture.owner_site.id
    )
    assert updated.state == ("disabled" if lifecycle == "disable" else "revoked")
    assert updated.enabled is False

    # The next catalogue request refreshes from AuthStore; no connection-list or
    # provider refresh request is needed to remove the private toolkit.
    inactive_catalogue = await fixture.client.get("/workspace/toolkits", headers=fixture.owner_auth)
    assert inactive_catalogue.status_code == 200, inactive_catalogue.text
    assert _TOOLKIT_ID not in {item["id"] for item in inactive_catalogue.json()["toolkits"]}
    inactive_search = await fixture.client.post(
        f"/sessions/{session_id}/search",
        headers=fixture.owner_auth,
        json={"query": "private fixture energy meter"},
    )
    assert inactive_search.status_code == 200, inactive_search.text
    assert _TOOL_NAME not in inactive_search.text
    inactive_execution = await fixture.client.post(
        f"/sessions/{session_id}/execute",
        headers=fixture.owner_auth,
        json={"tool": _TOOL_NAME, "arguments": {"probe": "after-lifecycle"}},
    )
    assert inactive_execution.status_code == 200, inactive_execution.text
    assert inactive_execution.json()["error"]["code"] == "tool_forbidden"
    assert len(fixture.handler_calls) == 1
