from __future__ import annotations

import base64
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


def _provider(
    auth_method: str,
    *,
    extra_token_params: dict[str, str] | None = None,
    refresh_endpoint_auth_method: str | None = None,
) -> OAuthProvider:
    return OAuthProvider(
        authorization_endpoint="https://oauth.example/authorize",
        token_endpoint="https://oauth.example/token",
        client_id="confidential-client",
        redirect_uri="https://app.example/oauth/callback",
        scopes=("meter.read", "tariff.read"),
        client_secret="client-secret-private",
        token_endpoint_auth_method=auth_method,  # type: ignore[arg-type]
        protocol="oauth2_confidential",
        extra_token_params=extra_token_params or {},
        refresh_endpoint_auth_method=refresh_endpoint_auth_method,  # type: ignore[arg-type]
    )


def _account() -> ConnectedAccount:
    return ConnectedAccount(
        id="octopus-home",
        user_id="alice",
        site_id="home",
        toolkit="octopus-energy-account",
        auth=AuthConfig(scheme="oauth"),
        settings={"base_url": "https://provider.example"},
    )


def _store(
    tmp_path: Path,
    client: httpx.AsyncClient,
    clock: Clock | None = None,
) -> AuthStore:
    return AuthStore(tmp_path, Fernet.generate_key(), http=client, clock=clock)


@pytest.mark.parametrize("auth_method", ["client_secret_basic", "client_secret_post"])
def test_confidential_provider_requires_a_client_secret_auth_method(auth_method: str) -> None:
    provider = _provider(auth_method)
    assert provider.protocol == "oauth2_confidential"
    assert provider.token_endpoint_auth_method == auth_method

    kwargs = {
        "authorization_endpoint": provider.authorization_endpoint,
        "token_endpoint": provider.token_endpoint,
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "protocol": "oauth2_confidential",
    }
    with pytest.raises(ValueError, match="client_secret"):
        OAuthProvider(**kwargs, token_endpoint_auth_method=auth_method)
    with pytest.raises(ValueError, match="client-secret token auth"):
        OAuthProvider(**kwargs, client_secret="secret", token_endpoint_auth_method="none")


@pytest.mark.parametrize(
    "extra_token_params",
    [
        {"grant_type": "refresh_token"},
        {"CODE": "replacement-code"},
        {"client-id": "replacement-client"},
        {"client_secret": "private-secret"},
        {"clientSecret": "private-secret"},
        {"redirect_uri": "https://evil.example/callback"},
        {"refresh_token": "private-refresh"},
        {"refreshToken": "private-refresh"},
        {"custom_token": "private-token"},
        {"api_key": "private-key"},
    ],
)
def test_provider_rejects_reserved_and_secret_token_params(
    extra_token_params: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="token parameters"):
        _provider("client_secret_post", extra_token_params=extra_token_params)


def test_refresh_auth_override_requires_secret_and_home_assistant_rejects_override() -> None:
    with pytest.raises(ValueError, match="client_secret is required for refresh"):
        OAuthProvider(
            authorization_endpoint="https://oauth.example/authorize",
            token_endpoint="https://oauth.example/token",
            client_id="public-client",
            redirect_uri="https://app.example/oauth/callback",
            refresh_endpoint_auth_method="client_secret_basic",
        )

    with pytest.raises(ValueError, match="Home Assistant"):
        OAuthProvider(
            authorization_endpoint="https://ha.example/auth/authorize",
            token_endpoint="https://ha.example/auth/token",
            client_id="https://app.example",
            redirect_uri="https://app.example/oauth/callback",
            protocol="home_assistant",
            refresh_endpoint_auth_method="none",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_method", ["client_secret_basic", "client_secret_post"])
async def test_confidential_authorization_code_and_refresh_rotation(
    tmp_path: Path, auth_method: str
) -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    clock = Clock(now)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        if form["grant_type"] == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "first-access-private",
                    "refresh_token": "first-refresh-private",
                    "expires_in": 1,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access-private",
                "refresh_token": "rotated-refresh-private",
                "expires_in": 3600,
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = _store(tmp_path, client, clock)
    audience = "https://fleet-api.prd.na.vn.cloud.tesla.com"
    provider = _provider(auth_method, extra_token_params={"audience": audience})
    store.configure(_account(), "")

    authorization = store.begin_oauth("alice", "octopus-home", provider)
    query = parse_qs(urlparse(authorization.authorization_url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [provider.client_id]
    assert query["redirect_uri"] == [provider.redirect_uri]
    assert query["state"] == [authorization.state]
    assert query["scope"] == ["meter.read tariff.read"]
    assert "code_challenge" not in query
    assert "code_challenge_method" not in query
    assert "client_secret" not in query
    assert "audience" not in query
    assert provider.client_secret not in authorization.authorization_url

    transaction_row = store._db.execute(
        "SELECT transaction_blob FROM oauth_transactions"
    ).fetchone()
    transaction = store._decrypt_json(transaction_row["transaction_blob"])
    assert transaction["verifier"] is None
    assert transaction["provider"]["protocol"] == "oauth2_confidential"
    assert transaction["provider"]["client_secret"] == provider.client_secret
    assert transaction["provider"]["extra_token_params"] == {"audience": audience}
    assert provider.client_secret.encode() not in (tmp_path / "auth.sqlite3").read_bytes()

    completed = await store.complete_oauth(
        "alice", authorization.state, "one-time-code", provider.redirect_uri
    )
    assert completed.state == "active"
    assert store.credential("alice", "octopus-home", site_id="home") == "first-access-private"
    assert provider.client_secret not in completed.model_dump_json()
    assert "first-access-private" not in completed.model_dump_json()

    clock.value = now + timedelta(seconds=2)
    refreshed = await store.refresh("alice", "octopus-home")
    assert refreshed.state == "active"
    assert store.credential("alice", "octopus-home", site_id="home") == "rotated-access-private"

    for request, expected_grant in zip(
        requests, ("authorization_code", "refresh_token"), strict=True
    ):
        assert str(request.url) == provider.token_endpoint
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == [expected_grant]
        assert body["client_id"] == [provider.client_id]
        if expected_grant == "authorization_code":
            assert body["audience"] == [audience]
        else:
            assert "audience" not in body
        assert "client_secret" not in request.url.params
        if auth_method == "client_secret_basic":
            scheme, encoded = request.headers["authorization"].split(" ", 1)
            assert scheme == "Basic"
            assert base64.b64decode(encoded).decode() == (
                f"{provider.client_id}:{provider.client_secret}"
            )
            assert "client_secret" not in body
        else:
            assert body["client_secret"] == [provider.client_secret]
            assert "authorization" not in request.headers

    authorization_form = parse_qs(requests[0].content.decode())
    assert authorization_form["code"] == ["one-time-code"]
    assert authorization_form["redirect_uri"] == [provider.redirect_uri]
    assert "code_verifier" not in authorization_form
    refresh_form = parse_qs(requests[1].content.decode())
    assert refresh_form["refresh_token"] == ["first-refresh-private"]

    stored = store._decrypt_json(
        store._db.execute("SELECT secret_blob FROM accounts WHERE id = 'octopus-home'").fetchone()[
            "secret_blob"
        ]
    )
    persisted_provider = store._provider_load(stored["provider"])
    assert persisted_provider == provider
    assert persisted_provider.extra_token_params == {"audience": audience}
    assert persisted_provider != _provider(
        auth_method,
        extra_token_params={"audience": "https://fleet-api.prd.eu.vn.cloud.tesla.com"},
    )
    assert stored["refresh_token"] == "rotated-refresh-private"
    raw_database = (tmp_path / "auth.sqlite3").read_bytes()
    for secret in (
        provider.client_secret,
        "first-access-private",
        "first-refresh-private",
        "rotated-access-private",
        "rotated-refresh-private",
    ):
        assert secret.encode() not in raw_database
    assert set(store.redaction_values()) == {
        provider.client_secret,
        "rotated-access-private",
        "rotated-refresh-private",
    }
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_confidential_denial_is_sanitized_and_state_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    provider = _provider("client_secret_post")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=("access_denied client-secret-private one-time-code provider-details-private"),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = _store(tmp_path, client)
    store.configure(_account(), "")
    authorization = store.begin_oauth("alice", "octopus-home", provider)

    with pytest.raises(EnergyError) as denied:
        await store.complete_oauth(
            "alice", authorization.state, "one-time-code", provider.redirect_uri
        )
    assert denied.value.code == "oauth_exchange_failed"
    for private_value in (
        "client-secret-private",
        "one-time-code",
        "provider-details-private",
    ):
        assert private_value not in str(denied.value)

    with pytest.raises(EnergyError) as replayed:
        await store.complete_oauth(
            "alice", authorization.state, "one-time-code", provider.redirect_uri
        )
    assert replayed.value.code in {"oauth_state_invalid", "oauth_state_replayed"}
    assert provider.client_secret not in authorization.authorization_url
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_confidential_refresh_can_omit_code_exchange_client_auth(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 2, tzinfo=UTC)
    clock = Clock(now)
    requests: list[httpx.Request] = []
    audience = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = parse_qs(request.content.decode())
        if body["grant_type"] == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access-private",
                    "refresh_token": "initial-refresh-private",
                    "expires_in": 1,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"access_token": "refreshed-access-private", "expires_in": 3600},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = _store(tmp_path, client, clock)
    provider = _provider(
        "client_secret_post",
        extra_token_params={"audience": audience},
        refresh_endpoint_auth_method="none",
    )
    store.configure(_account(), "")
    authorization = store.begin_oauth("alice", "octopus-home", provider)
    completed = await store.complete_oauth(
        "alice", authorization.state, "one-time-code", provider.redirect_uri
    )
    assert provider.client_secret not in completed.model_dump_json()

    clock.value = now + timedelta(seconds=2)
    await store.refresh("alice", "octopus-home")
    code_body = parse_qs(requests[0].content.decode())
    assert code_body["client_secret"] == [provider.client_secret]
    assert code_body["audience"] == [audience]
    refresh_body = parse_qs(requests[1].content.decode())
    assert refresh_body == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["initial-refresh-private"],
        "client_id": [provider.client_id],
    }
    assert "authorization" not in requests[1].headers
    assert "client_secret" not in requests[1].url.params
    assert "audience" not in requests[1].url.params

    stored = store._decrypt_json(
        store._db.execute("SELECT secret_blob FROM accounts WHERE id = 'octopus-home'").fetchone()[
            "secret_blob"
        ]
    )
    persisted_provider = store._provider_load(stored["provider"])
    assert persisted_provider == provider
    assert persisted_provider.refresh_endpoint_auth_method == "none"
    raw_database = (tmp_path / "auth.sqlite3").read_bytes()
    for private_value in (
        provider.client_secret,
        "initial-access-private",
        "initial-refresh-private",
        "refreshed-access-private",
    ):
        assert private_value.encode() not in raw_database
    store.close()
    await client.aclose()
