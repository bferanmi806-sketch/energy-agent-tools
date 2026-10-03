from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import (
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    Session,
)
from energy_agent_tools.workbench import Workbench
from energy_agent_tools.workspace_access import WorkspaceMemberGrants


def test_backup_restore_keeps_member_keys_grants_and_actor_artifacts(tmp_path):
    state = tmp_path / "state"
    control = ControlStore(state / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    control.create_user("member", "Member")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    control.add_member(owner.user.id, owner.workspace.id, "member")
    control.set_member_grants(
        owner.user.id,
        owner.workspace.id,
        "member",
        WorkspaceMemberGrants(site_ids=[site.id], connection_ids=["shared"]),
    )
    key = control.create_member_key(
        owner.user.id,
        owner.workspace.id,
        "member",
        "Member agent",
        access=AgentKeyAccess(site_ids=[site.id]),
    )
    vault_key = Fernet.generate_key()
    vault = AuthStore(state / "vault", vault_key)
    vault.stage_managed(
        ConnectedAccount(
            id="shared",
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            toolkit="home-assistant",
            state="pending_mapping",
            enabled=False,
            last_verified_at=datetime.now(UTC),
            auth=AuthConfig(scheme="bearer"),
        ),
        "private-restore-fixture-credential",
    )
    _, version = vault.managed_snapshot(owner.user.id, owner.workspace.id, "shared")
    vault.activate_managed(
        owner.user.id,
        owner.workspace.id,
        "shared",
        site=site,
        expected_version=version,
        verified_at=datetime.now(UTC),
    )
    workbench = Workbench(state)
    session = Session(user_id="member")
    artifact = workbench.persist(
        session,
        EnergyResult(data={"value": 2}, kind=DataKind.METERED, unit="kWh", source="fixture"),
    )
    identity = control.authenticate(key.token)
    assert identity is not None and identity.scope is not None
    revision = identity.scope.revision
    control.close()
    vault.close()
    archive = tmp_path / "backup.tar.gz"
    manifest = create_backup(state, archive)
    assert next(item for item in manifest.files if item.kind == "control").schema == "control.v4"
    assert key.token.encode() not in archive.read_bytes()
    restored_state = tmp_path / "restored"
    restore_backup(archive, restored_state)
    restored = ControlStore(restored_state / "control")
    restored_vault = AuthStore(restored_state / "vault", vault_key)
    restored_workbench = Workbench(restored_state)
    try:
        identity = restored.authenticate(key.token)
        assert identity is not None and identity.user_id == "member"
        assert identity.scope is not None and identity.scope.revision == revision
        assert identity.scope.resource_owner_id == owner.user.id
        assert identity.scope.connection_ids == ["shared"]
        assert identity.scope.site_ids == [site.id]
        assert (
            restored_vault.credential(owner.user.id, "shared", site.id)
            == "private-restore-fixture-credential"
        )
        assert restored_workbench.read(session, artifact["artifact_id"]).data == {"value": 2}
        with pytest.raises(EnergyError):
            restored_workbench.read(
                Session(user_id=owner.user.id, id=session.id), artifact["artifact_id"]
            )
        restored.remove_member(owner.user.id, owner.workspace.id, "member")
        assert restored.authenticate(key.token) is None
    finally:
        restored.close()
        restored_vault.close()
