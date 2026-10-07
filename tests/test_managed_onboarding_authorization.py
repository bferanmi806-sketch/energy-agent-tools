from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.connection_onboarding import (
    OctopusConnectionService,
    map_managed_connection,
)
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError, Site
from energy_agent_tools.onboarding import provider_settings

USER_ID = "managed-onboarding-owner"
WORKSPACE_ID = "managed-onboarding-workspace"
CREDENTIAL = "octopus-management-secret"
MPAN = "1234567890123"
SERIAL = "METER123"
SITE = Site(id="managed-home", user_id=USER_ID, name="Home", timezone="UTC")


def _probe_response(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "api.octopus.energy"
    assert request.url.path.endswith(
        f"/electricity-meter-points/{MPAN}/meters/{SERIAL}/consumption/"
    )
    return httpx.Response(200, json={"results": []}, request=request)


def _pending_account() -> ConnectedAccount:
    return ConnectedAccount(
        id="managed-octopus-authorization-fixture",
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        toolkit="octopus-energy-account",
        auth=AuthConfig(scheme="basic"),
        settings=provider_settings("octopus", {"mpan": MPAN, "serial_number": SERIAL}),
        state="pending_mapping",
        enabled=False,
        last_verified_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_stage_denial_at_entry_does_not_probe_or_persist(tmp_path: Path) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _probe_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=client)

        def deny() -> None:
            raise EnergyError(
                "workspace_key_revoked", "Management authorization is no longer valid."
            )

        try:
            service = OctopusConnectionService(store, client, authorize_write=deny)
            with pytest.raises(EnergyError) as error:
                await service.stage_managed(
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    credential=CREDENTIAL,
                    mpan=MPAN,
                    serial_number=SERIAL,
                )

            assert error.value.code == "workspace_key_revoked"
            assert requests == 0
            assert store.workspace_accounts(USER_ID, WORKSPACE_ID) == []
            assert CREDENTIAL.encode() not in store.path.read_bytes()
        finally:
            store.close()


@pytest.mark.asyncio
async def test_stage_rechecks_authorization_after_probe_before_storing_credential(
    tmp_path: Path,
) -> None:
    authorized = True
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authorized, requests
        requests += 1
        response = _probe_response(request)
        authorized = False
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=client)

        def authorize() -> None:
            if not authorized:
                raise EnergyError(
                    "workspace_key_revoked", "Management authorization is no longer valid."
                )

        try:
            service = OctopusConnectionService(store, client, authorize_write=authorize)
            with pytest.raises(EnergyError) as error:
                await service.stage_managed(
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    credential=CREDENTIAL,
                    mpan=MPAN,
                    serial_number=SERIAL,
                )

            assert error.value.code == "workspace_key_revoked"
            assert requests == 1
            assert store.workspace_accounts(USER_ID, WORKSPACE_ID) == []
            assert CREDENTIAL.encode() not in store.path.read_bytes()
        finally:
            store.close()


@pytest.mark.asyncio
async def test_map_denial_at_entry_does_not_probe_or_activate(tmp_path: Path) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _probe_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=client)
        account = store.stage_managed(_pending_account(), CREDENTIAL)

        def deny() -> None:
            raise EnergyError(
                "workspace_key_revoked", "Management authorization is no longer valid."
            )

        try:
            with pytest.raises(EnergyError) as error:
                await map_managed_connection(
                    store,
                    client,
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    connection_id=account.id,
                    site=SITE,
                    authorize_write=deny,
                )

            assert error.value.code == "workspace_key_revoked"
            assert requests == 0
            pending, revision = store.managed_snapshot(USER_ID, WORKSPACE_ID, account.id)
            assert pending.state == "pending_mapping" and pending.site_id is None
            assert revision == 1
            assert store.pending_credential(USER_ID, WORKSPACE_ID, account.id) == CREDENTIAL
        finally:
            store.close()


@pytest.mark.asyncio
async def test_map_rechecks_authorization_after_probe_before_activation(tmp_path: Path) -> None:
    authorized = True
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authorized, requests
        requests += 1
        response = _probe_response(request)
        authorized = False
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=client)
        account = store.stage_managed(_pending_account(), CREDENTIAL)

        def authorize() -> None:
            if not authorized:
                raise EnergyError(
                    "workspace_key_revoked", "Management authorization is no longer valid."
                )

        try:
            with pytest.raises(EnergyError) as error:
                await map_managed_connection(
                    store,
                    client,
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    connection_id=account.id,
                    site=SITE,
                    authorize_write=authorize,
                )

            assert error.value.code == "workspace_key_revoked"
            assert requests == 1
            pending, revision = store.managed_snapshot(USER_ID, WORKSPACE_ID, account.id)
            assert pending.state == "pending_mapping" and pending.site_id is None
            assert revision == 1
            assert store.pending_credential(USER_ID, WORKSPACE_ID, account.id) == CREDENTIAL
        finally:
            store.close()


@pytest.mark.asyncio
async def test_managed_stage_and_map_succeed_with_valid_authorization(tmp_path: Path) -> None:
    requests = 0
    authorization_checks = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _probe_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=client)

        def authorize() -> None:
            nonlocal authorization_checks
            authorization_checks += 1

        try:
            service = OctopusConnectionService(store, client, authorize_write=authorize)
            staged = await service.stage_managed(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                credential=CREDENTIAL,
                mpan=MPAN,
                serial_number=SERIAL,
            )
            connection_id = str(staged["account"]["id"])
            assert staged["account"]["state"] == "pending_mapping"
            assert store.pending_credential(USER_ID, WORKSPACE_ID, connection_id) == CREDENTIAL

            mapped = await map_managed_connection(
                store,
                client,
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=SITE,
                authorize_write=authorize,
            )

            assert mapped["account"]["state"] == "active"
            assert mapped["account"]["site_id"] == SITE.id
            assert authorization_checks == 4
            assert requests == 2
        finally:
            store.close()
