from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Session,
    Site,
    Tool,
    ToolAccountScope,
    Toolkit,
    schema,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def _pending_account(
    connection_id: str = "no-auth-managed",
    *,
    user_id: str = "owner",
    workspace_id: str = "workspace",
    scheme: str = "none",
) -> ConnectedAccount:
    return ConnectedAccount(
        id=connection_id,
        user_id=user_id,
        workspace_id=workspace_id,
        toolkit="no-auth-toolkit",
        auth=AuthConfig(scheme=scheme),
        enabled=False,
        state="pending_mapping",
        last_verified_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
    )


def _site(site_id: str = "site", user_id: str = "owner") -> Site:
    return Site(id=site_id, user_id=user_id, name="Fixture site", timezone="UTC")


@pytest.mark.asyncio
async def test_no_auth_managed_account_reopens_activates_and_executes_without_credential(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key()
    store = AuthStore(tmp_path / "vault", key)

    # Replacing a pending secret with an auth-none account overwrites the real
    # encrypted payload with JSON null and clears the vault reference.
    old_pending = _pending_account(scheme="bearer")
    store.stage_managed(old_pending, "old-private-credential")
    _, first_revision = store.managed_snapshot("owner", "workspace", old_pending.id)
    pending = _pending_account()
    staged = store.stage_managed(pending, None, expected_version=first_revision)
    assert staged.state == "pending_mapping"
    assert not staged.enabled
    assert staged.auth.secret_id is None
    assert staged.auth.credential_env is None
    assert "secret_id" not in staged.public()
    assert store.managed_snapshot("owner", "workspace", pending.id)[1] == first_revision + 1

    row = store._db.execute(
        "SELECT account_json, secret_blob FROM accounts WHERE id = ?", (pending.id,)
    ).fetchone()
    assert json.loads(row["account_json"])["auth"]["secret_id"] is None
    assert store._decrypt_json(row["secret_blob"]) == {
        "credential": None,
        "refresh_token": None,
        "provider": None,
    }
    with pytest.raises(EnergyError) as missing:
        store.pending_credential("owner", "workspace", pending.id)
    assert missing.value.code == "credential_missing"
    assert missing.value.message == "Connection credential is unavailable."

    store.close()
    wrong_key = AuthStore(tmp_path / "vault", Fernet.generate_key())
    with pytest.raises(EnergyError) as invalid_key:
        wrong_key.validate_encryption_key()
    assert invalid_key.value.code == "credential_unavailable"
    wrong_key.close()

    store = AuthStore(tmp_path / "vault", key)
    reopened_pending, revision = store.managed_snapshot("owner", "workspace", pending.id)
    assert reopened_pending.auth.scheme == "none"
    assert reopened_pending.auth.secret_id is None
    assert reopened_pending.state == "pending_mapping"
    assert revision == first_revision + 1
    site = _site()

    with pytest.raises(EnergyError) as wrong_user:
        store.activate_managed(
            "intruder",
            "workspace",
            pending.id,
            site=_site(user_id="intruder"),
            expected_version=revision,
            verified_at=datetime.now(UTC),
        )
    assert wrong_user.value.code == "account_forbidden"
    with pytest.raises(EnergyError) as wrong_workspace:
        store.activate_managed(
            "owner",
            "another-workspace",
            pending.id,
            site=site,
            expected_version=revision,
            verified_at=datetime.now(UTC),
        )
    assert wrong_workspace.value.code == "account_forbidden"
    with pytest.raises(EnergyError) as wrong_site:
        store.activate_managed(
            "owner",
            "workspace",
            pending.id,
            site=_site(user_id="intruder"),
            expected_version=revision,
            verified_at=datetime.now(UTC),
        )
    assert wrong_site.value.code == "account_forbidden"
    with pytest.raises(EnergyError) as wrong_revision:
        store.activate_managed(
            "owner",
            "workspace",
            pending.id,
            site=site,
            expected_version=revision + 1,
            verified_at=datetime.now(UTC),
        )
    assert wrong_revision.value.code == "connection_changed"
    assert store.managed_snapshot("owner", "workspace", pending.id)[1] == revision

    activated = store.activate_managed(
        "owner",
        "workspace",
        pending.id,
        site=site,
        expected_version=revision,
        verified_at=datetime.now(UTC),
    )
    assert activated.state == "active"
    assert activated.enabled
    assert activated.site_id == site.id
    assert activated.auth.secret_id is None
    assert activated.auth.credential_env is None
    assert activated.public()["auth_scheme"] == "none"
    assert "secret_id" not in activated.public()
    assert store.workspace_accounts("owner", "workspace") == [activated]

    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="no-auth-toolkit",
            name="No-auth toolkit",
            description="Managed account without a credential",
            runtime="native",
            status="stable",
            auth_required=True,
        )
    )
    scope = ToolAccountScope(workspace_id="workspace", user_id="owner", account_id=pending.id)
    handler_calls: list[tuple[str | None, str | None]] = []

    async def execute(_arguments: dict[str, Any], context: ExecutionContext) -> EnergyResult:
        handler_calls.append(
            (
                context.account.id if context.account is not None else None,
                context.credential,
            )
        )
        return EnergyResult(
            data={"account_id": context.account.id if context.account else None},
            kind=DataKind.CALCULATED,
            unit="unitless",
            source="fixture",
            site_id=context.site_id,
        )

    registry.add(
        Tool(
            name="no-auth-toolkit.read",
            toolkit="no-auth-toolkit",
            resource_scope="account",
            account_scope=scope,
            description="Read from a scoped no-auth account",
            input_schema=schema({}, []),
            capabilities=[],
            actions={Action.READ},
        ),
        execute,
    )
    agent = EnergyAgent(
        registry,
        tmp_path / "agent",
        accounts=[activated],
        sites=[site],
        auth_store=store,
    )
    agent.workspace_authorizer = _authorize_workspace
    session = agent.session(
        "owner",
        site.id,
        access_mode="hosted",
        workspace_id="workspace",
        resource_owner_id="owner",
        workspace_key_id="owner-key",
        workspace_policy_revision=4,
    )
    try:
        result = await agent.execute(session, "no-auth-toolkit.read", {})
        assert result["ok"]
        assert handler_calls == [(pending.id, None)]
        assert result["result"]["data"]["account_id"] == pending.id

        disabled = store.disable("owner", pending.id, site.id)
        assert disabled.state == "disabled" and not disabled.enabled
        with pytest.raises(EnergyError) as disabled_error:
            store.credential("owner", pending.id, site.id)
        assert disabled_error.value.code == "connection_disabled"

        revoked = store.revoke("owner", pending.id, site.id)
        assert revoked.state == "revoked" and not revoked.enabled
        assert revoked.auth.secret_id is None
        with pytest.raises(EnergyError) as revoked_error:
            store.credential("owner", pending.id, site.id)
        assert revoked_error.value.code == "connection_disabled"
    finally:
        await agent.close()


def _authorize_workspace(session: Session) -> None:
    assert (
        session.user_id,
        session.resource_user_id,
        session.workspace_id,
        session.workspace_key_id,
        session.workspace_policy_revision,
    ) == ("owner", "owner", "workspace", "owner-key", 4)


@pytest.mark.parametrize("scheme", ["api-key", "bearer", "basic", "oauth", "mcp", "local"])
def test_missing_non_none_managed_credentials_are_rejected_without_mutation(
    tmp_path: Path, scheme: str
) -> None:
    store = AuthStore(tmp_path / scheme, Fernet.generate_key())
    account = _pending_account(f"missing-{scheme}", scheme=scheme)
    with pytest.raises(ValueError, match="credential is required"):
        store.stage_managed(account, None)
    with pytest.raises(EnergyError) as missing:
        store.managed_snapshot("owner", "workspace", account.id)
    assert missing.value.code == "connection_not_found"
    assert store._db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    store.close()


def test_auth_none_still_rejects_empty_string_credential(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "empty", Fernet.generate_key())
    with pytest.raises(ValueError, match="non-empty string"):
        store.stage_managed(_pending_account(), "")
    assert store._db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    store.close()
