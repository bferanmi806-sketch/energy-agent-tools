from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from energy_agent_tools.control_contracts import AgentKeyAccess, LegacyKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import _SCHEMA_V1, ControlStore
from energy_agent_tools.models import Asset, EnergyError, Site


def _site(site_id: str, user_id: str, name: str = "Home") -> Site:
    return Site(id=site_id, user_id=user_id, name=name, timezone="Europe/London")


def _asset(asset_id: str, site_id: str, name: str = "Meter") -> Asset:
    return Asset(id=asset_id, site_id=site_id, kind="meter", name=name)


def _assert_not_found(callable_: Callable[[], object]) -> None:
    with pytest.raises(EnergyError) as caught:
        callable_()
    assert caught.value.code == "not_found"
    assert "alice" not in str(caught.value)
    assert "bob" not in str(caught.value)


def test_sqlite_persistence_and_raw_key_is_not_stored(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    user = store.create_user("alice", "Alice")
    workspace = store.create_workspace(user.id, "Home energy")
    site = _site("home", user.id)
    asset = _asset("main-meter", site.id)
    store.put_site(user.id, workspace.id, site)
    store.put_asset(user.id, workspace.id, asset)
    issued = store.create_key(user.id, workspace.id, "CLI", access=ManageKeyAccess())

    decoded_secret = base64.urlsafe_b64decode(issued.token[4:] + "=")
    assert len(decoded_secret) >= 32
    identity = store.authenticate(issued.token)
    assert identity is not None
    assert identity.key_id == issued.key.id
    assert issued.token not in repr(issued)
    assert "token_hash" not in issued.key.model_dump()
    assert issued.key.created_at.tzinfo is not None
    assert issued.key.created_at.utcoffset() == timedelta(0)
    assert issued.key.expires_at is None

    database_bytes = (tmp_path / "control.sqlite3").read_bytes()
    assert issued.token.encode("ascii") not in database_bytes
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        stored_hash = db.execute("SELECT token_hash FROM api_keys").fetchone()[0]
        assert stored_hash == hashlib.sha256(issued.token.encode("ascii")).hexdigest()

    store.close()
    reopened = ControlStore(tmp_path)
    assert reopened.workspaces(user.id) == [workspace]
    assert reopened.sites(user.id, workspace.id) == [site]
    assert reopened.assets(user.id, workspace.id) == [asset]
    assert reopened.keys(user.id, workspace.id) == [issued.key]
    identity = reopened.authenticate(issued.token)
    assert identity is not None
    assert identity.key_id == issued.key.id
    reopened.close()


def test_owner_scope_hides_foreign_reads_and_refuses_colliding_ids(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    store.create_user("bob", "Bob")
    alice_workspace = store.create_workspace("alice", "Alice home")
    bob_workspace = store.create_workspace("bob", "Bob home")
    alice_site = _site("shared-site-id", "alice", "Alice site")
    bob_site = _site("bob-site", "bob", "Bob site")
    store.put_site("alice", alice_workspace.id, alice_site)
    store.put_site("bob", bob_workspace.id, bob_site)
    alice_asset = _asset("shared-asset-id", alice_site.id, "Alice meter")
    store.put_asset("alice", alice_workspace.id, alice_asset)
    alice_key = store.create_key("alice", alice_workspace.id, "Alice key", access=ManageKeyAccess())

    _assert_not_found(lambda: store.create_workspace("mallory", "Unknown"))
    _assert_not_found(lambda: store.workspaces("mallory"))
    _assert_not_found(
        lambda: store.create_key("bob", alice_workspace.id, "Foreign", access=ManageKeyAccess())
    )
    _assert_not_found(lambda: store.keys("bob", alice_workspace.id))
    _assert_not_found(lambda: store.revoke_key("bob", bob_workspace.id, alice_key.key.id))
    _assert_not_found(lambda: store.sites("bob", alice_workspace.id))
    _assert_not_found(lambda: store.assets("bob", alice_workspace.id))
    _assert_not_found(lambda: store.put_site("bob", bob_workspace.id, alice_site))
    _assert_not_found(
        lambda: store.put_site("bob", bob_workspace.id, _site(alice_site.id, "bob", "Takeover"))
    )
    _assert_not_found(
        lambda: store.put_asset("bob", bob_workspace.id, _asset(alice_asset.id, bob_site.id))
    )
    _assert_not_found(
        lambda: store.put_asset(
            "bob", bob_workspace.id, _asset("foreign-site-asset", alice_site.id)
        )
    )

    assert store.sites("alice", alice_workspace.id) == [alice_site]
    assert store.assets("alice", alice_workspace.id) == [alice_asset]
    assert store.authenticate(alice_key.token) is not None
    assert store.keys("bob", bob_workspace.id) == []
    store.close()


def test_assets_are_bound_to_sites_and_parents_in_the_same_scope(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    workspace = store.create_workspace("alice", "Home")
    first_site = _site("first-site", "alice")
    second_site = _site("second-site", "alice", "Other")
    store.put_site("alice", workspace.id, first_site)
    store.put_site("alice", workspace.id, second_site)
    _assert_not_found(lambda: store.put_asset("alice", workspace.id, _asset("orphan", "missing")))

    parent = _asset("parent", first_site.id)
    child = Asset(
        id="child",
        site_id=first_site.id,
        kind="meter",
        name="Submeter",
        parent_id=parent.id,
    )
    store.put_asset("alice", workspace.id, parent)
    store.put_asset("alice", workspace.id, child)
    wrong_site_parent = Asset(
        id="wrong-child",
        site_id=second_site.id,
        kind="meter",
        name="Wrong parent",
        parent_id=parent.id,
    )
    _assert_not_found(lambda: store.put_asset("alice", workspace.id, wrong_site_parent))
    assert store.assets("alice", workspace.id) == [child, parent]
    store.close()


def test_expiration_and_revocation_are_visible_across_store_instances(tmp_path: Path) -> None:
    first = ControlStore(tmp_path)
    first.create_user("alice", "Alice")
    workspace = first.create_workspace("alice", "Home")
    second = ControlStore(tmp_path)
    expiring = first.create_key(
        "alice",
        workspace.id,
        "Expiring",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        access=ManageKeyAccess(),
    )
    assert second.authenticate(expiring.token) is not None

    first.revoke_key("alice", workspace.id, expiring.key.id)
    assert second.authenticate(expiring.token) is None
    assert first.keys("alice", workspace.id)[0].revoked is True

    expired = first.create_key(
        "alice",
        workspace.id,
        "Expired",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        access=ManageKeyAccess(),
    )
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            "UPDATE api_keys SET expires_at = ? WHERE id = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds"),
                expired.key.id,
            ),
        )
    assert second.authenticate(expired.token) is None
    assert second.authenticate("eat_invalid") is None
    assert second.authenticate("eat_" + "a" * 5000) is None
    with pytest.raises(EnergyError) as caught:
        first.create_key(
            "alice",
            workspace.id,
            "Naive",
            expires_at=datetime(2026, 1, 1),
            access=ManageKeyAccess(),
        )
    assert caught.value.code == "invalid_request"
    first.close()
    second.close()


def test_newer_database_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 3")

    with pytest.raises(RuntimeError, match="newer"):
        ControlStore(tmp_path)

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def _make_v1_store(root: Path) -> dict[str, str]:
    path = root / "control.sqlite3"
    tokens = {
        "active": "eat_" + "A" * 43,
        "revoked": "eat_" + "B" * 43,
        "expired": "eat_" + "C" * 43,
    }
    created_at = datetime(2026, 9, 1, tzinfo=UTC).isoformat(timespec="microseconds")
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="microseconds")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA_V1:
            db.execute(statement)
        db.executemany(
            "INSERT INTO users(id, name) VALUES (?, ?)",
            [("alice", "Alice"), ("bob", "Bob")],
        )
        db.executemany(
            "INSERT INTO workspaces(id, user_id, name) VALUES (?, ?, ?)",
            [("alice-home", "alice", "Alice home"), ("bob-home", "bob", "Bob home")],
        )
        rows = [
            (
                "active-key",
                "alice",
                "alice-home",
                "Active",
                hashlib.sha256(tokens["active"].encode("ascii")).hexdigest(),
                tokens["active"][:12],
                created_at,
                None,
                0,
            ),
            (
                "revoked-key",
                "alice",
                "alice-home",
                "Revoked",
                hashlib.sha256(tokens["revoked"].encode("ascii")).hexdigest(),
                tokens["revoked"][:12],
                created_at,
                None,
                1,
            ),
            (
                "expired-key",
                "bob",
                "bob-home",
                "Expired",
                hashlib.sha256(tokens["expired"].encode("ascii")).hexdigest(),
                tokens["expired"][:12],
                created_at,
                expired_at,
                0,
            ),
        ]
        db.executemany(
            """INSERT INTO api_keys(
                   id, user_id, workspace_id, name, token_hash, token_prefix,
                   created_at, expires_at, revoked
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        db.execute("PRAGMA user_version = 1")
    return tokens


def test_genuine_v1_migration_preserves_keys_and_marks_them_legacy(tmp_path: Path) -> None:
    tokens = _make_v1_store(tmp_path)
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as db:
        original_rows = db.execute("SELECT * FROM api_keys ORDER BY id").fetchall()

    store = ControlStore(tmp_path)
    assert store.authenticate(tokens["active"]) is not None
    active_identity = store.authenticate(tokens["active"])
    assert active_identity is not None
    assert active_identity.access == LegacyKeyAccess()
    assert store.authenticate(tokens["revoked"]) is None
    assert store.authenticate(tokens["expired"]) is None
    alice_keys = store.keys("alice", "alice-home")
    bob_keys = store.keys("bob", "bob-home")
    assert [key.id for key in alice_keys] == ["active-key", "revoked-key"]
    assert [key.id for key in bob_keys] == ["expired-key"]
    assert all(key.access == LegacyKeyAccess() for key in alice_keys + bob_keys)

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT * FROM api_keys ORDER BY id").fetchall() == [
            (*row, '{"kind":"legacy-agent"}') for row in original_rows
        ]
    store.close()


def test_concurrent_v1_store_initialization_converges_on_schema_v2(tmp_path: Path) -> None:
    tokens = _make_v1_store(tmp_path)

    def open_and_close() -> str | None:
        store = ControlStore(tmp_path)
        identity = store.authenticate(tokens["active"])
        store.close()
        return identity.access.kind if identity is not None else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: open_and_close(), range(8)))

    assert results == ["legacy-agent"] * 8
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA table_info(api_keys)").fetchall()[-1][1] == "access_json"


def test_access_grants_persist_and_agent_sites_are_owner_workspace_scoped(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    store.create_user("bob", "Bob")
    alice_home = store.create_workspace("alice", "Alice home")
    alice_other = store.create_workspace("alice", "Alice other")
    bob_home = store.create_workspace("bob", "Bob home")
    alice_site = _site("alice-home-site", "alice")
    other_alice_site = _site("alice-other-site", "alice", "Other")
    bob_site = _site("bob-site", "bob")
    store.put_site("alice", alice_home.id, alice_site)
    store.put_site("alice", alice_other.id, other_alice_site)
    store.put_site("bob", bob_home.id, bob_site)

    agent_access = AgentKeyAccess(site_ids=[alice_site.id])
    agent = store.create_key("alice", alice_home.id, "Agent", access=agent_access)
    management = store.create_key("alice", alice_home.id, "Manage", access=ManageKeyAccess())
    assert agent.key.access == agent_access
    assert management.key.access == ManageKeyAccess()
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        serialized_access = db.execute(
            "SELECT access_json FROM api_keys WHERE id = ?", (agent.key.id,)
        ).fetchone()[0]
    assert json.loads(serialized_access) == {"kind": "agent", "site_ids": [alice_site.id]}
    assert serialized_access == '{"kind":"agent","site_ids":["alice-home-site"]}'
    with pytest.raises(EnergyError) as caught:
        store.create_key(
            "alice",
            alice_home.id,
            "Foreign site grant",
            access=AgentKeyAccess(site_ids=[other_alice_site.id]),
        )
    assert caught.value.code == "not_found"
    with pytest.raises(EnergyError) as caught:
        store.create_key(
            "alice",
            alice_home.id,
            "Foreign owner grant",
            access=AgentKeyAccess(site_ids=[bob_site.id]),
        )
    assert caught.value.code == "not_found"
    assert len(store.keys("alice", alice_home.id)) == 2
    store.close()

    reopened = ControlStore(tmp_path)
    identity = reopened.authenticate(agent.token)
    assert identity is not None
    assert identity.access == agent_access
    assert {key.id: key.access for key in reopened.keys("alice", alice_home.id)} == {
        agent.key.id: agent_access,
        management.key.id: ManageKeyAccess(),
    }
    reopened.close()


def test_invalid_or_legacy_access_cannot_be_issued(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    workspace = store.create_workspace("alice", "Home")

    for access in (
        LegacyKeyAccess(),
        {"kind": "manage"},
        AgentKeyAccess.model_construct(kind="agent", site_ids=[]),
    ):
        with pytest.raises(EnergyError) as caught:
            store.create_key("alice", workspace.id, "Invalid", access=access)
        assert caught.value.code == "invalid_request"
    with pytest.raises(TypeError):
        store.create_key("alice", workspace.id, "Missing access")  # type: ignore[call-arg]
    manage_key = store.create_key("alice", workspace.id, "Manage", access=ManageKeyAccess())
    assert manage_key.key.access == ManageKeyAccess()
    assert store.keys("alice", workspace.id) == [manage_key.key]
    store.close()


def test_corrupt_access_fails_closed_without_exposing_stored_json(tmp_path: Path) -> None:
    store = ControlStore(tmp_path)
    store.create_user("alice", "Alice")
    workspace = store.create_workspace("alice", "Home")
    issued = store.create_key("alice", workspace.id, "Manage", access=ManageKeyAccess())
    marker = "private-corruption-payload"
    with sqlite3.connect(tmp_path / "control.sqlite3") as db:
        db.execute(
            "UPDATE api_keys SET access_json = ? WHERE id = ?",
            (json.dumps({"kind": "administrator", "secret": marker}), issued.key.id),
        )

    assert store.authenticate(issued.token) is None
    with pytest.raises(RuntimeError) as caught:
        store.keys("alice", workspace.id)
    assert "invalid key access" in str(caught.value)
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
    store.close()
