from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from energy_agent_tools.capabilities import CapabilityBinding, CapabilityRequest
from energy_agent_tools.models import (
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    Session,
    Site,
    Tool,
    Toolkit,
    schema,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent
from energy_agent_tools.workspace_access import WorkspaceKeyScope, WorkspaceMemberGrants


def test_members_require_complete_identity_and_explicit_grants():
    with pytest.raises(ValidationError):
        Session(user_id="member", resource_owner_id="owner")
    with pytest.raises(ValidationError):
        Session(
            user_id="member",
            workspace_id="workspace",
            resource_owner_id="owner",
            workspace_key_id="key",
            workspace_policy_revision=1,
        )
    with pytest.raises(ValidationError):
        WorkspaceKeyScope(
            actor_user_id="member",
            resource_owner_id="owner",
            workspace_id="workspace",
            key_id="key",
            revision=1,
            site_ids=[],
            connection_ids=None,
        )
    with pytest.raises(ValidationError):
        WorkspaceMemberGrants(site_ids=["site", "site"])


async def test_member_runtime_resolves_only_granted_accounts_and_keeps_actor_artifacts(tmp_path):
    secret = "owner-private-fixture-credential"
    requests = []

    class Vault:
        def workspace_accounts(self, user_id, workspace_id):
            assert (user_id, workspace_id) == ("owner", "workspace")
            return accounts

        def redaction_values(self, user_id):
            return [secret] if user_id == "owner" else []

        def credential(self, user_id, account_id, site_id):
            assert (user_id, account_id, site_id) == ("owner", "shared", "site")
            return secret

        def close(self):
            pass

    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="meter",
            name="Meter",
            description="Fixture meter",
            runtime="http",
            status="stable",
            auth_required=True,
        )
    )

    async def read(arguments, context):
        requests.append((context.session.user_id, context.account.id, context.credential))
        return EnergyResult(
            data={"value": 2, "provider_diagnostic": context.credential},
            kind=DataKind.METERED,
            unit="kWh",
            source="fixture",
        )

    registry.add(
        Tool(
            name="meter.read",
            toolkit="meter",
            description="Read interval energy",
            resource_scope="account",
            input_schema=schema({}, []),
            capabilities=[],
        ),
        read,
    )
    accounts = [
        ConnectedAccount(
            id=identifier,
            user_id="owner",
            workspace_id="workspace",
            site_id="site",
            toolkit="meter",
            last_verified_at=datetime.now(UTC),
            auth=AuthConfig(scheme="bearer", secret_id="fixture-vault-reference"),
        )
        for identifier in ["shared", "private"]
    ]
    agent = EnergyAgent(
        registry,
        tmp_path,
        sites=[Site(id="site", user_id="owner", name="Home", timezone="UTC")],
        accounts=accounts,
        auth_store=Vault(),
        bindings=[
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="meter.read",
                account_id=identifier,
                reviewed=True,
                kind=DataKind.METERED,
                unit="kWh",
            )
            for identifier in ["shared", "private"]
        ],
    )
    active = True

    def authorize(session):
        assert session.user_id == "member" and session.resource_user_id == "owner"
        if not active:
            raise EnergyError("workspace_forbidden", "Workspace authorization changed.")

    agent.workspace_authorizer = authorize
    try:
        session = agent.session(
            "member",
            "site",
            access_mode="hosted",
            workspace_id="workspace",
            resource_owner_id="owner",
            workspace_key_id="member-key",
            workspace_policy_revision=1,
            connection_grants={"shared"},
        )
        assert [item["id"] for item in agent.connections(session)] == ["shared"]
        resolution = agent.resolver.resolve(
            session, CapabilityRequest(capability="get_energy_consumption")
        )
        assert resolution["selected"]["account_id"] == "shared"
        assert [item["account_id"] for item in resolution["candidates"]] == ["shared"]
        denied = await agent.execute(session, "meter.read", {}, account_id="private")
        assert denied["error"]["code"] == "account_forbidden" and not requests
        output = await agent.execute(session, "meter.read", {}, persist=True)
        assert output["ok"] and requests == [("member", "shared", secret)]
        assert secret not in json.dumps(output)
        artifact_id = output["result"]["data"]["artifact_id"]
        assert agent.workbench.read(session, artifact_id).data["value"] == 2
        owner = Session(user_id="owner", id=session.id)
        with pytest.raises(EnergyError):
            agent.workbench.read(owner, artifact_id)
        event = agent.events[-1]
        assert event["user_id"] == "member" and event["key_id"] == "member-key"
        assert event["workspace_id"] == "workspace"
        active = False
        denied = await agent.execute(session, "meter.read", {})
        assert denied["error"]["code"] == "workspace_forbidden"
        assert len(requests) == 1
        with pytest.raises(EnergyError):
            agent.connections(session)
    finally:
        await agent.close()


async def test_missing_live_authorizer_denies_before_expired_credential_refresh(tmp_path):
    registry = Registry()
    registry.add_toolkit(
        Toolkit(id="meter", name="Meter", description="Meter", runtime="http", status="stable")
    )
    calls = []

    async def read(arguments, context):
        calls.append("provider")
        raise AssertionError("Provider must not be called")

    registry.add(
        Tool(
            name="meter.read",
            toolkit="meter",
            description="Meter",
            resource_scope="account",
            capabilities=[],
            input_schema=schema({}, []),
        ),
        read,
    )
    agent = EnergyAgent(
        registry,
        tmp_path,
        sites=[Site(id="site", user_id="owner", name="Home", timezone="UTC")],
        accounts=[
            ConnectedAccount(
                id="shared",
                user_id="owner",
                workspace_id="workspace",
                site_id="site",
                toolkit="meter",
                last_verified_at=datetime.now(UTC),
                auth=AuthConfig(scheme="oauth", secret_id="fixture"),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        ],
    )
    session = Session(
        user_id="member",
        workspace_id="workspace",
        resource_owner_id="owner",
        workspace_key_id="key",
        workspace_policy_revision=1,
        connection_grants={"shared"},
    )
    try:
        output = await agent.execute(session, "meter.read", {})
        assert output["error"]["code"] == "workspace_forbidden" and not calls
    finally:
        await agent.close()
