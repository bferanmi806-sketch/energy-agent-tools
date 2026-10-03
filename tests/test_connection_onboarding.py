from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.connection_onboarding import OctopusConnectionService
from energy_agent_tools.models import EnergyError, Site


def _site(user_id: str = "user-1") -> Site:
    return Site(id="home", user_id=user_id, name="Home", timezone="UTC")


@pytest.mark.asyncio
async def test_successful_probe_persists_verified_encrypted_account(tmp_path: Path) -> None:
    credential = "octopus-fixture-secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.octopus.energy"
        assert request.url.path.endswith(
            "/electricity-meter-points/MPAN123/meters/SERIAL456/consumption/"
        )
        assert request.headers["authorization"].startswith("Basic ")
        decoded = base64.b64decode(request.headers["authorization"].removeprefix("Basic "))
        assert decoded.decode() == f"{credential}:"
        return httpx.Response(200, json={"results": []}, request=request)

    key = Fernet.generate_key()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AuthStore(tmp_path, key, http=client)
    try:
        service = OctopusConnectionService(store, client)
        outcome = await service.connect(
            user_id="user-1",
            site=_site(),
            credential=credential,
            mpan="MPAN123",
            serial_number="SERIAL456",
        )

        assert outcome["ok"] is True
        account = outcome["account"]
        assert account["id"].startswith("octopus-")
        assert account["site_id"] == "home"
        assert account["auth_scheme"] == "basic"
        assert account["verified"] is True
        assert outcome["health"]["status"] == "healthy"
        assert outcome["health"]["provider"] == "octopus"
        assert outcome["health"]["probe"] == "provider-read"
        assert "checked_at" in outcome["health"]
        assert credential not in json.dumps(outcome)

        stored = store.get_account("user-1", account["id"], "home")
        assert set(stored.settings) == {"mpan", "serial_number", "capability_bindings"}
        assert stored.settings["mpan"] == "MPAN123"
        assert stored.settings["serial_number"] == "SERIAL456"
        binding = stored.settings["capability_bindings"][0]
        assert binding["capability"] == "get_energy_consumption"
        assert binding["tool"] == "octopus_energy.get_consumption"
        assert binding["reviewed"] is True
        assert b"octopus-fixture-secret" not in store.path.read_bytes()

        store.close()
        reopened = AuthStore(tmp_path, key, http=client)
        try:
            assert (
                reopened.get_account("user-1", account["id"], "home").last_verified_at is not None
            )
            assert reopened.credential("user-1", account["id"], "home") == credential
        finally:
            reopened.close()
    finally:
        # AuthStore.close is idempotent and the service borrows both resources.
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_failed_probe_does_not_persist_an_active_account(tmp_path: Path) -> None:
    credential = "private-octopus-secret"
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, text=credential, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    try:
        service = OctopusConnectionService(store, client)
        with pytest.raises(EnergyError) as error:
            await service.connect(
                user_id="user-1",
                site=_site(),
                credential=credential,
                mpan="MPAN123",
                serial_number="SERIAL456",
            )

        assert error.value.code == "provider_verification_failed"
        assert credential not in str(error.value)
        assert requests == 1
        assert store.accounts("user-1") == []
        assert credential.encode() not in store.path.read_bytes()
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_foreign_site_is_rejected_before_request_or_storage(tmp_path: Path) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"results": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    try:
        service = OctopusConnectionService(store, client)
        with pytest.raises(EnergyError) as error:
            await service.connect(
                user_id="user-1",
                site=_site("user-2"),
                credential="octopus-secret",
                mpan="MPAN123",
                serial_number="SERIAL456",
            )

        assert error.value.code == "connection_site_forbidden"
        assert requests == 0
        assert store.accounts("user-1") == []
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["", "has whitespace", "has\ncontrol", "x" * 4097, "\u200e"])
async def test_invalid_credentials_are_rejected_before_request_or_storage(
    tmp_path: Path, credential: str
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"results": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    try:
        service = OctopusConnectionService(store, client)
        with pytest.raises(EnergyError) as error:
            await service.connect(
                user_id="user-1",
                site=_site(),
                credential=credential,
                mpan="MPAN123",
                serial_number="SERIAL456",
            )

        assert error.value.code == "credential_invalid"
        assert requests == 0
        assert store.accounts("user-1") == []
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mpan", "serial_number"),
    [("bad/mpan", "SERIAL456"), ("MPAN123", "bad/serial")],
)
async def test_invalid_meter_identifiers_are_rejected_before_request_or_storage(
    tmp_path: Path, mpan: str, serial_number: str
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"results": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
    try:
        service = OctopusConnectionService(store, client)
        with pytest.raises(EnergyError) as error:
            await service.connect(
                user_id="user-1",
                site=_site(),
                credential="octopus-secret",
                mpan=mpan,
                serial_number=serial_number,
            )

        assert error.value.code == "connection_settings_invalid"
        assert requests == 0
        assert store.accounts("user-1") == []
    finally:
        store.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_reuses_connection_and_failed_replacement_preserves_secret(tmp_path: Path):
    def handler(request: httpx.Request):
        key = base64.b64decode(request.headers["authorization"][6:]).decode()
        return httpx.Response(401 if key == "bad-key:" else 200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path, Fernet.generate_key(), http=client)
        try:
            service = OctopusConnectionService(store, client)
            args = {
                "user_id": "user-1",
                "site": _site(),
                "mpan": "1234567890123",
                "serial_number": "TEST123",
            }
            first = await service.connect(**args, credential="first-key")
            retry = await service.connect(**args, credential="second-key")
            assert first["account"]["id"] == retry["account"]["id"]
            assert len(store.accounts("user-1")) == 1
            with pytest.raises(EnergyError, match="Provider verification failed"):
                await service.connect(**args, credential="bad-key")
            assert store.credential("user-1", first["account"]["id"], "home") == "second-key"
            other = await service.connect(
                **{**args, "site": _site().model_copy(update={"id": "workshop"})},
                credential="second-key",
            )
            assert other["account"]["id"] != first["account"]["id"]
        finally:
            store.close()
