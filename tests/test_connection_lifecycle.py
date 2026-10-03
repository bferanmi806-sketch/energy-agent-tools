from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.connection_lifecycle import OctopusConnectionLifecycle
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError

_USER_ID = "user-1"
_SITE_ID = "home"
_CONNECTION_ID = "octopus-home"
_CREDENTIAL = "octopus-private-fixture-key"


def _account(*, toolkit: str = "octopus-energy-account") -> ConnectedAccount:
    return ConnectedAccount(
        id=_CONNECTION_ID,
        user_id=_USER_ID,
        site_id=_SITE_ID,
        toolkit=toolkit,
        auth=AuthConfig(scheme="basic"),
        settings={"mpan": "MPAN123", "serial_number": "SERIAL456"},
    )


def _setup(
    tmp_path: Path,
    handler: httpx.AsyncBaseTransport,
    *,
    toolkit: str = "octopus-energy-account",
) -> tuple[OctopusConnectionLifecycle, AuthStore, httpx.AsyncClient, bytes]:
    key = Fernet.generate_key()
    client = httpx.AsyncClient(transport=handler)
    store = AuthStore(tmp_path, key, http=client)
    store.configure(_account(toolkit=toolkit), _CREDENTIAL)
    return OctopusConnectionLifecycle(store, client), store, client, key


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"results": []}, request=request)


@pytest.mark.asyncio
async def test_verify_returns_healthy_public_account_and_utc_health(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "api.octopus.energy"
        return _ok_response(request)

    service, store, client, _ = _setup(tmp_path, httpx.MockTransport(handler))
    try:
        result = await service.verify(
            user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID
        )

        assert result["ok"] is True
        assert result["account"]["verified"] is True
        assert result["health"]["status"] == "healthy"
        assert result["health"]["provider"] == "octopus"
        assert result["health"]["probe"] == "provider-read"
        assert result["health"]["message"] == "Provider read succeeded."
        checked_at = datetime.fromisoformat(result["health"]["checked_at"].replace("Z", "+00:00"))
        assert checked_at.tzinfo is not None
        assert checked_at.utcoffset() == timedelta(0)
        assert _CREDENTIAL not in str(result)
        assert store.get_account(_USER_ID, _CONNECTION_ID, _SITE_ID).last_verified_at is not None
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_failure_keeps_verified_time_and_credential(tmp_path: Path) -> None:
    fail = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(401, text=f"rejected {_CREDENTIAL}", request=request)
        return _ok_response(request)

    service, store, client, _ = _setup(tmp_path, httpx.MockTransport(handler))
    try:
        success = await service.verify(
            user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID
        )
        previous_verified_at = store.get_account(
            _USER_ID, _CONNECTION_ID, _SITE_ID
        ).last_verified_at
        assert previous_verified_at is not None

        fail = True
        result = await service.verify(
            user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID
        )

        current = store.get_account(_USER_ID, _CONNECTION_ID, _SITE_ID)
        assert result["ok"] is True
        assert result["account"] == success["account"]
        assert result["health"]["status"] == "unhealthy"
        assert result["health"]["probe"] == "provider-read"
        assert result["health"]["message"] == "Provider verification failed."
        assert current.last_verified_at == previous_verified_at
        assert store.credential(_USER_ID, _CONNECTION_ID, _SITE_ID) == _CREDENTIAL
        assert _CREDENTIAL not in str(result)
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "site_id"),
    [("other-user", _SITE_ID), (_USER_ID, "other-site")],
)
async def test_wrong_user_or_site_is_rejected_before_provider_request(
    tmp_path: Path, user_id: str, site_id: str
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _ok_response(request)

    service, store, client, _ = _setup(tmp_path, httpx.MockTransport(handler))
    try:
        with pytest.raises(EnergyError) as error:
            await service.verify(user_id=user_id, site_id=site_id, connection_id=_CONNECTION_ID)
        assert error.value.code == "account_forbidden"
        assert requests == 0
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_unsupported_toolkit_is_rejected_before_provider_request(tmp_path: Path) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _ok_response(request)

    service, store, client, _ = _setup(
        tmp_path, httpx.MockTransport(handler), toolkit="home-assistant"
    )
    try:
        with pytest.raises(EnergyError) as error:
            await service.verify(user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID)
        assert error.value.code == "unsupported_provider"
        assert requests == 0
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_revoked_connection_refuses_verification_without_request(tmp_path: Path) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _ok_response(request)

    service, store, client, _ = _setup(tmp_path, httpx.MockTransport(handler))
    try:
        service.disconnect(user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID)
        with pytest.raises(EnergyError) as error:
            await service.verify(user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID)
        assert error.value.code == "connection_revoked"
        assert requests == 0
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_disconnect_removes_credential_and_revocation_survives_reopen(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(request)

    service, store, client, key = _setup(tmp_path, httpx.MockTransport(handler))
    result = service.disconnect(user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID)
    assert result["ok"] is True
    assert result["account"]["state"] == "revoked"
    assert result["account"]["enabled"] is False
    assert store.redaction_values() == []
    store.close()

    reopened = AuthStore(tmp_path, key, http=client)
    try:
        account = reopened.get_account(_USER_ID, _CONNECTION_ID, _SITE_ID)
        assert account.state == "revoked"
        assert not account.enabled
        with pytest.raises(EnergyError) as error:
            reopened.credential(_USER_ID, _CONNECTION_ID, _SITE_ID)
        assert error.value.code == "connection_disabled"
        assert reopened.redaction_values() == []
    finally:
        reopened.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_verification_racing_disconnect_cannot_reactivate_account(tmp_path: Path) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return _ok_response(request)

    service, store, client, _ = _setup(tmp_path, httpx.MockTransport(handler))
    try:
        verification = asyncio.create_task(
            service.verify(user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID)
        )
        await request_started.wait()

        disconnected = service.disconnect(
            user_id=_USER_ID, site_id=_SITE_ID, connection_id=_CONNECTION_ID
        )
        release_request.set()
        with pytest.raises(EnergyError) as error:
            await verification

        current = store.get_account(_USER_ID, _CONNECTION_ID, _SITE_ID)
        assert disconnected["account"]["state"] == "revoked"
        assert error.value.code == "connection_changed"
        assert current.state == "revoked"
        assert current.enabled is False
        assert store.redaction_values() == []
    finally:
        store.close()
        await client.aclose()
