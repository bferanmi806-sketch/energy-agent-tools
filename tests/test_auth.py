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
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def account(
    identifier: str = "octopus-home",
    *,
    user_id: str = "alice",
    site_id: str | None = "home",
    scheme: str = "bearer",
) -> ConnectedAccount:
    return ConnectedAccount(
        id=identifier,
        user_id=user_id,
        site_id=site_id,
        toolkit="octopus-energy-account",
        auth=AuthConfig(scheme=scheme),
        settings={"base_url": "https://provider.example"},
    )


def make_store(
    tmp_path: Path,
    *,
    key: bytes | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
) -> tuple[AuthStore, bytes]:
    key = key or Fernet.generate_key()
    return AuthStore(tmp_path, key, http=http, clock=clock), key


def test_encrypted_scoped_storage_and_lifecycle(tmp_path: Path) -> None:
    store, key = make_store(tmp_path)
    store.configure(account(), "meter-token-never-in-db")

    raw_db = (tmp_path / "auth.sqlite3").read_bytes()
    assert b"meter-token-never-in-db" not in raw_db
    stored = store.get_account("alice", "octopus-home", site_id="home")
    assert stored.auth.secret_id == "octopus-home"
    assert "meter-token-never-in-db" not in stored.model_dump_json()
    assert store.redaction_values() == ["meter-token-never-in-db"]

    with pytest.raises(EnergyError, match="outside this user/site scope"):
        store.get_account("mallory", "octopus-home", site_id="home")
    with pytest.raises(EnergyError, match="outside this user/site scope"):
        store.credential("alice", "octopus-home", site_id="other")

    disabled = store.disable("alice", "octopus-home", site_id="home")
    assert disabled.state == "disabled"
    assert not disabled.enabled
    with pytest.raises(EnergyError, match="disabled or revoked"):
        store.credential("alice", "octopus-home", site_id="home")

    reconnected = store.reconnect("alice", "octopus-home", site_id="home")
    assert reconnected.state == "active"
    assert reconnected.enabled
    verified = store.verify("alice", "octopus-home", site_id="home")
    assert verified.last_verified_at is None
    assert store.credential("alice", "octopus-home", site_id="home") == "meter-token-never-in-db"

    wrong_key_store = AuthStore(tmp_path, Fernet.generate_key())
    with pytest.raises(EnergyError, match="cannot be decrypted"):
        wrong_key_store.credential("alice", "octopus-home", site_id="home")
    wrong_key_store.close()

    revoked = store.revoke("alice", "octopus-home", site_id="home")
    assert revoked.state == "revoked"
    assert not revoked.enabled
    assert store.redaction_values() == []
    with pytest.raises(EnergyError, match="disabled or revoked"):
        store.credential("alice", "octopus-home", site_id="home")
    with pytest.raises(EnergyError, match="must be configured again"):
        store.reconnect("alice", "octopus-home", site_id="home")

    reopened = AuthStore(tmp_path, key)
    with pytest.raises(EnergyError, match="disabled or revoked"):
        reopened.credential("alice", "octopus-home", site_id="home")
    reopened.close()


@pytest.mark.asyncio
async def test_provider_verification_is_explicit_and_scoped(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.configure(account(), "provider-token")
    seen: list[tuple[str, str]] = []

    async def probe(connection: ConnectedAccount, credential: str) -> bool:
        seen.append((connection.id, credential))
        return True

    verified = await store.verify_provider("alice", "octopus-home", probe, site_id="home")
    assert verified.last_verified_at is not None
    assert seen == [("octopus-home", "provider-token")]
    timestamp = verified.last_verified_at

    async def failed_probe(connection: ConnectedAccount, credential: str) -> bool:
        raise RuntimeError(f"provider leaked {credential}")

    with pytest.raises(EnergyError, match="Provider verification failed") as caught:
        await store.verify_provider("alice", "octopus-home", failed_probe, site_id="home")
    assert "provider-token" not in str(caught.value)
    assert store.get_account("alice", "octopus-home", site_id="home").last_verified_at == timestamp

    store.disable("alice", "octopus-home", site_id="home")
    with pytest.raises(EnergyError, match="disabled or revoked"):
        await store.verify_provider("alice", "octopus-home", probe, site_id="home")


def test_invalid_master_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="valid Fernet key"):
        AuthStore(tmp_path, b"too-short")
    with pytest.raises(TypeError, match="bytes"):
        AuthStore(tmp_path, "not-bytes")  # type: ignore[arg-type]


def test_oauth_provider_rejects_unsafe_redirect_and_parameter_overrides() -> None:
    kwargs = {
        "authorization_endpoint": "https://oauth.example/authorize",
        "token_endpoint": "https://oauth.example/token",
        "client_id": "client",
        "redirect_uri": "https://app.example/callback",
    }
    with pytest.raises(ValueError, match="redirect_uri"):
        OAuthProvider(**{**kwargs, "redirect_uri": "http://app.example/callback"})
    with pytest.raises(ValueError, match="protocol fields"):
        OAuthProvider(**{**kwargs, "extra_authorization_params": {"state": "attacker"}})
    with pytest.raises(ValueError, match="protocol fields"):
        OAuthProvider(**{**kwargs, "extra_authorization_params": {"CLIENT_ID": "attacker"}})


@pytest.mark.asyncio
async def test_oauth_pkce_callback_encryption_and_state_replay(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = Clock(now)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "oauth-access-never-public",
                "refresh_token": "oauth-refresh-never-public",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store, _ = make_store(tmp_path, clock=clock, http=client)
    store.configure(account(scheme="oauth"), "")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="energy-client",
        redirect_uri="https://app.example/oauth/callback",
        scopes=("meter.read", "tariff.read"),
    )

    request = store.begin_oauth("alice", "octopus-home", provider)
    pending = store.get_account("alice", "octopus-home", site_id="home")
    assert pending.state == "pending"
    assert not pending.enabled
    with pytest.raises(EnergyError, match="awaiting OAuth"):
        store.credential("alice", "octopus-home", site_id="home")
    query = parse_qs(urlparse(request.authorization_url).query)
    assert query["state"] == [request.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [provider.redirect_uri]
    assert "oauth-access-never-public" not in request.authorization_url

    completed = await store.complete_oauth(
        "alice", request.state, "one-time-code", provider.redirect_uri
    )
    assert completed.auth.secret_id == "octopus-home"
    assert completed.state == "active"
    assert store.credential("alice", "octopus-home", site_id="home") == "oauth-access-never-public"
    assert "oauth-access-never-public" not in completed.model_dump_json()
    assert "oauth-refresh-never-public" not in (tmp_path / "auth.sqlite3").read_bytes().decode(
        "latin1"
    )
    assert requests[0].url == provider.token_endpoint
    body = parse_qs(requests[0].content.decode())
    assert body["code"] == ["one-time-code"]
    assert body["code_verifier"]
    assert hashlib.sha256(body["code_verifier"][0].encode()).digest()

    with pytest.raises(EnergyError, match="invalid or expired"):
        await store.complete_oauth("alice", request.state, "replayed", provider.redirect_uri)
    await client.aclose()


@pytest.mark.asyncio
async def test_oauth_redirect_and_expiry_checks(tmp_path: Path) -> None:
    now = datetime(2026, 2, 1, tzinfo=UTC)
    clock = Clock(now)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"access_token": "unused"},
                request=request,
            )
        )
    )
    store, _ = make_store(tmp_path, clock=clock, http=client)
    store.configure(account(scheme="oauth"), "initial-token")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
        state_ttl_seconds=10,
    )

    mismatch = store.begin_oauth("alice", "octopus-home", provider)
    with pytest.raises(EnergyError, match="does not match"):
        await store.complete_oauth("alice", mismatch.state, "code", "https://evil.example/callback")
    clock.value = now + timedelta(seconds=11)
    with pytest.raises(EnergyError, match="invalid or expired"):
        await store.complete_oauth("alice", mismatch.state, "code", provider.redirect_uri)

    fresh = store.begin_oauth("alice", "octopus-home", provider)
    with pytest.raises(EnergyError, match="invalid or expired"):
        await store.complete_oauth("mallory", fresh.state, "code", provider.redirect_uri)
    await client.aclose()


@pytest.mark.asyncio
async def test_oauth_refresh_retains_refresh_token_and_sanitizes_failure(tmp_path: Path) -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    clock = Clock(now)
    calls: list[dict[str, str]] = []
    responses = [
        {"access_token": "first-access", "refresh_token": "long-lived-refresh", "expires_in": 1},
        {"access_token": "second-access", "expires_in": 3600},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append({key: values[0] for key, values in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json=responses.pop(0), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store, _ = make_store(tmp_path, clock=clock, http=client)
    store.configure(account(scheme="oauth"), "initial-token")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
    )
    auth_request = store.begin_oauth("alice", "octopus-home", provider)
    await store.complete_oauth("alice", auth_request.state, "code", provider.redirect_uri)
    clock.value = now + timedelta(seconds=2)
    refreshed = await store.refresh("alice", "octopus-home")
    assert refreshed.state == "active"
    assert store.credential("alice", "octopus-home", site_id="home") == "second-access"
    assert calls[1]["grant_type"] == "refresh_token"
    assert calls[1]["refresh_token"] == "long-lived-refresh"
    assert "long-lived-refresh" in store.redaction_values()

    async def failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text="provider secret=private-token",
            request=request,
        )

    failing = httpx.AsyncClient(transport=httpx.MockTransport(failure))
    store.http = failing
    clock.value = now + timedelta(hours=2)
    with pytest.raises(EnergyError) as caught:
        await store.refresh("alice", "octopus-home")
    assert caught.value.code == "oauth_exchange_failed"
    assert "private-token" not in str(caught.value)
    await client.aclose()
    await failing.aclose()


@pytest.mark.asyncio
async def test_pending_oauth_cannot_reactivate_after_revoke_during_exchange(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "late-token"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store, _ = make_store(tmp_path, http=client)
    store.configure(account(scheme="oauth"), "")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
    )
    auth_request = store.begin_oauth("alice", "octopus-home", provider)
    task = asyncio.create_task(
        store.complete_oauth("alice", auth_request.state, "code", provider.redirect_uri)
    )
    await started.wait()
    revoked = store.revoke("alice", "octopus-home", site_id="home")
    assert revoked.state == "revoked"
    release.set()
    with pytest.raises(EnergyError, match="Connection changed") as caught:
        await task
    assert caught.value.code == "connection_changed"
    assert store.get_account("alice", "octopus-home", site_id="home").state == "revoked"
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_is_serialized_and_cannot_restore_after_revoke(tmp_path: Path) -> None:
    now = datetime(2026, 4, 1, tzinfo=UTC)
    clock = Clock(now)
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    refresh_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access",
                    "refresh_token": "single-use-refresh",
                    "expires_in": 1,
                },
                request=request,
            )
        refresh_calls += 1
        refresh_started.set()
        await refresh_release.wait()
        return httpx.Response(200, json={"access_token": "late-refresh"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store, _ = make_store(tmp_path, clock=clock, http=client)
    store.configure(account(scheme="oauth"), "")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
    )
    auth_request = store.begin_oauth("alice", "octopus-home", provider)
    await store.complete_oauth("alice", auth_request.state, "code", provider.redirect_uri)
    clock.value = now + timedelta(seconds=2)
    task = asyncio.create_task(store.refresh("alice", "octopus-home"))
    await refresh_started.wait()
    store.revoke("alice", "octopus-home", site_id="home")
    refresh_release.set()
    with pytest.raises(EnergyError, match="Connection changed") as caught:
        await task
    assert caught.value.code == "connection_changed"
    assert refresh_calls == 1
    assert store.get_account("alice", "octopus-home", site_id="home").state == "revoked"

    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_lock_prevents_duplicate_single_use_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    clock = Clock(now)
    refresh_calls = 0
    refresh_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access",
                    "refresh_token": "single-use-refresh",
                    "expires_in": 1,
                },
                request=request,
            )
        refresh_calls += 1
        refresh_started.set()
        await release.wait()
        return httpx.Response(
            200, json={"access_token": "refreshed", "expires_in": 3600}, request=request
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store, _ = make_store(tmp_path, clock=clock, http=client)
    store.configure(account(scheme="oauth"), "")
    provider = OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="client",
        redirect_uri="https://app.example/callback",
    )
    auth_request = store.begin_oauth("alice", "octopus-home", provider)
    await store.complete_oauth("alice", auth_request.state, "code", provider.redirect_uri)
    clock.value = now + timedelta(seconds=2)
    first = asyncio.create_task(store.refresh("alice", "octopus-home"))
    await refresh_started.wait()
    second = asyncio.create_task(store.refresh("alice", "octopus-home"))
    release.set()
    results = await asyncio.gather(first, second)
    assert refresh_calls == 1
    assert all(result.state == "active" for result in results)
    assert store.credential("alice", "octopus-home", site_id="home") == "refreshed"
    await client.aclose()
