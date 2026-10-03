from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore, OAuthProvider
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError, Site


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _managed_account(
    connection_id: str = "managed-octopus-1",
    *,
    user_id: str = "alice",
    workspace_id: str = "workspace-a",
    verified_at: datetime | None = None,
    scheme: str = "api-key",
) -> ConnectedAccount:
    return ConnectedAccount(
        id=connection_id,
        user_id=user_id,
        workspace_id=workspace_id,
        toolkit="octopus-energy-account",
        site_id=None,
        auth=AuthConfig(scheme=scheme),
        settings={"base_url": "https://provider.example"},
        enabled=False,
        state="pending_mapping",
        last_verified_at=verified_at or datetime(2026, 10, 3, 11, 59, tzinfo=UTC),
    )


def _site(site_id: str = "site-home", *, user_id: str = "alice") -> Site:
    return Site(id=site_id, user_id=user_id, name="Home", timezone="UTC")


def _open_store(tmp_path: Path, key: bytes, clock: FixedClock | None = None) -> AuthStore:
    return AuthStore(tmp_path, key, clock=clock)


def test_legacy_rows_migrate_without_promotion_and_reopen(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    legacy = ConnectedAccount(
        id="legacy-octopus",
        user_id="alice",
        toolkit="octopus-energy-account",
        site_id="home",
        auth=AuthConfig(scheme="bearer"),
        settings={"base_url": "https://provider.example"},
    )
    payload = legacy.model_dump(mode="json")
    payload.pop("workspace_id", None)
    payload.pop("state", None)
    payload.pop("last_verified_at", None)
    payload.pop("expires_at", None)
    private = Fernet(key).encrypt(b'{"credential":"legacy-token","refresh_token":null}')
    db = sqlite3.connect(tmp_path / "auth.sqlite3")
    db.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, toolkit TEXT NOT NULL, site_id TEXT,
            account_json TEXT NOT NULL, secret_blob BLOB NOT NULL, state TEXT NOT NULL,
            enabled INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            last_verified_at TEXT, expires_at TEXT, last_error TEXT
        );
        CREATE INDEX accounts_scope ON accounts(user_id, site_id, toolkit);
        CREATE TABLE oauth_transactions (
            state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, connection_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
            transaction_blob BLOB NOT NULL
        );
        CREATE INDEX oauth_expiry ON oauth_transactions(expires_at);
        """
    )
    db.execute(
        """INSERT INTO accounts(
            id,user_id,toolkit,site_id,account_json,secret_blob,state,enabled,created_at,
            updated_at,last_verified_at,expires_at,last_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            legacy.id,
            legacy.user_id,
            legacy.toolkit,
            legacy.site_id,
            json.dumps(payload),
            private,
            "active",
            1,
            "2026-10-03T11:00:00+00:00",
            "2026-10-03T11:00:00+00:00",
            None,
            None,
        ),
    )
    db.commit()
    db.close()

    store = _open_store(tmp_path, key)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 2
    migrated = store._db.execute(
        "SELECT workspace_id, managed_revision FROM accounts WHERE id = ?", (legacy.id,)
    ).fetchone()
    assert tuple(migrated) == (None, 0)
    assert store.workspace_accounts("alice", None)[0].workspace_id is None
    assert store.credential("alice", legacy.id, site_id="home") == "legacy-token"
    store.close()

    reopened = _open_store(tmp_path, key)
    assert reopened.workspace_accounts("alice", "workspace-a") == []
    assert reopened.credential("alice", legacy.id, site_id="home") == "legacy-token"
    reopened.close()


def test_stage_encrypts_pending_secret_and_scopes_queries_exactly(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    clock = FixedClock()
    store = _open_store(tmp_path, key, clock)
    store.configure(
        ConnectedAccount(
            id="legacy",
            user_id="alice",
            toolkit="legacy-toolkit",
            site_id="home",
            auth=AuthConfig(scheme="bearer"),
        ),
        "legacy-secret",
    )
    account = _managed_account()
    other_workspace = _managed_account("managed-octopus-2", workspace_id="workspace-b")
    staged = store.stage_managed(account, "pending-secret")
    store.stage_managed(other_workspace, "other-workspace-secret")
    assert staged.workspace_id == "workspace-a"
    assert staged.state == "pending_mapping"
    assert staged.site_id is None
    assert not staged.enabled
    assert staged.last_verified_at is not None

    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"pending-secret" not in raw_db
    assert "pending-secret" not in staged.model_dump_json()
    assert store.workspace_accounts("alice", "workspace-a") == [staged]
    assert [item.id for item in store.workspace_accounts("alice", None)] == ["legacy"]
    assert [item.id for item in store.workspace_accounts("alice", "workspace-b")] == [
        other_workspace.id
    ]
    assert store.pending_credential("alice", "workspace-a", account.id) == "pending-secret"

    with pytest.raises(EnergyError, match="outside this user/workspace scope"):
        store.pending_credential("alice", "workspace-b", account.id)
    with pytest.raises(EnergyError, match="outside this user/workspace scope"):
        store.managed_snapshot("mallory", "workspace-a", account.id)
    with pytest.raises(EnergyError, match="outside this user/workspace scope"):
        store.stage_managed(_managed_account(workspace_id="workspace-b"), "wrong-workspace")
    with pytest.raises(EnergyError, match="pending and unavailable to runtime"):
        store.credential("alice", account.id)
    store.close()


def test_revision_cas_and_activation_replay_are_clock_independent(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    clock = FixedClock()
    first = _open_store(tmp_path, key, clock)
    second = _open_store(tmp_path, key, clock)
    account = _managed_account()
    first.stage_managed(account, "credential-one")
    _, first_version = first.managed_snapshot("alice", "workspace-a", account.id)
    _, stale_version = second.managed_snapshot("alice", "workspace-a", account.id)
    assert first_version == stale_version == 1

    first.stage_managed(account, "credential-two", expected_version=first_version)
    with pytest.raises(EnergyError, match="changed while it was being updated"):
        second.stage_managed(account, "stale-credential", expected_version=stale_version)
    _, version = first.managed_snapshot("alice", "workspace-a", account.id)
    assert version == 2
    assert first.pending_credential("alice", "workspace-a", account.id) == "credential-two"
    with pytest.raises(EnergyError, match="changed while it was being mapped"):
        second.activate_managed(
            "alice",
            "workspace-a",
            account.id,
            site=_site(),
            expected_version=stale_version,
            verified_at=clock.value,
        )

    activated = first.activate_managed(
        "alice",
        "workspace-a",
        account.id,
        site=_site(),
        expected_version=version,
        verified_at=clock.value,
    )
    assert activated.state == "active"
    assert activated.enabled
    assert activated.site_id == "site-home"
    assert activated.last_verified_at == clock.value
    assert first.managed_snapshot("alice", "workspace-a", account.id)[1] == 3

    # A retry after a lost response is idempotent even with the pre-activation version.
    assert (
        first.activate_managed(
            "alice",
            "workspace-a",
            account.id,
            site=_site(),
            expected_version=version,
            verified_at=clock.value,
        )
        == activated
    )
    with pytest.raises(EnergyError, match="already mapped to another site"):
        first.activate_managed(
            "alice",
            "workspace-a",
            account.id,
            site=_site("site-other"),
            expected_version=version,
            verified_at=clock.value,
        )
    assert first.managed_snapshot("alice", "workspace-a", account.id)[1] == 3
    second.close()
    first.close()


def test_failed_stage_preserves_pending_secret_and_active_cannot_be_reset(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    clock = FixedClock()
    store = _open_store(tmp_path, key, clock)
    account = _managed_account()
    store.stage_managed(account, "preserved-secret")
    with pytest.raises(EnergyError, match="changed while it was being updated"):
        store.stage_managed(account, "replacement", expected_version=99)
    assert store.pending_credential("alice", "workspace-a", account.id) == "preserved-secret"

    _, version = store.managed_snapshot("alice", "workspace-a", account.id)
    wrong_owner = _site(user_id="mallory")
    with pytest.raises(EnergyError, match="outside this user/workspace scope"):
        store.activate_managed(
            "alice",
            "workspace-a",
            account.id,
            site=wrong_owner,
            expected_version=version,
            verified_at=clock.value,
        )
    assert store.managed_snapshot("alice", "workspace-a", account.id)[0].state == "pending_mapping"

    activated = store.activate_managed(
        "alice",
        "workspace-a",
        account.id,
        site=_site(),
        expected_version=version,
        verified_at=clock.value,
    )
    with pytest.raises(EnergyError, match="already mapped to another site"):
        store.activate_managed(
            "alice",
            "workspace-a",
            account.id,
            site=_site("site-other"),
            expected_version=version + 1,
            verified_at=clock.value,
        )
    _, active_version = store.managed_snapshot("alice", "workspace-a", account.id)
    with pytest.raises(EnergyError, match="active or mapped connection cannot be restaged"):
        store.stage_managed(account, "must-not-reset", expected_version=active_version)
    with pytest.raises(EnergyError, match="workspace-scoped lifecycle operations"):
        store.configure(activated, "must-not-overwrite")
    with pytest.raises(EnergyError, match="workspace-scoped lifecycle operations"):
        store.configure(_managed_account(), "must-not-create")
    with pytest.raises(EnergyError, match="workspace-scoped lifecycle operations"):
        store.configure(
            ConnectedAccount(
                id=account.id,
                user_id="alice",
                toolkit="octopus-energy-account",
                site_id="site-home",
                auth=AuthConfig(scheme="api-key"),
            ),
            "must-not-overwrite",
        )
    assert store.credential("alice", account.id, site_id="site-home") == "preserved-secret"
    store.close()


def test_revoked_record_can_only_be_restaged_with_exact_revision(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    clock = FixedClock()
    store = _open_store(tmp_path, key, clock)
    account = _managed_account()
    store.stage_managed(account, "first-secret")
    _, version = store.managed_snapshot("alice", "workspace-a", account.id)
    activated = store.activate_managed(
        "alice",
        "workspace-a",
        account.id,
        site=_site(),
        expected_version=version,
        verified_at=clock.value,
    )
    revoked = store.revoke("alice", account.id, site_id=activated.site_id)
    assert revoked.state == "revoked"
    _, revoked_version = store.managed_snapshot("alice", "workspace-a", account.id)
    with pytest.raises(EnergyError, match="changed while it was being updated"):
        store.stage_managed(account, "stale-restage", expected_version=version + 1)
    assert store.managed_snapshot("alice", "workspace-a", account.id)[0].state == "revoked"

    restaged = store.stage_managed(account, "new-verified-secret", expected_version=revoked_version)
    assert restaged.state == "pending_mapping"
    assert restaged.site_id is None
    assert not restaged.enabled
    assert store.pending_credential("alice", "workspace-a", account.id) == "new-verified-secret"
    with pytest.raises(EnergyError, match="pending and unavailable to runtime"):
        store.credential("alice", account.id)
    store.close()


def test_schema_and_scope_corruption_fail_closed(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    store = _open_store(tmp_path, key)
    account = _managed_account()
    store.stage_managed(account, "secret")
    store._db.execute(
        "UPDATE accounts SET workspace_id = ? WHERE id = ?", ("workspace-corrupt", account.id)
    )
    store._db.commit()
    with pytest.raises(EnergyError, match="Stored connection scope is invalid"):
        store.managed_snapshot("alice", "workspace-corrupt", account.id)
    store.close()

    db = sqlite3.connect(tmp_path / "auth.sqlite3")
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="newer than this version supports"):
        _open_store(tmp_path, key)


def test_incomplete_versioned_schema_is_rejected(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    store = _open_store(tmp_path, key)
    store.close()
    db = sqlite3.connect(tmp_path / "auth.sqlite3")
    db.execute("DROP INDEX accounts_workspace_scope")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="corrupt or unsupported"):
        _open_store(tmp_path, key)


def test_encryption_key_validation_checks_account_and_oauth_blobs(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    store = _open_store(tmp_path, key)
    local = ConnectedAccount(
        id="legacy-auth",
        user_id="alice",
        toolkit="test-toolkit",
        site_id="home",
        auth=AuthConfig(scheme="bearer"),
    )
    store.configure(local, "account-secret")
    store._db.execute(
        """INSERT INTO oauth_transactions(
            state_hash,user_id,connection_id,redirect_uri,expires_at,consumed_at,transaction_blob
        ) VALUES (?,?,?,?,?,NULL,?)""",
        (
            "state-hash",
            "alice",
            local.id,
            "https://app.example/callback",
            "2026-10-03T13:00:00+00:00",
            store._encrypt_json({"verifier": "oauth-verifier"}),
        ),
    )
    store._db.commit()
    assert store.validate_encryption_key() is None

    wrong_key = _open_store(tmp_path, Fernet.generate_key())
    with pytest.raises(EnergyError, match="encrypted data cannot be decrypted") as caught:
        wrong_key.validate_encryption_key()
    assert "account-secret" not in str(caught.value)
    store._db.execute("DELETE FROM accounts")
    store._db.commit()
    with pytest.raises(EnergyError, match="encrypted data cannot be decrypted") as caught:
        wrong_key.validate_encryption_key()
    assert "oauth-verifier" not in str(caught.value)
    wrong_key.close()
    store.close()


@pytest.mark.asyncio
async def test_managed_verification_cas_preserves_newer_metadata_and_state(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key()
    clock = FixedClock()
    verifier = _open_store(tmp_path, key, clock)
    writer = _open_store(tmp_path, key, clock)
    account = _managed_account()
    verifier.stage_managed(account, "original-secret")
    _, version = verifier.managed_snapshot("alice", "workspace-a", account.id)
    verifier.activate_managed(
        "alice",
        "workspace-a",
        account.id,
        site=_site(),
        expected_version=version,
        verified_at=clock.value,
    )

    probe_started = asyncio.Event()
    finish_probe = asyncio.Event()

    async def delayed_probe(connection: ConnectedAccount, credential: str) -> bool:
        assert connection.state == "active"
        assert credential == "original-secret"
        probe_started.set()
        await finish_probe.wait()
        return True

    pending_verification = asyncio.create_task(
        verifier.verify_provider("alice", account.id, delayed_probe, site_id="site-home")
    )
    await probe_started.wait()

    row = writer._db.execute("SELECT * FROM accounts WHERE id = ?", (account.id,)).fetchone()
    captured_updated_at = row["updated_at"]
    newer_json = json.loads(row["account_json"])
    newer_json["settings"]["concurrent_marker"] = "preserve-this"
    writer._db.execute(
        """UPDATE accounts SET account_json = ?, managed_revision = managed_revision + 1
        WHERE id = ? AND workspace_id = ? AND state = 'active'""",
        (
            json.dumps(newer_json, separators=(",", ":"), sort_keys=True),
            account.id,
            "workspace-a",
        ),
    )
    writer._db.commit()
    after_update = writer._db.execute(
        "SELECT updated_at FROM accounts WHERE id = ?", (account.id,)
    ).fetchone()
    assert after_update["updated_at"] == captured_updated_at  # fixed-clock collision

    finish_probe.set()
    with pytest.raises(EnergyError, match="changed while provider verification was running"):
        await pending_verification
    current, current_version = writer.managed_snapshot("alice", "workspace-a", account.id)
    assert current.state == "active"
    assert current.settings["concurrent_marker"] == "preserve-this"
    assert current_version == 3

    # A stale probe cannot restore an active account over a concurrent
    # revoke-and-restage transition either.
    second_started = asyncio.Event()
    finish_second = asyncio.Event()

    async def second_delayed_probe(connection: ConnectedAccount, credential: str) -> bool:
        second_started.set()
        await finish_second.wait()
        return True

    stale_reactivation = asyncio.create_task(
        verifier.verify_provider("alice", account.id, second_delayed_probe, site_id="site-home")
    )
    await second_started.wait()
    revoked = writer.revoke("alice", account.id, site_id="site-home")
    assert revoked.state == "revoked"
    _, revoked_version = writer.managed_snapshot("alice", "workspace-a", account.id)
    staged = writer.stage_managed(account, "newer-secret", expected_version=revoked_version)
    assert staged.state == "pending_mapping"
    finish_second.set()
    with pytest.raises(EnergyError, match="changed while provider verification was running"):
        await stale_reactivation
    current, current_version = writer.managed_snapshot("alice", "workspace-a", account.id)
    assert current.state == "pending_mapping"
    assert current.site_id is None
    assert not current.enabled
    assert writer.pending_credential("alice", "workspace-a", account.id) == "newer-secret"
    assert current_version == revoked_version + 1
    writer.close()
    verifier.close()


@pytest.mark.asyncio
async def test_managed_oauth_refresh_is_denied_before_network_io(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "new-access"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = FixedClock()
    store = AuthStore(tmp_path, key, http=client, clock=clock)
    account = _managed_account(scheme="oauth")
    store.stage_managed(account, "oauth-access")
    _, version = store.managed_snapshot("alice", "workspace-a", account.id)
    store.activate_managed(
        "alice",
        "workspace-a",
        account.id,
        site=_site(),
        expected_version=version,
        verified_at=clock.value,
    )
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
    )
    store._db.execute(
        "UPDATE accounts SET secret_blob = ? WHERE id = ?",
        (
            store._encrypt_json(
                {
                    "credential": "oauth-access",
                    "refresh_token": "oauth-refresh",
                    "provider": store._provider_dump(provider),
                }
            ),
            account.id,
        ),
    )
    store._db.commit()

    with pytest.raises(EnergyError, match="workspace-scoped lifecycle operations"):
        await store.refresh("alice", account.id)
    assert requests == []
    await client.aclose()
    store.close()
