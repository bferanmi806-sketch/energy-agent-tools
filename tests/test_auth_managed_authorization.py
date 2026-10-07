from __future__ import annotations

import asyncio
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


def _authorization_error() -> EnergyError:
    return EnergyError("management_key_revoked", "Management authorization was revoked.")


def _account_row(store: AuthStore, connection_id: str) -> tuple[object, ...]:
    row = store._db.execute(
        "SELECT account_json, secret_blob, updated_at, last_verified_at, managed_revision "
        "FROM accounts WHERE id = ?",
        (connection_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _active_bearer(store: AuthStore, clock: FixedClock) -> ConnectedAccount:
    pending = ConnectedAccount(
        id="managed-provider-1",
        user_id="alice",
        workspace_id="workspace-a",
        toolkit="example-provider",
        auth=AuthConfig(scheme="bearer"),
        settings={"base_url": "https://provider.example"},
        enabled=False,
        state="pending_mapping",
        last_verified_at=clock.value - timedelta(minutes=1),
    )
    store.stage_managed(pending, "initial-access-token")
    snapshot, revision = store.managed_snapshot("alice", "workspace-a", pending.id)
    return store.activate_managed(
        "alice",
        "workspace-a",
        pending.id,
        site=Site(id="home", user_id="alice", name="Home", timezone="UTC"),
        expected_version=revision,
        verified_at=snapshot.last_verified_at,
    )


def _provider() -> OAuthProvider:
    return OAuthProvider(
        authorization_endpoint="https://ha.example/auth/authorize",
        token_endpoint="https://ha.example/auth/token",
        revocation_endpoint="https://ha.example/auth/revoke",
        client_id="https://app.example",
        redirect_uri="https://app.example/oauth/callback",
        protocol="home_assistant",
    )


def _oauth_account() -> ConnectedAccount:
    return ConnectedAccount(
        id="managed-oauth-1",
        user_id="alice",
        toolkit="home-assistant",
        auth=AuthConfig(scheme="oauth"),
        settings={
            "base_url": "https://ha.example",
            "managed_oauth_configuration_id": "home",
        },
    )


async def _verify_initial(_account: ConnectedAccount, credential: str) -> None:
    assert credential == "initial-access-token"


async def _active_oauth(
    tmp_path: Path,
    handler: httpx.AsyncBaseTransport,
    clock: FixedClock,
) -> tuple[AuthStore, httpx.AsyncClient, ConnectedAccount]:
    client = httpx.AsyncClient(transport=handler)
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client, clock=clock)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _oauth_account(), provider)
    pending = await store.complete_managed_oauth(
        "alice",
        "workspace-a",
        authorization.state,
        "authorization-code",
        provider.redirect_uri,
        verify=_verify_initial,
        expected_provider=provider,
        expected_configuration_id="home",
    )
    snapshot, revision = store.managed_snapshot("alice", "workspace-a", pending.id)
    active = store.activate_managed(
        "alice",
        "workspace-a",
        pending.id,
        site=Site(id="home", user_id="alice", name="Home", timezone="UTC"),
        expected_version=revision,
        verified_at=snapshot.last_verified_at,
    )
    return store, client, active


@pytest.mark.asyncio
async def test_complete_managed_oauth_denial_at_entry_keeps_transaction_and_skips_provider(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "initial-access-token", "refresh_token": "initial-refresh-token"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _oauth_account(), provider)
    denied = _authorization_error()
    verification_calls = 0

    async def verify(_account: ConnectedAccount, _credential: str) -> None:
        nonlocal verification_calls
        verification_calls += 1

    def authorize_write() -> None:
        raise denied

    with pytest.raises(EnergyError) as caught:
        await store.complete_managed_oauth(
            "alice",
            "workspace-a",
            authorization.state,
            "authorization-code",
            provider.redirect_uri,
            verify=verify,
            expected_provider=provider,
            expected_configuration_id="home",
            authorize_write=authorize_write,
        )

    assert caught.value is denied
    assert requests == []
    assert verification_calls == 0
    assert store._db.execute("SELECT COUNT(*) FROM oauth_transactions").fetchone()[0] == 1
    assert store.workspace_accounts("alice", "workspace-a") == []
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_managed_oauth_denial_before_publish_cleans_returned_grant(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    authorized = True

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/revoke":
            return httpx.Response(503, json={"error": "temporary provider failure"})
        return httpx.Response(
            200,
            json={
                "access_token": "initial-access-token",
                "refresh_token": "initial-refresh-token",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _oauth_account(), provider)
    denied = _authorization_error()
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1
        if not authorized:
            raise denied

    async def verify(_account: ConnectedAccount, credential: str) -> None:
        nonlocal authorized
        assert credential == "initial-access-token"
        authorized = False

    with pytest.raises(EnergyError) as caught:
        await store.complete_managed_oauth(
            "alice",
            "workspace-a",
            authorization.state,
            "authorization-code",
            provider.redirect_uri,
            verify=verify,
            expected_provider=provider,
            expected_configuration_id="home",
            authorize_write=authorize_write,
        )

    assert caught.value is denied
    assert authorizations == 2
    assert [request.url.path for request in requests] == ["/auth/token", "/auth/revoke"]
    assert store.workspace_accounts("alice", "workspace-a") == []
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 1
    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"initial-access-token" not in raw_db
    assert b"initial-refresh-token" not in raw_db
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_managed_oauth_publishes_with_valid_authorization(tmp_path: Path) -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "initial-access-token",
                "refresh_token": "initial-refresh-token",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _oauth_account(), provider)
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1

    pending = await store.complete_managed_oauth(
        "alice",
        "workspace-a",
        authorization.state,
        "authorization-code",
        provider.redirect_uri,
        verify=_verify_initial,
        expected_provider=provider,
        expected_configuration_id="home",
        authorize_write=authorize_write,
    )

    assert authorizations == 2
    assert pending.state == "pending_mapping"
    assert pending.site_id is None
    assert store.pending_credential("alice", "workspace-a", pending.id) == "initial-access-token"
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_verify_provider_rejects_revoked_authorization_before_probe(tmp_path: Path) -> None:
    clock = FixedClock()
    store = AuthStore(tmp_path, Fernet.generate_key(), clock=clock)
    active = _active_bearer(store, clock)
    before = _account_row(store, active.id)
    probe_calls = 0
    denied = _authorization_error()

    async def probe(_account: ConnectedAccount, _credential: str) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return True

    def authorize_write() -> None:
        raise denied

    with pytest.raises(EnergyError) as caught:
        await store.verify_provider(
            "alice", active.id, probe, site_id="home", authorize_write=authorize_write
        )

    assert caught.value is denied
    assert probe_calls == 0
    assert _account_row(store, active.id) == before
    store.close()


@pytest.mark.asyncio
async def test_verify_provider_rechecks_authorization_after_probe(tmp_path: Path) -> None:
    clock = FixedClock()
    store = AuthStore(tmp_path, Fernet.generate_key(), clock=clock)
    active = _active_bearer(store, clock)
    before = _account_row(store, active.id)
    authorized = True
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1
        if not authorized:
            raise _authorization_error()

    async def probe(_account: ConnectedAccount, credential: str) -> bool:
        nonlocal authorized
        assert credential == "initial-access-token"
        clock.value += timedelta(seconds=1)
        authorized = False
        return True

    with pytest.raises(EnergyError, match="Management authorization was revoked") as caught:
        await store.verify_provider(
            "alice", active.id, probe, site_id="home", authorize_write=authorize_write
        )

    assert caught.value.code == "management_key_revoked"
    assert authorizations == 2
    assert _account_row(store, active.id) == before
    store.close()


@pytest.mark.asyncio
async def test_verify_provider_commits_when_authorization_remains_valid(tmp_path: Path) -> None:
    clock = FixedClock()
    store = AuthStore(tmp_path, Fernet.generate_key(), clock=clock)
    active = _active_bearer(store, clock)
    _, revision = store.managed_snapshot("alice", "workspace-a", active.id)
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1

    async def probe(_account: ConnectedAccount, credential: str) -> bool:
        assert credential == "initial-access-token"
        clock.value += timedelta(seconds=1)
        return True

    verified = await store.verify_provider(
        "alice", active.id, probe, site_id="home", authorize_write=authorize_write
    )

    _, new_revision = store.managed_snapshot("alice", "workspace-a", active.id)
    assert authorizations == 2
    assert new_revision == revision + 1
    assert verified.last_verified_at == clock.value
    store.close()


@pytest.mark.asyncio
async def test_refresh_managed_rejects_revoked_authorization_before_provider_io(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "initial-access-token",
                "refresh_token": "initial-refresh-token",
                "expires_in": 1,
            },
        )

    clock = FixedClock()
    store, client, active = await _active_oauth(tmp_path, httpx.MockTransport(upstream), clock)
    requests.clear()
    clock.value += timedelta(seconds=2)
    before = _account_row(store, active.id)
    denied = _authorization_error()

    def authorize_write() -> None:
        raise denied

    with pytest.raises(EnergyError) as caught:
        await store.refresh_managed(
            "alice", "workspace-a", active.id, authorize_write=authorize_write
        )

    assert caught.value is denied
    assert requests == []
    assert _account_row(store, active.id) == before
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_managed_rechecks_authorization_after_waiting_for_lock(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "initial-access-token",
                "refresh_token": "initial-refresh-token",
                "expires_in": 1,
            },
        )

    clock = FixedClock()
    store, client, active = await _active_oauth(tmp_path, httpx.MockTransport(upstream), clock)
    requests.clear()
    clock.value += timedelta(seconds=2)
    before = _account_row(store, active.id)
    lock_id = "\0".join(("alice", "workspace-a", active.id))
    lock = store._refresh_lock(lock_id)
    await lock.acquire()
    authorized = True
    authorizations = 0
    entry_authorized = asyncio.Event()

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1
        if authorizations == 1:
            entry_authorized.set()
        if not authorized:
            raise _authorization_error()

    refresh = asyncio.create_task(
        store.refresh_managed("alice", "workspace-a", active.id, authorize_write=authorize_write)
    )
    await entry_authorized.wait()
    assert not refresh.done()
    authorized = False
    lock.release()

    with pytest.raises(EnergyError) as caught:
        await refresh

    assert caught.value.code == "management_key_revoked"
    assert authorizations == 2
    assert requests == []
    assert _account_row(store, active.id) == before
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_managed_denial_after_rotation_preserves_account_and_queues_cleanup(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    authorized = True

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal authorized
        requests.append(request)
        if request.url.path == "/auth/revoke":
            return httpx.Response(503, json={"error": "temporary provider failure"})
        grant_type = parse_qs(request.content.decode())["grant_type"][0]
        if grant_type == "authorization_code":
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access-token",
                    "refresh_token": "initial-refresh-token",
                    "expires_in": 1,
                },
            )
        authorized = False
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 120,
            },
        )

    clock = FixedClock()
    store, client, active = await _active_oauth(tmp_path, httpx.MockTransport(upstream), clock)
    clock.value += timedelta(seconds=2)
    before = _account_row(store, active.id)
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1
        if not authorized:
            raise _authorization_error()

    with pytest.raises(EnergyError, match="Management authorization was revoked") as caught:
        await store.refresh_managed(
            "alice", "workspace-a", active.id, authorize_write=authorize_write
        )

    assert caught.value.code == "management_key_revoked"
    assert authorizations == 3
    assert [request.url.path for request in requests] == [
        "/auth/token",
        "/auth/token",
        "/auth/revoke",
    ]
    assert parse_qs(requests[-1].content.decode()) == {"token": ["rotated-refresh-token"]}
    assert _account_row(store, active.id) == before
    original = store._decrypt_json(
        store._db.execute("SELECT secret_blob FROM accounts WHERE id = ?", (active.id,)).fetchone()[
            0
        ]
    )
    assert original["credential"] == "initial-access-token"
    assert original["refresh_token"] == "initial-refresh-token"
    assert store.pending_managed_oauth_cleanup("alice", "workspace-a", "home") == 1
    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"rotated-access-token" not in raw_db
    assert b"rotated-refresh-token" not in raw_db
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_managed_commits_rotation_when_authorization_remains_valid(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        grant_type = parse_qs(request.content.decode())["grant_type"][0]
        if grant_type == "authorization_code":
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access-token",
                    "refresh_token": "initial-refresh-token",
                    "expires_in": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 120,
            },
        )

    clock = FixedClock()
    store, client, active = await _active_oauth(tmp_path, httpx.MockTransport(upstream), clock)
    clock.value += timedelta(seconds=2)
    _, revision = store.managed_snapshot("alice", "workspace-a", active.id)
    authorizations = 0

    def authorize_write() -> None:
        nonlocal authorizations
        authorizations += 1

    refreshed = await store.refresh_managed(
        "alice", "workspace-a", active.id, authorize_write=authorize_write
    )

    _, new_revision = store.managed_snapshot("alice", "workspace-a", active.id)
    secret = store._decrypt_json(
        store._db.execute("SELECT secret_blob FROM accounts WHERE id = ?", (active.id,)).fetchone()[
            0
        ]
    )
    assert authorizations == 3
    assert len(requests) == 2
    assert new_revision == revision + 1
    assert refreshed.state == "active"
    assert secret["credential"] == "rotated-access-token"
    assert secret["refresh_token"] == "rotated-refresh-token"
    store.close()
    await client.aclose()
