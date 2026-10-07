from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from test_host_connection_onboarding import PAYLOAD
from test_hosting import _Lifespan

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.models import AuthConfig, ConnectedAccount


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stage", "map", "verify"])
async def test_revoked_manager_cannot_save_after_provider_probe(tmp_path: Path, operation: str):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    revoke = operation == "stage"
    probes = 0

    def provider(request: httpx.Request):
        nonlocal probes
        probes += 1
        if revoke:
            control.revoke_key(owner.user.id, owner.workspace.id, owner.key.key.id)
        return httpx.Response(200, json={"results": []})

    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent.auth_store = vault
    host = create_host(agent, {}, control_store=control, managed_workspaces=True)
    auth = {"Authorization": "Bearer " + owner.key.token}
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            response = await client.post("/workspace/connections", headers=auth, json=PAYLOAD)
            if operation != "stage":
                assert response.status_code == 201, response.text
                connection_id = response.json()["account"]["id"]
                before, revision = vault.managed_snapshot(
                    owner.user.id, owner.workspace.id, connection_id
                )
                revoke = operation == "map"
                response = await client.post(
                    f"/workspace/connections/{connection_id}/map",
                    headers=auth,
                    json={"site_id": site.id},
                )
                if operation == "verify":
                    assert response.status_code == 200, response.text
                    before, revision = vault.managed_snapshot(
                        owner.user.id, owner.workspace.id, connection_id
                    )
                    revoke = True
                    response = await client.post(
                        f"/workspace/connections/{connection_id}/verify", headers=auth, json={}
                    )
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "workspace_forbidden"
            assert PAYLOAD["credential"] not in response.text
            if operation == "stage":
                assert vault.workspace_accounts(owner.user.id, owner.workspace.id) == []
            else:
                after, current_revision = vault.managed_snapshot(
                    owner.user.id, owner.workspace.id, connection_id
                )
                assert after == before and current_revision == revision
                assert after.state == ("pending_mapping" if operation == "map" else "active")
            assert probes == {"stage": 1, "map": 2, "verify": 3}[operation]
    finally:
        await upstream.aclose()
        await agent.close()
        control.close()


@pytest.mark.asyncio
async def test_home_assistant_health_does_not_write_after_manager_revocation(tmp_path: Path):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    probes = 0

    def provider(request: httpx.Request):
        nonlocal probes
        probes += 1
        control.revoke_key(owner.user.id, owner.workspace.id, owner.key.key.id)
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.power",
                "state": "1.75",
                "attributes": {"unit_of_measurement": "kW", "state_class": "measurement"},
                "last_updated": datetime.now(UTC).isoformat(),
            },
        )

    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent.auth_store = vault
    pending = vault.stage_managed(
        ConnectedAccount(
            id="fixture-home",
            user_id=owner.user.id,
            workspace_id=owner.workspace.id,
            toolkit="home-assistant",
            auth=AuthConfig(scheme="bearer"),
            settings={"base_url": "https://home.example.test", "entity_id": "sensor.power"},
            state="pending_mapping",
            enabled=False,
            last_verified_at=datetime.now(UTC),
        ),
        "synthetic-home-token",
    )
    vault.activate_managed(
        owner.user.id,
        owner.workspace.id,
        pending.id,
        site=site,
        expected_version=1,
        verified_at=datetime.now(UTC),
    )
    before, revision = vault.managed_snapshot(owner.user.id, owner.workspace.id, pending.id)
    host = create_host(agent, {}, control_store=control, managed_workspaces=True)
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            response = await client.post(
                f"/workspace/connections/{pending.id}/verify",
                headers={"Authorization": "Bearer " + owner.key.token},
                json={},
            )
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "workspace_forbidden"
            after, current_revision = vault.managed_snapshot(
                owner.user.id, owner.workspace.id, pending.id
            )
            assert after == before and current_revision == revision
            assert probes == 1
    finally:
        await upstream.aclose()
        await agent.close()
        control.close()
