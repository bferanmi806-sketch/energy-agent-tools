"""Private provider schemas and execution stay with their owned connection."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from energy_agent_tools.capabilities import CapabilityBinding, CapabilityRequest, CapabilityResolver
from energy_agent_tools.models import (
    ConnectedAccount,
    DataKind,
    EnergyError,
    Tool,
    ToolAccountScope,
    Toolkit,
)
from energy_agent_tools.sdk import EnergyAgentTools

NAME = "private_thermal.heat_loss"
ARGS = {
    "indoor_temp_c": 21,
    "outdoor_temp_c": 2,
    "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
    "air_changes_per_hour": 0.4,
}


@pytest.fixture
async def private_service(tmp_path):
    energy = EnergyAgentTools(
        tmp_path,
        {
            "sites": [
                {"id": "home", "user_id": "alice", "name": "Home", "timezone": "UTC"},
                {"id": "other", "user_id": "alice", "name": "Other", "timezone": "UTC"},
            ]
        },
    )
    agent = energy.agent

    def authorize(session):
        assert session.workspace_id == "workspace-a"
        assert session.resource_user_id == "alice"
        assert session.workspace_key_id == "member-key"
        assert session.workspace_policy_revision == 1

    agent.workspace_authorizer = authorize
    agent.registry.add_toolkit(
        Toolkit(
            id="private_thermal",
            name="Private thermal service",
            description="Owner-private schema",
            runtime="native",
            status="experimental",
            auth_required=True,
        )
    )
    original = agent.registry.get("engineering.calculate_heat_loss")
    handler = agent.registry.handlers[original.name]
    calls = []

    async def calculate(arguments, context):
        calls.append(context.account.id)
        return await handler(arguments, context)

    scope = ToolAccountScope(workspace_id="workspace-a", user_id="alice", account_id="connection-a")
    agent.registry.add(
        Tool(
            **{
                **original.model_dump(),
                "name": NAME,
                "toolkit": "private_thermal",
                "resource_scope": "account",
                "account_scope": scope,
            }
        ),
        calculate,
    )
    for identifier in ("connection-a", "connection-b"):
        agent.accounts[identifier] = ConnectedAccount(
            id=identifier,
            user_id="alice",
            workspace_id="workspace-a",
            site_id="home",
            toolkit="private_thermal",
            last_verified_at=datetime.now(UTC),
        )
    agent.resolver = CapabilityResolver(
        agent,
        [
            CapabilityBinding(
                capability="calculate_heat_loss",
                tool=NAME,
                kind=DataKind.CALCULATED,
                unit="W",
                reviewed=True,
            )
        ],
    )
    try:
        yield energy, calls
    finally:
        await energy.close()


def test_owned_schema_binding_is_internal_and_cannot_be_public():
    scope = ToolAccountScope(workspace_id="workspace-a", user_id="alice", account_id="connection-a")
    fields = dict(
        name=NAME,
        toolkit="private_thermal",
        description="Private schema",
        input_schema={"type": "object"},
        capabilities=[],
        account_scope=scope,
    )
    with pytest.raises(ValidationError, match="account resource scope"):
        Tool(**fields, resource_scope="public")
    tool = Tool(**fields, resource_scope="account")
    assert "account_scope" not in tool.public()
    assert "account_scope" not in Tool.model_json_schema(mode="serialization")["properties"]
    with pytest.raises(ValidationError):
        ToolAccountScope(workspace_id=" ", user_id="alice", account_id="connection-a")


async def test_private_schema_is_hidden_across_workspace_owner_site_and_grant_boundaries(
    private_service,
):
    energy, calls = private_service
    own = energy.session("alice", "home", workspace_id="workspace-a", access_mode="hosted")
    assert own.agent.get_tool(own.context, NAME)["resource_scope"] == "account"
    denied = [
        energy.session("alice", "home", workspace_id="workspace-b", access_mode="local"),
        energy.session("alice", "home", workspace_id="workspace-b", access_mode="hosted"),
        energy.session("bob", None, workspace_id="workspace-a", access_mode="hosted"),
        energy.session("alice", "other", workspace_id="workspace-a", access_mode="hosted"),
        energy.session(
            "bob",
            "home",
            workspace_id="workspace-a",
            access_mode="hosted",
            resource_owner_id="alice",
            workspace_key_id="member-key",
            workspace_policy_revision=1,
            connection_grants=set(),
        ),
    ]
    for session in denied:
        assert NAME not in [tool["name"] for tool in session.search("thermal heat loss", 10)]
        assert "private_thermal" not in [
            toolkit["id"] for toolkit in energy.agent.catalogue(session.context)
        ]
        with pytest.raises(EnergyError) as error:
            energy.agent.get_tool(session.context, NAME)
        assert error.value.code == "tool_forbidden"
        result = await session.execute(NAME, ARGS)
        assert result["ok"] is False and result["error"]["code"] == "tool_forbidden"
    assert calls == []


async def test_private_tool_resolves_its_exact_account_and_rejects_retargeting(private_service):
    energy, calls = private_service
    own = energy.session(
        "alice",
        "home",
        workspace_id="workspace-a",
        access_mode="hosted",
        account_ids={"private_thermal": "connection-b"},
    )
    candidate = energy.agent.resolver.resolve(
        own.context,
        CapabilityRequest(
            capability="calculate_heat_loss",
            tool=NAME,
            arguments=ARGS,
        ),
    )
    assert candidate["status"] == "resolved", candidate
    assert candidate["selected"]["account_id"] == "connection-a"
    result = await own.execute(NAME, ARGS)
    assert result["ok"] and result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
    assert calls == ["connection-a"]
    rejected = await own.execute(NAME, ARGS, account_id="connection-b")
    assert rejected["ok"] is False and rejected["error"]["code"] == "account_forbidden"
    assert calls == ["connection-a"]


async def test_shared_private_tool_requires_active_matching_connection(private_service):
    energy, calls = private_service
    shared = energy.session(
        "bob",
        "home",
        workspace_id="workspace-a",
        access_mode="hosted",
        resource_owner_id="alice",
        workspace_key_id="member-key",
        workspace_policy_revision=1,
        connection_grants={"connection-a"},
    )
    result = await shared.execute(NAME, ARGS)
    assert result["ok"] and calls == ["connection-a"]
    account = energy.agent.accounts["connection-a"]
    energy.agent.accounts[account.id] = account.model_copy(
        update={"enabled": False, "state": "disabled"}
    )
    assert "private_thermal" not in [
        toolkit["id"] for toolkit in energy.agent.catalogue(shared.context)
    ]
    rejected = await shared.execute(NAME, ARGS)
    assert rejected["ok"] is False and rejected["error"]["code"] == "tool_forbidden"
    assert calls == ["connection-a"]


async def test_private_schema_refreshes_disabled_and_removed_no_auth_connections(private_service):
    energy, calls = private_service
    account = energy.agent.accounts["connection-a"]
    records = [account]

    class Store:
        def workspace_accounts(self, user_id, workspace_id):
            assert (user_id, workspace_id) == ("alice", "workspace-a")
            return records

        def redaction_values(self, user_id):
            return []

        def close(self):
            pass

    energy.agent.auth_store = Store()
    own = energy.session("alice", "home", workspace_id="workspace-a", access_mode="hosted")
    assert NAME == energy.agent.get_tool(own.context, NAME)["name"]
    records[:] = [account.model_copy(update={"enabled": False, "state": "disabled"})]
    with pytest.raises(EnergyError, match="resource scope"):
        energy.agent.get_tool(own.context, NAME)
    records[:] = [account]
    assert "private_thermal" in [item["id"] for item in energy.agent.catalogue(own.context)]
    records.clear()
    assert "private_thermal" not in [item["id"] for item in energy.agent.catalogue(own.context)]
    assert "connection-a" not in energy.agent.accounts
    result = await own.execute(NAME, ARGS)
    assert result["ok"] is False and result["error"]["code"] == "tool_forbidden"
    assert calls == []
