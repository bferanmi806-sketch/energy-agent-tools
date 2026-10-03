from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from energy_agent_tools.control_contracts import AgentKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.models import EnergyError, Site
from energy_agent_tools.workspace_access import WorkspaceKeyScope, WorkspaceMemberGrants


def _site(site_id: str, user_id: str, name: str = "Home") -> Site:
    return Site(id=site_id, user_id=user_id, name=name, timezone="Europe/London")


def _assert_not_found(callable_: Callable[[], object]) -> None:
    with pytest.raises(EnergyError) as caught:
        callable_()
    assert caught.value.code == "not_found"
    assert "alice" not in str(caught.value)
    assert "bob" not in str(caught.value)


def _seed_store(root: Path) -> tuple[ControlStore, str, str, Site, Site, Site]:
    store = ControlStore(root)
    store.create_user("alice", "Alice")
    store.create_user("bob", "Bob")
    store.create_user("carol", "Carol")
    workspace = store.create_workspace("alice", "Shared home", mode="managed")
    other_workspace = store.create_workspace("alice", "Other home", mode="managed")
    bob_workspace = store.create_workspace("bob", "Bob home", mode="managed")
    site = _site("alice-site", "alice")
    other_site = _site("alice-other-site", "alice", "Other")
    bob_site = _site("bob-site", "bob", "Bob")
    store.put_site("alice", workspace.id, site)
    store.put_site("alice", other_workspace.id, other_site)
    store.put_site("bob", bob_workspace.id, bob_site)
    return store, workspace.id, other_workspace.id, site, other_site, bob_site


def test_member_scope_uses_current_intersection_and_policy_revision(tmp_path: Path) -> None:
    store, workspace_id, _, site, _, _ = _seed_store(tmp_path)
    member = store.add_member("alice", workspace_id, "bob")
    assert member.name == "Bob"
    assert member.grants == WorkspaceMemberGrants()
    assert store.members("alice", workspace_id) == [member]

    with pytest.raises(EnergyError) as caught:
        store.create_member_key(
            "alice",
            workspace_id,
            "bob",
            "Too early",
            access=AgentKeyAccess(site_ids=[site.id]),
        )
    assert caught.value.code == "not_found"

    grants = WorkspaceMemberGrants(site_ids=[site.id], connection_ids=["connection-a"])
    changed = store.set_member_grants("alice", workspace_id, "bob", grants)
    assert changed.grants == grants
    key = store.create_member_key(
        "alice", workspace_id, "bob", "Bob agent", access=AgentKeyAccess(site_ids=[site.id])
    )
    identity = store.authenticate(key.token)
    assert identity is not None
    assert identity.scope == WorkspaceKeyScope(
        actor_user_id="bob",
        resource_owner_id="alice",
        workspace_id=workspace_id,
        key_id=key.key.id,
        revision=2,
        site_ids=[site.id],
        connection_ids=["connection-a"],
    )
    assert store.key_scope(key.key.id) == identity.scope

    same = store.set_member_grants("alice", workspace_id, "bob", grants)
    assert same.grants == grants
    assert store.key_scope(key.key.id) == identity.scope

    narrower = WorkspaceMemberGrants(site_ids=[], connection_ids=[])
    store.set_member_grants("alice", workspace_id, "bob", narrower)
    assert store.authenticate(key.token) is not None
    current_scope = store.key_scope(key.key.id)
    assert current_scope is not None
    assert current_scope.revision == 3
    assert current_scope.site_ids == []
    assert current_scope.connection_ids == []
    store.close()


def test_owner_and_member_keys_have_distinct_scope_and_workspace_sites(tmp_path: Path) -> None:
    store, workspace_id, _, site, other_site, _ = _seed_store(tmp_path)
    store.add_member("alice", workspace_id, "bob")
    store.set_member_grants(
        "alice",
        workspace_id,
        "bob",
        WorkspaceMemberGrants(site_ids=[site.id], connection_ids=[]),
    )
    member_key = store.create_member_key(
        "alice", workspace_id, "bob", "Bob", access=AgentKeyAccess(site_ids=[site.id])
    )
    owner_manage = store.create_key("alice", workspace_id, "Alice manage", access=ManageKeyAccess())
    owner_agent = store.create_key(
        "alice", workspace_id, "Alice agent", access=AgentKeyAccess(site_ids=[site.id])
    )

    member_scope = store.key_scope(member_key.key.id)
    owner_scope = store.key_scope(owner_manage.key.id)
    agent_scope = store.key_scope(owner_agent.key.id)
    assert member_scope is not None
    assert member_scope.actor_user_id == "bob"
    assert member_scope.resource_owner_id == "alice"
    assert member_scope.site_ids == [site.id]
    assert member_scope.connection_ids == []
    assert owner_scope is not None
    assert owner_scope.site_ids == [site.id]
    assert owner_scope.connection_ids is None
    assert agent_scope is not None
    assert agent_scope.site_ids == [site.id]
    assert agent_scope.connection_ids is None
    assert other_site.id not in owner_scope.site_ids

    owner_keys = store.keys("alice", workspace_id)
    assert {key.id for key in owner_keys} == {
        member_key.key.id,
        owner_manage.key.id,
        owner_agent.key.id,
    }
    assert next(key for key in owner_keys if key.id == member_key.key.id).user_id == "bob"
    store.close()


def test_foreign_sites_operator_workspaces_and_nonowners_are_denied(tmp_path: Path) -> None:
    store, workspace_id, other_workspace_id, site, other_site, bob_site = _seed_store(tmp_path)
    operator = store.create_workspace("alice", "Operator")
    store.add_member("alice", workspace_id, "bob")

    with pytest.raises(EnergyError) as caught:
        store.add_member("alice", workspace_id, "alice")
    assert caught.value.code == "invalid_request"
    with pytest.raises(EnergyError) as caught:
        store.add_member("alice", workspace_id, "bob")
    assert caught.value.code == "conflict"
    _assert_not_found(lambda: store.add_member("alice", workspace_id, "missing-user"))

    for grants in (
        WorkspaceMemberGrants(site_ids=[other_site.id]),
        WorkspaceMemberGrants(site_ids=[bob_site.id]),
    ):
        with pytest.raises(EnergyError) as caught:
            store.set_member_grants("alice", workspace_id, "bob", grants)
        assert caught.value.code == "not_found"

    for action in (
        lambda: store.add_member("bob", workspace_id, "carol"),
        lambda: store.members("bob", workspace_id),
        lambda: store.set_member_grants("bob", workspace_id, "carol", WorkspaceMemberGrants()),
        lambda: store.remove_member("bob", workspace_id, "carol"),
        lambda: store.add_member("alice", operator.id, "carol"),
        lambda: store.members("alice", operator.id),
        lambda: store.create_member_key(
            "alice",
            operator.id,
            "bob",
            "No operator sharing",
            access=AgentKeyAccess(site_ids=[site.id]),
        ),
    ):
        _assert_not_found(action)

    with pytest.raises(EnergyError) as caught:
        store.create_member_key(
            "alice",
            workspace_id,
            "bob",
            "Foreign site",
            access=AgentKeyAccess(site_ids=[other_site.id]),
        )
    assert caught.value.code == "not_found"
    assert store.workspace("alice", other_workspace_id).mode == "managed"
    operator_key = store.create_key("alice", operator.id, "Operator", access=ManageKeyAccess())
    operator_identity = store.authenticate(operator_key.token)
    assert operator_identity is not None
    assert operator_identity.scope is None
    assert store.key_scope(operator_key.key.id) is None
    store.close()


def test_remove_is_idempotent_and_reenrollment_never_revives_keys(tmp_path: Path) -> None:
    store, workspace_id, _, site, _, _ = _seed_store(tmp_path)
    store.add_member("alice", workspace_id, "bob")
    store.set_member_grants(
        "alice",
        workspace_id,
        "bob",
        WorkspaceMemberGrants(site_ids=[site.id], connection_ids=["connection-a"]),
    )
    old_key = store.create_member_key(
        "alice", workspace_id, "bob", "Old", access=AgentKeyAccess(site_ids=[site.id])
    )
    before_remove = store.key_scope(old_key.key.id)
    assert before_remove is not None and before_remove.revision == 2

    store.revoke_key("alice", workspace_id, old_key.key.id)
    assert store.authenticate(old_key.token) is None
    store.remove_member("alice", workspace_id, "bob")
    assert store.key_scope(old_key.key.id) is None
    assert store.keys("alice", workspace_id)[0].revoked is True
    store.remove_member("alice", workspace_id, "bob")

    rejoined = store.add_member("alice", workspace_id, "bob")
    assert rejoined.grants == WorkspaceMemberGrants()
    assert store.authenticate(old_key.token) is None
    assert store.key_scope(old_key.key.id) is None
    assert store.keys("alice", workspace_id)[0].revoked is True
    assert store.key_scope(old_key.key.id) is None
    store.close()


def test_expired_and_corrupt_member_keys_fail_closed(tmp_path: Path) -> None:
    store, workspace_id, _, site, _, _ = _seed_store(tmp_path)
    store.add_member("alice", workspace_id, "bob")
    store.set_member_grants(
        "alice",
        workspace_id,
        "bob",
        WorkspaceMemberGrants(site_ids=[site.id], connection_ids=["connection-a"]),
    )
    expiring = store.create_member_key(
        "alice",
        workspace_id,
        "bob",
        "Expiring",
        access=AgentKeyAccess(site_ids=[site.id]),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            "UPDATE api_keys SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expiring.key.id),
        )
    assert store.authenticate(expiring.token) is None
    assert store.key_scope(expiring.key.id) is None

    active = store.create_member_key(
        "alice", workspace_id, "bob", "Active", access=AgentKeyAccess(site_ids=[site.id])
    )
    marker = "private-corruption-payload"
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            "UPDATE workspace_members SET site_ids_json = ? WHERE workspace_id = ? AND user_id = ?",
            (json.dumps([marker]), workspace_id, "bob"),
        )
    assert store.authenticate(active.token) is None
    assert store.key_scope(active.key.id) is None
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            """UPDATE workspace_members
               SET site_ids_json = ?, connection_ids_json = ?
               WHERE workspace_id = ? AND user_id = ?""",
            (json.dumps([site.id]), json.dumps(["connection-a"]), workspace_id, "bob"),
        )

    store.set_member_grants(
        "alice",
        workspace_id,
        "bob",
        WorkspaceMemberGrants(site_ids=[site.id], connection_ids=["connection-a"]),
    )
    malformed = store.create_member_key(
        "alice", workspace_id, "bob", "Malformed", access=AgentKeyAccess(site_ids=[site.id])
    )
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            "UPDATE api_keys SET access_json = ? WHERE id = ?",
            ('{"kind":"administrator"}', malformed.key.id),
        )
    assert store.authenticate(malformed.token) is None
    assert store.key_scope(malformed.key.id) is None
    store.close()


def test_invalid_constructed_grants_are_revalidated(tmp_path: Path) -> None:
    store, workspace_id, _, site, _, _ = _seed_store(tmp_path)
    store.add_member("alice", workspace_id, "bob")
    invalid = WorkspaceMemberGrants.model_construct(site_ids=[site.id, site.id], connection_ids=[])
    with pytest.raises(EnergyError) as caught:
        store.set_member_grants("alice", workspace_id, "bob", invalid)
    assert caught.value.code == "invalid_request"
    store.close()


def test_member_limit_is_enforced(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    workspace = store.create_workspace("alice", "Shared", mode="managed")
    for index in range(257):
        user_id = f"member-{index}"
        store.create_user(user_id, user_id)
        if index < 256:
            store.add_member("alice", workspace.id, user_id)
    with pytest.raises(EnergyError) as caught:
        store.add_member("alice", workspace.id, "member-256")
    assert caught.value.code == "conflict"
    assert len(store.members("alice", workspace.id)) == 256
    store.close()
