from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def _account(
    connection_id: str = "managed-ha-1",
    *,
    user_id: str = "alice",
    toolkit: str = "home-assistant",
    configuration_id: str | None = None,
) -> ConnectedAccount:
    settings = {"base_url": "https://ha.example"}
    if configuration_id is not None:
        settings["managed_oauth_configuration_id"] = configuration_id
    return ConnectedAccount(
        id=connection_id,
        user_id=user_id,
        toolkit=toolkit,
        auth=AuthConfig(scheme="oauth"),
        settings=settings,
    )


def _provider(
    *,
    protocol: str = "home_assistant",
    revocation_endpoint: str | None = None,
    state_ttl_seconds: int = 600,
) -> OAuthProvider:
    if protocol == "home_assistant":
        return OAuthProvider(
            authorization_endpoint="https://ha.example/auth/authorize",
            token_endpoint="https://ha.example/auth/token",
            client_id="https://app.example",
            redirect_uri="https://app.example/oauth/callback",
            revocation_endpoint=revocation_endpoint,
            protocol="home_assistant",
            state_ttl_seconds=state_ttl_seconds,
        )
    return OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="energy-agent-client",
        redirect_uri="https://app.example/oauth/callback",
        revocation_endpoint=revocation_endpoint,
        protocol="oauth2_pkce",
        state_ttl_seconds=state_ttl_seconds,
    )


def _store(
    tmp_path: Path,
    *,
    handler: httpx.AsyncBaseTransport | None = None,
    clock: FixedClock | None = None,
    key: bytes | None = None,
) -> tuple[AuthStore, bytes]:
    key = key or Fernet.generate_key()
    client = httpx.AsyncClient(transport=handler) if handler else None
    store = AuthStore(tmp_path, key, http=client, clock=clock)
    return store, key


async def _verify(account: ConnectedAccount, credential: str) -> None:
    assert account.workspace_id is None
    assert account.site_id is None
    assert credential == "access-token"


async def _complete(
    store: AuthStore,
    request,
    provider: OAuthProvider,
    *,
    user_id: str = "alice",
    workspace_id: str = "workspace-a",
    verify=_verify,
    expected_provider: OAuthProvider | None = None,
    expected_configuration_id: str | None = None,
) -> ConnectedAccount:
    return await store.complete_managed_oauth(
        user_id,
        workspace_id,
        request.state,
        "one-time-code",
        provider.redirect_uri,
        verify=verify,
        expected_provider=expected_provider,
        expected_configuration_id=expected_configuration_id,
    )


async def _authorize_pending(
    store: AuthStore,
    provider: OAuthProvider,
    account: ConnectedAccount | None = None,
) -> ConnectedAccount:
    template = account or _account()
    workspace_id = template.workspace_id or "workspace-a"
    authorization = store.begin_managed_oauth(template.user_id, workspace_id, template, provider)
    return await store.complete_managed_oauth(
        template.user_id,
        workspace_id,
        authorization.state,
        "one-time-code",
        provider.redirect_uri,
        verify=_verify,
        expected_provider=provider,
    )


def _activate(store: AuthStore, account: ConnectedAccount) -> ConnectedAccount:
    workspace_id = account.workspace_id or ""
    pending, revision = store.managed_snapshot(account.user_id, workspace_id, account.id)
    assert pending.last_verified_at is not None
    return store.activate_managed(
        account.user_id,
        workspace_id,
        account.id,
        site=Site(id="home", user_id=account.user_id, name="Home", timezone="UTC"),
        expected_version=revision,
        verified_at=pending.last_verified_at,
    )


@pytest.mark.asyncio
async def test_managed_home_assistant_flow_is_unmapped_until_verified_and_persisted(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 120,
                "token_type": "Bearer",
            },
        )

    clock = FixedClock()
    store, key = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    params = parse_qs(urlparse(authorization.authorization_url).query)
    assert params["client_id"] == [provider.client_id]
    assert params["redirect_uri"] == [provider.redirect_uri]
    assert params["state"] == [authorization.state]
    assert "code_challenge" not in params
    assert "response_type" not in params
    assert store.workspace_accounts("alice", "workspace-a") == []

    store.close()
    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock, key=key)
    pending = await _complete(store, authorization, provider, expected_provider=provider)
    assert len(requests) == 1
    assert requests[0].url == provider.token_endpoint
    assert set(parse_qs(requests[0].content.decode())) == {
        "grant_type",
        "code",
        "client_id",
    }
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": ["one-time-code"],
        "client_id": [provider.client_id],
    }
    assert pending.workspace_id == "workspace-a"
    assert pending.site_id is None
    assert pending.state == "pending_mapping"
    assert not pending.enabled
    with pytest.raises(EnergyError, match="pending"):
        store.credential("alice", pending.id)
    assert store.pending_credential("alice", "workspace-a", pending.id) == "access-token"
    assert store.redaction_values() == ["access-token", "refresh-token"]

    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    for secret in (authorization.state, "one-time-code", "access-token", "refresh-token"):
        assert secret.encode() not in raw_db

    snapshot, revision = store.managed_snapshot("alice", "workspace-a", pending.id)
    mapped = store.activate_managed(
        "alice",
        "workspace-a",
        pending.id,
        site=Site(id="home", user_id="alice", name="Home", timezone="UTC"),
        expected_version=revision,
        verified_at=snapshot.last_verified_at,
    )
    assert mapped.state == "active"
    assert mapped.site_id == "home"
    store.close()


def test_home_assistant_provider_rejects_generic_oauth_options() -> None:
    with pytest.raises(ValueError, match="client_secret"):
        OAuthProvider(
            authorization_endpoint="https://ha.example/auth/authorize",
            token_endpoint="https://ha.example/auth/token",
            client_id="https://app.example",
            redirect_uri="https://app.example/oauth/callback",
            client_secret="not-supported",
            protocol="home_assistant",
        )
    with pytest.raises(ValueError, match="Home Assistant OAuth URLs"):
        OAuthProvider(
            authorization_endpoint="https://ha.example/auth/authorize",
            token_endpoint="https://ha.example/auth/token",
            client_id="https://app.example",
            redirect_uri="https://other.example/oauth/callback",
            protocol="home_assistant",
        )


@pytest.mark.asyncio
async def test_managed_scope_and_approved_provider_are_checked_before_io(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)

    with pytest.raises(EnergyError):
        await store.complete_managed_oauth(
            "alice",
            "workspace-b",
            authorization.state,
            "one-time-code",
            provider.redirect_uri,
            verify=_verify,
            expected_provider=provider,
        )
    changed_provider = _provider(revocation_endpoint="https://ha.example/auth/revoke")
    with pytest.raises(EnergyError):
        await _complete(store, authorization, provider, expected_provider=changed_provider)
    row = store._db.execute(
        "SELECT consumed_at FROM oauth_transactions WHERE state_hash = ?",
        (hashlib.sha256(authorization.state.encode("ascii")).hexdigest(),),
    ).fetchone()
    assert row["consumed_at"] is None
    assert requests == []

    await _complete(store, authorization, provider, expected_provider=provider)
    assert len(requests) == 1
    store.close()


@pytest.mark.asyncio
async def test_configuration_alias_mismatch_preserves_state_before_provider_io(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider()
    authorization = store.begin_managed_oauth(
        "alice", "workspace-a", _account(configuration_id="approved-ha-a"), provider
    )
    with pytest.raises(EnergyError):
        await _complete(
            store,
            authorization,
            provider,
            expected_provider=provider,
            expected_configuration_id="approved-ha-b",
        )
    state_hash = hashlib.sha256(authorization.state.encode("ascii")).hexdigest()
    row = store._db.execute(
        "SELECT consumed_at FROM oauth_transactions WHERE state_hash = ?", (state_hash,)
    ).fetchone()
    assert row["consumed_at"] is None
    assert calls == []

    await _complete(
        store,
        authorization,
        provider,
        expected_provider=provider,
        expected_configuration_id="approved-ha-a",
    )
    assert len(calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_failed_verification_publishes_no_account_and_state_cannot_replay(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)

    async def fail_verification(account: ConnectedAccount, credential: str) -> None:
        raise RuntimeError(f"sensitive {credential}")

    with pytest.raises(EnergyError, match="verification failed") as caught:
        await _complete(store, authorization, provider, verify=fail_verification)
    assert "access-token" not in str(caught.value)
    assert store.workspace_accounts("alice", "workspace-a") == []
    assert len(requests) == 1
    with pytest.raises(EnergyError):
        await _complete(store, authorization, provider)
    assert len(requests) == 1
    store.close()


@pytest.mark.parametrize(
    ("user_id", "workspace_id"),
    [("mallory", "workspace-a"), ("alice", "workspace-b")],
)
@pytest.mark.asyncio
async def test_wrong_owner_or_workspace_is_rejected_before_claim_and_provider_io(
    tmp_path: Path, user_id: str, workspace_id: str
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    with pytest.raises(EnergyError):
        await store.complete_managed_oauth(
            user_id,
            workspace_id,
            authorization.state,
            "one-time-code",
            provider.redirect_uri,
            verify=_verify,
            expected_provider=provider,
        )
    state_hash = hashlib.sha256(authorization.state.encode("ascii")).hexdigest()
    row = store._db.execute(
        "SELECT consumed_at FROM oauth_transactions WHERE state_hash = ?", (state_hash,)
    ).fetchone()
    assert row["consumed_at"] is None
    assert calls == []
    await _complete(store, authorization, provider, expected_provider=provider)
    assert len(calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_operator_and_managed_transactions_cannot_consume_each_other(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    managed = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    with pytest.raises(EnergyError):
        await store.complete_oauth("alice", managed.state, "one-time-code", provider.redirect_uri)
    assert calls == []
    await _complete(store, managed, provider)
    assert len(calls) == 1

    operator_account = ConnectedAccount(
        id="operator-oauth",
        user_id="alice",
        toolkit="home-assistant",
        auth=AuthConfig(scheme="oauth"),
        settings={"base_url": "https://ha.example"},
    )
    store.configure(operator_account, "")
    operator = store.begin_oauth("alice", operator_account.id, provider)
    with pytest.raises(EnergyError):
        await store.complete_managed_oauth(
            "alice",
            "workspace-a",
            operator.state,
            "one-time-code",
            provider.redirect_uri,
            verify=_verify,
            expected_provider=provider,
        )
    operator_hash = hashlib.sha256(operator.state.encode("ascii")).hexdigest()
    row = store._db.execute(
        "SELECT consumed_at FROM oauth_transactions WHERE state_hash = ?", (operator_hash,)
    ).fetchone()
    assert row["consumed_at"] is None
    assert len(calls) == 1
    await store.complete_oauth("alice", operator.state, "one-time-code", provider.redirect_uri)
    assert len(calls) == 2
    store.close()


@pytest.mark.asyncio
async def test_expired_state_replay_and_persisted_transaction_recovery(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "access-token"})

    clock = FixedClock()
    key = Fernet.generate_key()
    provider = _provider(state_ttl_seconds=1)
    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock, key=key)
    expired = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    clock.value += timedelta(seconds=2)
    with pytest.raises(EnergyError):
        await _complete(store, expired, provider)
    assert calls == []

    fresh = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    store.close()
    reopened, _ = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock, key=key)
    await _complete(reopened, fresh, provider)
    assert len(calls) == 1
    with pytest.raises(EnergyError):
        await _complete(reopened, fresh, provider)
    assert len(calls) == 1
    reopened.close()


@pytest.mark.asyncio
async def test_concurrent_callback_claims_state_once(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    first = asyncio.create_task(_complete(store, authorization, provider))
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(EnergyError):
        await _complete(store, authorization, provider)
    assert len(calls) == 1
    release.set()
    await first
    assert len(calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_new_authorization_supersedes_inflight_callback(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"access_token": "access-token"})

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    old = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    completion = asyncio.create_task(_complete(store, old, provider))
    await asyncio.wait_for(entered.wait(), timeout=1)
    latest = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    release.set()
    with pytest.raises(EnergyError, match="changed"):
        await completion
    assert store.workspace_accounts("alice", "workspace-a") == []
    await _complete(store, latest, provider)
    assert len(calls) == 2
    store.close()


@pytest.mark.asyncio
async def test_pending_transaction_secrets_are_encrypted_and_redacted(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="energy-agent-client",
        redirect_uri="https://app.example/oauth/callback",
        client_secret="client-secret-private",
        protocol="oauth2_pkce",
    )
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    row = store._db.execute("SELECT transaction_blob FROM oauth_transactions").fetchone()
    transaction = store._decrypt_json(row["transaction_blob"])
    assert transaction["format_version"] == 1
    assert transaction["kind"] == "managed"
    assert transaction["nonce"]
    assert transaction["provider"]["protocol"] == "oauth2_pkce"
    verifier = transaction["verifier"]
    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    for secret in (authorization.state, verifier, "client-secret-private"):
        assert secret.encode() not in raw_db
    assert set(store.redaction_values()) == {verifier, "client-secret-private"}
    store.close()


@pytest.mark.asyncio
async def test_managed_refresh_rotates_token_with_exact_scope_and_keeps_verification_time(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        values = parse_qs(request.content.decode())
        if values["grant_type"] == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 10,
                },
            )
        assert values["grant_type"] == ["refresh_token"]
        assert values["refresh_token"] == ["refresh-token"]
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 120,
            },
        )

    clock = FixedClock()
    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, authorization, provider, expected_provider=provider)
    active = _activate(store, pending)
    verified_at = active.last_verified_at

    with pytest.raises(EnergyError):
        await store.refresh_managed("alice", "workspace-b", active.id)
    assert len(requests) == 1

    clock.value += timedelta(seconds=11)
    refreshed = await store.refresh_managed("alice", "workspace-a", active.id)
    assert len(requests) == 2
    assert refreshed.state == "active"
    assert refreshed.last_verified_at == verified_at
    row = store._db.execute(
        "SELECT secret_blob FROM accounts WHERE id = ?", (active.id,)
    ).fetchone()
    secret = store._decrypt_json(row["secret_blob"])
    assert secret["credential"] == "rotated-access-token"
    assert secret["refresh_token"] == "rotated-refresh-token"

    still_fresh = await store.refresh_managed("alice", "workspace-a", active.id)
    assert still_fresh.last_verified_at == verified_at
    assert len(requests) == 2
    store.close()


@pytest.mark.asyncio
async def test_revoke_wins_during_inflight_managed_refresh(tmp_path: Path) -> None:
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        values = parse_qs(request.content.decode())
        if values["grant_type"] == ["refresh_token"]:
            refresh_entered.set()
            await release_refresh.wait()
            return httpx.Response(
                200,
                json={"access_token": "late-access", "refresh_token": "late-refresh"},
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 1,
            },
        )

    clock = FixedClock()
    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler), clock=clock)
    provider = _provider()
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, authorization, provider, expected_provider=provider)
    active = _activate(store, pending)
    clock.value += timedelta(seconds=2)

    refreshing = asyncio.create_task(store.refresh_managed("alice", "workspace-a", active.id))
    await asyncio.wait_for(refresh_entered.wait(), timeout=1)
    revoked, upstream = await store.revoke_managed("alice", "workspace-a", active.id)
    assert revoked.state == "revoked"
    assert upstream is None
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")

    release_refresh.set()
    with pytest.raises(EnergyError, match="changed"):
        await refreshing
    current, _ = store.managed_snapshot("alice", "workspace-a", active.id)
    assert current.state == "revoked"
    assert len(requests) == 2
    store.close()


@pytest.mark.asyncio
async def test_revoke_wins_during_callback_verification(tmp_path: Path) -> None:
    verification_entered = asyncio.Event()
    release_verification = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "access-token", "refresh_token": "refresh-token"},
        )

    async def blocked_verify(account: ConnectedAccount, credential: str) -> None:
        assert account.workspace_id is None
        assert credential == "access-token"
        verification_entered.set()
        await release_verification.wait()

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    first = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, first, provider)
    second = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    completion = asyncio.create_task(
        store.complete_managed_oauth(
            "alice",
            "workspace-a",
            second.state,
            "one-time-code",
            provider.redirect_uri,
            verify=blocked_verify,
            expected_provider=provider,
        )
    )
    await asyncio.wait_for(verification_entered.wait(), timeout=1)

    revoked, upstream = await store.revoke_managed("alice", "workspace-a", pending.id)
    assert revoked.state == "revoked"
    assert upstream is None
    release_verification.set()
    with pytest.raises(EnergyError, match="changed"):
        await completion
    current, _ = store.managed_snapshot("alice", "workspace-a", pending.id)
    assert current.state == "revoked"
    assert len(requests) == 2
    store.close()


@pytest.mark.asyncio
async def test_repeated_revoke_cancels_reauthorization_from_revoked_state(
    tmp_path: Path,
) -> None:
    verification_entered = asyncio.Event()
    release_verification = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "access-token", "refresh_token": "refresh-token"},
        )

    async def blocked_verify(account: ConnectedAccount, credential: str) -> None:
        assert credential == "access-token"
        verification_entered.set()
        await release_verification.wait()

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(protocol="oauth2_pkce")
    first = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, first, provider)
    revoked, status = await store.revoke_managed("alice", "workspace-a", pending.id)
    assert revoked.state == "revoked"
    assert status is None

    reconnect = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    completion = asyncio.create_task(
        store.complete_managed_oauth(
            "alice",
            "workspace-a",
            reconnect.state,
            "one-time-code",
            provider.redirect_uri,
            verify=blocked_verify,
            expected_provider=provider,
        )
    )
    await asyncio.wait_for(verification_entered.wait(), timeout=1)
    second_revoke, second_status = await store.revoke_managed("alice", "workspace-a", pending.id)
    assert second_revoke.state == "revoked"
    assert second_status is None
    release_verification.set()
    with pytest.raises(EnergyError, match="changed"):
        await completion
    current, _ = store.managed_snapshot("alice", "workspace-a", pending.id)
    assert current.state == "revoked"
    store.close()


@pytest.mark.parametrize(("status_code", "expected_status"), [(204, True), (503, False)])
@pytest.mark.asyncio
async def test_managed_revoke_is_scoped_local_first_and_reports_remote_status(
    tmp_path: Path, status_code: int, expected_status: bool
) -> None:
    revoke_entered = asyncio.Event()
    release_revoke = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/revoke":
            revoke_entered.set()
            await release_revoke.wait()
            return httpx.Response(status_code, content=b"provider detail secret")
        return httpx.Response(
            200,
            json={"access_token": "access-token", "refresh_token": "refresh-token"},
        )

    store, _ = _store(tmp_path, handler=httpx.MockTransport(handler))
    provider = _provider(revocation_endpoint="https://ha.example/auth/revoke")
    authorization = store.begin_managed_oauth("alice", "workspace-a", _account(), provider)
    pending = await _complete(store, authorization, provider, expected_provider=provider)
    active = _activate(store, pending)

    with pytest.raises(EnergyError):
        await store.revoke_managed("alice", "workspace-b", active.id)
    assert len(requests) == 1
    assert requests[0].url.path == "/auth/token"

    revoking = asyncio.create_task(store.revoke_managed("alice", "workspace-a", active.id))
    await asyncio.wait_for(revoke_entered.wait(), timeout=1)
    current, _ = store.managed_snapshot("alice", "workspace-a", active.id)
    assert current.state == "revoked"
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    revoke_request = requests[-1]
    assert revoke_request.url.path == "/auth/revoke"
    assert parse_qs(revoke_request.content.decode()) == {"token": ["refresh-token"]}

    release_revoke.set()
    revoked, reported_status = await revoking
    assert revoked.state == "revoked"
    assert reported_status is expected_status
    with pytest.raises(EnergyError):
        store.credential("alice", active.id, site_id="home")
    assert "provider detail secret" not in str(revoked)
    store.close()
