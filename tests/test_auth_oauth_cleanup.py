from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

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


def _provider(*, protocol: str = "home_assistant") -> OAuthProvider:
    if protocol == "home_assistant":
        return OAuthProvider(
            authorization_endpoint="https://ha.example/auth/authorize",
            token_endpoint="https://ha.example/auth/token",
            client_id="https://app.example",
            redirect_uri="https://app.example/oauth/callback",
            revocation_endpoint="https://ha.example/auth/revoke",
            protocol="home_assistant",
        )
    return OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="oauth-client",
        redirect_uri="https://app.example/oauth/callback",
        revocation_endpoint="https://oauth.example/revoke",
        protocol="oauth2_pkce",
    )


def _account(
    *, user_id: str = "alice", workspace_id: str = "workspace-a", configuration_id: str = "home"
) -> ConnectedAccount:
    return ConnectedAccount(
        id="managed-ha-1",
        user_id=user_id,
        toolkit="home-assistant",
        auth=AuthConfig(scheme="oauth"),
        settings={
            "base_url": "https://ha.example",
            "managed_oauth_configuration_id": configuration_id,
        },
    )


def _open(
    root: Path,
    key: bytes,
    handler: httpx.AsyncBaseTransport,
    clock: FixedClock | None = None,
) -> tuple[AuthStore, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    return AuthStore(root, key, http=client, clock=clock), client


async def _verify(_account: ConnectedAccount, credential: str) -> None:
    assert credential == "access-token"


async def _complete(
    store: AuthStore,
    request,
    provider: OAuthProvider,
    *,
    account: ConnectedAccount | None = None,
    verify=_verify,
) -> ConnectedAccount:
    selected = account or _account()
    return await store.complete_managed_oauth(
        selected.user_id,
        "workspace-a",
        request.state,
        "authorization-code-private",
        provider.redirect_uri,
        verify=verify,
        expected_provider=provider,
        expected_configuration_id=selected.settings["managed_oauth_configuration_id"],
    )


def _activate(store: AuthStore, account: ConnectedAccount) -> ConnectedAccount:
    pending, revision = store.managed_snapshot(account.user_id, "workspace-a", account.id)
    return store.activate_managed(
        account.user_id,
        "workspace-a",
        account.id,
        site=Site(id="home", user_id=account.user_id, name="Home", timezone="UTC"),
        expected_version=revision,
        verified_at=pending.last_verified_at,
    )


@pytest.mark.asyncio
async def test_failed_verification_revokes_exchanged_grant_without_publishing_account(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "refresh-token"},
            )
        return httpx.Response(204)

    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream))
    provider = _provider()
    account = _account()
    request = store.begin_managed_oauth("alice", "workspace-a", account, provider)

    async def fail_verification(_account: ConnectedAccount, _credential: str) -> None:
        raise RuntimeError("private access token")

    with pytest.raises(EnergyError, match="Provider verification failed") as caught:
        await _complete(store, request, provider, account=account, verify=fail_verification)
    assert "private access token" not in str(caught.value)
    assert [item.url.path for item in requests] == ["/auth/token", "/auth/revoke"]
    assert parse_qs(requests[-1].content.decode()) == {"token": ["refresh-token"]}
    assert store.workspace_accounts("alice", "workspace-a") == []
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_failed_verification_retains_cleanup_for_retry_after_provider_503(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def first_upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "failed-probe-refresh"},
            )
        return httpx.Response(503, json={"error": "provider secret detail"})

    key = Fernet.generate_key()
    store, client = _open(tmp_path, key, httpx.MockTransport(first_upstream))
    provider = _provider()
    account = _account()
    authorization = store.begin_managed_oauth("alice", "workspace-a", account, provider)

    async def fail_verification(_account: ConnectedAccount, _credential: str) -> None:
        raise RuntimeError("probe secret detail")

    with pytest.raises(EnergyError, match="Provider verification failed") as caught:
        await _complete(store, authorization, provider, account=account, verify=fail_verification)
    assert "probe secret detail" not in str(caught.value)
    assert "provider secret detail" not in str(caught.value)
    assert [item.url.path for item in requests] == ["/auth/token", "/auth/revoke"]
    assert store.workspace_accounts("alice", "workspace-a") == []
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 1
    assert "failed-probe-refresh" in store.redaction_values("alice")
    assert b"failed-probe-refresh" not in (tmp_path / "auth.sqlite3").read_bytes()
    store.close()
    await client.aclose()

    async def retry_upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    reopened, retry_client = _open(tmp_path, key, httpx.MockTransport(retry_upstream))
    result = await reopened.retry_managed_oauth_cleanup(
        "alice", "workspace-a", configuration_id="home", provider=provider
    )
    assert result == {"attempted": 1, "succeeded": 1, "pending": 0}
    assert reopened.workspace_accounts("alice", "workspace-a") == []
    reopened.close()
    await retry_client.aclose()


@pytest.mark.asyncio
async def test_failed_revoke_survives_restart_and_scoped_retry_clears_only_cleanup_record(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def first_upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/revoke":
            return httpx.Response(503, json={"error": "private provider detail"})
        return httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token-private",
                "expires_in": 3600,
            },
        )

    key = Fernet.generate_key()
    store, client = _open(tmp_path, key, httpx.MockTransport(first_upstream))
    provider = _provider()
    account = _account()
    authorization = store.begin_managed_oauth("alice", "workspace-a", account, provider)
    pending = await _complete(store, authorization, provider, account=account)
    active = _activate(store, pending)
    revoked, status = await store.revoke_managed("alice", "workspace-a", active.id)
    assert revoked.state == "revoked"
    assert status is False
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 1
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    store.validate_encryption_key()
    assert "refresh-token-private" in store.redaction_values("alice")
    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"refresh-token-private" not in raw_db
    assert b"https://ha.example/auth/revoke" not in raw_db
    wrong_key_store = AuthStore(tmp_path, Fernet.generate_key())
    with pytest.raises(EnergyError, match="encrypted data cannot be decrypted"):
        wrong_key_store.validate_encryption_key()
    wrong_key_store.close()

    backup_root = tmp_path / "sqlite-backup"
    backup_root.mkdir()
    source = sqlite3.connect(tmp_path / "auth.sqlite3")
    destination = sqlite3.connect(backup_root / "auth.sqlite3")
    source.backup(destination)
    destination.close()
    source.close()
    store.close()
    await client.aclose()

    async def retry_upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    reopened, retry_client = _open(backup_root, key, httpx.MockTransport(retry_upstream))
    result = await reopened.retry_managed_oauth_cleanup(
        "alice", "workspace-a", configuration_id="home", provider=provider
    )
    assert result == {"attempted": 1, "succeeded": 1, "pending": 0}
    assert reopened.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    current, _ = reopened.managed_snapshot("alice", "workspace-a", active.id)
    assert current.state == "revoked"
    with pytest.raises(EnergyError):
        reopened.credential("alice", active.id, site_id="home")
    assert [item.url.path for item in requests] == ["/auth/token", "/auth/revoke", "/auth/revoke"]
    reopened.close()
    await retry_client.aclose()


@pytest.mark.asyncio
async def test_cleanup_retry_checks_user_workspace_and_full_provider_snapshot_before_io(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/revoke":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"access_token": "access-token", "refresh_token": "refresh-token"},
        )

    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream))
    provider = _provider()
    account = _account()
    authorization = store.begin_managed_oauth("alice", "workspace-a", account, provider)
    pending = await _complete(store, authorization, provider, account=account)
    active = _activate(store, pending)
    await store.revoke_managed("alice", "workspace-a", active.id)
    assert len(requests) == 2

    for user_id, workspace_id in (("mallory", "workspace-a"), ("alice", "workspace-b")):
        result = await store.retry_managed_oauth_cleanup(
            user_id, workspace_id, configuration_id="home", provider=provider
        )
        assert result == {"attempted": 0, "succeeded": 0, "pending": 0}
    changed_provider = OAuthProvider(
        authorization_endpoint=provider.authorization_endpoint,
        token_endpoint=provider.token_endpoint,
        client_id="https://app.example/other-client-id",
        redirect_uri=provider.redirect_uri,
        revocation_endpoint=provider.revocation_endpoint,
        protocol="home_assistant",
    )
    result = await store.retry_managed_oauth_cleanup(
        "alice", "workspace-a", configuration_id="home", provider=changed_provider
    )
    assert result == {"attempted": 0, "succeeded": 0, "pending": 1}
    assert len(requests) == 2
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 1
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_repeat_revoke_retries_failed_remote_cleanup(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/revoke" and len(requests) == 2:
            return httpx.Response(503)
        if request.url.path == "/auth/token":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "refresh-token"},
            )
        return httpx.Response(204)

    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream))
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, authorization, provider)
    active = _activate(store, pending)
    _, first_status = await store.revoke_managed("alice", "workspace-a", active.id)
    _, second_status = await store.revoke_managed("alice", "workspace-a", active.id)
    assert first_status is False
    assert second_status is True
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    assert [item.url.path for item in requests] == ["/auth/token", "/auth/revoke", "/auth/revoke"]
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_failed_publication_after_superseded_state_revokes_orphan_grant(
    tmp_path: Path,
) -> None:
    exchange_entered = asyncio.Event()
    continue_exchange = asyncio.Event()
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            if parse_qs(request.content.decode())["grant_type"] == ["authorization_code"]:
                exchange_entered.set()
                await continue_exchange.wait()
                return httpx.Response(
                    200,
                    json={"access_token": "access-token", "refresh_token": "orphan-refresh"},
                )
        return httpx.Response(204)

    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream))
    provider = _provider(protocol="oauth2_pkce")
    account = _account()
    old = store.begin_managed_oauth("alice", "workspace-a", account, provider)
    complete = asyncio.create_task(_complete(store, old, provider, account=account))
    await asyncio.wait_for(exchange_entered.wait(), timeout=1)
    store.begin_managed_oauth("alice", "workspace-a", account, provider)
    continue_exchange.set()
    with pytest.raises(EnergyError, match="changed"):
        await complete
    assert [item.url.path for item in requests] == ["/token", "/revoke"]
    assert parse_qs(requests[-1].content.decode()) == {
        "token": ["orphan-refresh"],
        "token_type_hint": ["refresh_token"],
        "client_id": [provider.client_id],
    }
    assert store.workspace_accounts("alice", "workspace-a") == []
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_cas_failure_revokes_only_returned_rotated_grant(tmp_path: Path) -> None:
    refresh_entered = asyncio.Event()
    continue_refresh = asyncio.Event()
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/token":
            values = parse_qs(request.content.decode())
            if values["grant_type"] == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-token",
                        "refresh_token": "old-refresh",
                        "expires_in": 1,
                    },
                )
            refresh_entered.set()
            await continue_refresh.wait()
            return httpx.Response(
                200,
                json={"access_token": "new-access", "refresh_token": "rotated-refresh"},
            )
        return httpx.Response(204)

    clock = FixedClock()
    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream), clock)
    provider = _provider()
    account = _account()
    authorization = store.begin_managed_oauth("alice", "workspace-a", account, provider)
    pending = await _complete(store, authorization, provider, account=account)
    active = _activate(store, pending)
    clock.value += timedelta(seconds=2)
    refresh = asyncio.create_task(store.refresh_managed("alice", "workspace-a", active.id))
    await asyncio.wait_for(refresh_entered.wait(), timeout=1)
    revoked, upstream_status = await store.revoke_managed("alice", "workspace-a", active.id)
    assert revoked.state == "revoked"
    assert upstream_status is True
    continue_refresh.set()
    with pytest.raises(EnergyError, match="changed"):
        await refresh
    revoke_tokens = [
        parse_qs(item.content.decode()).get("token", [])
        for item in requests
        if item.url.path == "/auth/revoke" or item.url.path == "/revoke"
    ]
    assert revoke_tokens == [["old-refresh"], ["rotated-refresh"]]
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    store.close()
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_profile", [False, True])
async def test_disconnect_bounds_cleanup_and_respects_current_provider(
    tmp_path: Path, changed_profile: bool
) -> None:
    calls: list[httpx.Request] = []
    available = False

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(
                200, json={"access_token": "access-token", "refresh_token": "fixture-refresh"}
            )
        return httpx.Response(204 if available else 503)

    async def reject(_account: ConnectedAccount, _credential: str) -> None:
        raise RuntimeError("fixture verification failure")

    store, client = _open(tmp_path, Fernet.generate_key(), httpx.MockTransport(upstream))
    old = _provider()
    for _ in range(3):
        start = store.begin_managed_oauth("alice", "workspace-a", _account(), old)
        with pytest.raises(EnergyError, match="verification"):
            await _complete(store, start, old, verify=reject)
    current = (
        OAuthProvider(
            authorization_endpoint="https://new-ha.example/auth/authorize",
            token_endpoint="https://new-ha.example/auth/token",
            revocation_endpoint="https://new-ha.example/auth/revoke",
            client_id=old.client_id,
            redirect_uri=old.redirect_uri,
            protocol="home_assistant",
        )
        if changed_profile
        else old
    )
    start = store.begin_managed_oauth("alice", "workspace-a", _account(), current)
    active = _activate(store, await _complete(store, start, current))
    calls.clear()
    available = True
    revoked, status = await store.revoke_managed(
        "alice", "workspace-a", active.id, expected_provider=current
    )
    assert revoked.state == "revoked"
    assert status is False
    assert len(calls) == 1
    assert calls[0].url.host == ("new-ha.example" if changed_profile else "ha.example")
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 3
    calls.clear()
    _, status = await store.revoke_managed(
        "alice", "workspace-a", active.id, expected_provider=current
    )
    assert status is False
    assert len(calls) == (0 if changed_profile else 1)
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    store.close()
    await client.aclose()


def test_schema_one_migration_preserves_accounts_and_oauth_transactions(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    store = AuthStore(tmp_path, key)
    account = ConnectedAccount(
        id="legacy-oauth",
        user_id="alice",
        toolkit="home-assistant",
        auth=AuthConfig(scheme="oauth"),
        settings={"base_url": "https://ha.example"},
    )
    store.configure(account, "")
    provider = _provider(protocol="oauth2_pkce")
    authorization = store.begin_oauth("alice", account.id, provider)
    transaction_hash = hashlib.sha256(authorization.state.encode("ascii")).hexdigest()
    assert store._db.execute(
        "SELECT 1 FROM oauth_transactions WHERE state_hash = ?", (transaction_hash,)
    ).fetchone()
    store.close()

    db = sqlite3.connect(tmp_path / "auth.sqlite3")
    db.execute("DROP TABLE IF EXISTS oauth_cleanup")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    migrated = AuthStore(tmp_path, key)
    assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated.get_account("alice", account.id).state == "pending"
    assert migrated._db.execute(
        "SELECT 1 FROM oauth_transactions WHERE state_hash = ?", (transaction_hash,)
    ).fetchone()
    assert migrated.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 0
    migrated.close()
