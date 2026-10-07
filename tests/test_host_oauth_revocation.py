from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.managed_oauth import HomeAssistantOAuthConfiguration


@pytest.mark.asyncio
async def test_revoked_manager_cannot_publish_oauth_after_provider_probe(tmp_path):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    alternate_manager = control.create_key(
        owner.user.id,
        owner.workspace.id,
        "Alternate manager",
        access=ManageKeyAccess(),
    )
    revoked_key = False
    revocation_attempts: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal revoked_key
        if request.url.path == "/auth/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-private-access",
                    "refresh_token": "synthetic-private-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                request=request,
            )
        if request.url.path == "/api/states/sensor.power":
            control.revoke_key(owner.user.id, owner.workspace.id, owner.key.key.id)
            revoked_key = True
            return httpx.Response(
                200,
                json={
                    "entity_id": "sensor.power",
                    "state": "1.75",
                    "attributes": {
                        "unit_of_measurement": "kW",
                        "state_class": "measurement",
                    },
                    "last_updated": datetime.now(UTC).isoformat(),
                },
                request=request,
            )
        if request.url.path == "/auth/revoke":
            form = parse_qs(request.content.decode())
            revocation_attempts.extend(form.get("token", []))
            return httpx.Response(503, request=request)
        raise AssertionError(f"Unexpected provider request: {request.url}")

    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=upstream)
    agent.auth_store = vault
    configuration = HomeAssistantOAuthConfiguration(
        id="home",
        name="Home",
        base_url="https://home.example.test",
        client_id="https://energy.example.test",
        redirect_uri="https://energy.example.test/callback",
    )
    host = create_host(
        agent,
        {},
        control_store=control,
        managed_workspaces=True,
        managed_oauth_configurations=(configuration,),
    )
    auth = {"Authorization": "Bearer " + owner.key.token}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost"
        ) as client:
            begun = await client.post(
                "/workspace/authorizations",
                headers=auth,
                json={
                    "configuration_id": configuration.id,
                    "entity_id": "sensor.power",
                    "mapping": {
                        "telemetry_role": "current_power",
                        "unit": "kW",
                        "quantity_shape": "instantaneous",
                        "measurement_kind": "metered",
                    },
                },
            )
            assert begun.status_code == 201, begun.text
            state = begun.json()["authorization"]["state"]

            completed = await client.post(
                "/workspace/authorizations/complete",
                headers=auth,
                json={
                    "configuration_id": configuration.id,
                    "state": state,
                    "code": "synthetic-code",
                },
            )

        assert revoked_key
        assert completed.status_code == 403, completed.text
        assert completed.json()["error"]["code"] == "workspace_forbidden"
        assert vault.workspace_accounts(owner.user.id, owner.workspace.id) == []
        assert revocation_attempts == ["synthetic-private-refresh"]
        assert (
            vault.pending_managed_oauth_cleanup(owner.user.id, owner.workspace.id, configuration.id)
            == 1
        )
        assert "synthetic-private-access" not in completed.text
        assert "synthetic-private-refresh" not in completed.text
        assert alternate_manager.token not in completed.text
    finally:
        vault.close()
        await agent.close()
        await upstream.aclose()
        control.close()
