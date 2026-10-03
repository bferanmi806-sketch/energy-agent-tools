from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from energy_agent_tools.control_store import ControlStore
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
    issued = store.create_key(user.id, workspace.id, "CLI")

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
    alice_key = store.create_key("alice", alice_workspace.id, "Alice key")

    _assert_not_found(lambda: store.create_workspace("mallory", "Unknown"))
    _assert_not_found(lambda: store.workspaces("mallory"))
    _assert_not_found(lambda: store.create_key("bob", alice_workspace.id, "Foreign"))
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
        "alice", workspace.id, "Expiring", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert second.authenticate(expiring.token) is not None

    first.revoke_key("alice", workspace.id, expiring.key.id)
    assert second.authenticate(expiring.token) is None
    assert first.keys("alice", workspace.id)[0].revoked is True

    expired = first.create_key(
        "alice", workspace.id, "Expired", expires_at=datetime.now(UTC) + timedelta(hours=1)
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
        first.create_key("alice", workspace.id, "Naive", expires_at=datetime(2026, 1, 1))
    assert caught.value.code == "invalid_request"
    first.close()
    second.close()


def test_newer_database_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="newer"):
        ControlStore(tmp_path)

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
