from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.managed_oauth import HomeAssistantOAuthConfiguration


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration_removed", [True, False], ids=["removed", "changed"])
async def test_changed_oauth_configuration_denies_execution_and_provider_io(
    tmp_path: Path, configuration_removed: bool
):
    requests = []
    provider_unhealthy = False

    def provider(request):
        requests.append(request)
        if request.url.path == "/auth/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"]:
                assert form["refresh_token"] == ["synthetic-private-refresh"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "synthetic-refreshed-access",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            assert form["code"] == ["synthetic-code"]
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-private-access",
                    "refresh_token": "synthetic-private-refresh",
                    "token_type": "Bearer",
                    "expires_in": 30,
                },
            )
        if provider_unhealthy:
            return httpx.Response(401, json={"message": "Private provider diagnostic"})
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.power",
                "state": "1.75",
                "attributes": {"unit_of_measurement": "kW", "state_class": "measurement"},
                "last_updated": datetime.now(UTC).isoformat(),
            },
        )

    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault_key = Fernet.generate_key()
    vault = AuthStore(tmp_path / "vault", vault_key, http=upstream)
    agent.auth_store = vault
    configuration = HomeAssistantOAuthConfiguration(
        id="home",
        name="Home",
        base_url="https://home.example.test",
        client_id="https://energy.example.test",
        redirect_uri="https://energy.example.test/callback",
    )
    auth = {"Authorization": "Bearer " + owner.key.token}
    host = create_host(
        agent,
        {},
        control_store=control,
        managed_workspaces=True,
        managed_oauth_configurations=(configuration,),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost"
        ) as client:
            begun = await client.post(
                "/workspace/authorizations",
                headers=auth,
                json={
                    "configuration_id": "home",
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
                json={"configuration_id": "home", "state": state, "code": "synthetic-code"},
            )
            assert completed.status_code == 201, completed.text
            account = completed.json()["account"]
            assert account["state"] == "pending_mapping" and account["site_id"] is None
            assert "synthetic-private" not in completed.text
            site = (
                await client.post(
                    "/workspace/sites", headers=auth, json={"name": "Home", "timezone": "UTC"}
                )
            ).json()["site"]
            path = f"/workspace/connections/{account['id']}"
            mapped = await client.post(path + "/map", headers=auth, json={"site_id": site["id"]})
            assert mapped.status_code == 200, mapped.text
            created_session = await client.post(
                "/sessions", headers=auth, json={"site_id": site["id"]}
            )
            assert created_session.status_code == 200, created_session.text
            session = created_session.json()["session_id"]
            before_foreign_entity = len(requests)
            for tool_name, arguments in [
                ("home_assistant.get_state", {"entity_id": "sensor.other_site"}),
                (
                    "home_assistant.get_history",
                    {
                        "entity_id": "sensor.other_site",
                        "start": "2026-10-02T00:00:00Z",
                        "end": "2026-10-03T00:00:00Z",
                    },
                ),
            ]:
                foreign_entity = await client.post(
                    f"/sessions/{session}/execute",
                    headers=auth,
                    json={
                        "tool": tool_name,
                        "arguments": arguments,
                        "account_id": account["id"],
                    },
                )
                assert foreign_entity.json()["ok"] is False, foreign_entity.text
                assert foreign_entity.json()["error"]["code"] == "account_resource_forbidden"
            assert len(requests) == before_foreign_entity
            result = await client.post(
                f"/sessions/{session}/capability",
                headers=auth,
                json={"capability": "get_current_power"},
            )
            assert result.json()["ok"] is True, result.text
            healthy = await client.post(path + "/verify", headers=auth, json={})
            assert healthy.status_code == 200, healthy.text
            assert healthy.json()["health"]["status"] == "healthy"
            verified_at = vault.managed_snapshot(owner.user.id, owner.workspace.id, account["id"])[
                0
            ].last_verified_at
            provider_unhealthy = True
            unhealthy = await client.post(path + "/verify", headers=auth, json={})
            assert unhealthy.status_code == 200, unhealthy.text
            assert unhealthy.json()["health"]["status"] == "unhealthy"
            assert "Private provider diagnostic" not in unhealthy.text
            current = vault.managed_snapshot(owner.user.id, owner.workspace.id, account["id"])[0]
            assert current.last_verified_at == verified_at and current.state == "active"

        # A fresh host with the deployment configuration removed must reject the
        # persisted grant before discovery, refresh, health or mapping opens a socket.
        await agent.close()
        agent = build_agent(tmp_path / "restarted-agent")
        await agent.http.aclose()
        agent.http = upstream
        agent._owns_http = False
        agent.auth_store = AuthStore(tmp_path / "vault", vault_key, http=upstream)
        current_configurations = (
            ()
            if configuration_removed
            else (
                HomeAssistantOAuthConfiguration.model_validate(
                    {**configuration.model_dump(), "base_url": "https://replacement.example.test"}
                ),
            )
        )
        removed = create_host(
            agent,
            {},
            control_store=control,
            managed_workspaces=True,
            managed_oauth_configurations=current_configurations,
        )
        before = len(requests)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=removed), base_url="http://localhost"
        ) as client:
            created_session = await client.post(
                "/sessions", headers=auth, json={"site_id": site["id"]}
            )
            assert created_session.status_code == 200, created_session.text
            session = created_session.json()["session_id"]
            result = await client.post(
                f"/sessions/{session}/capability",
                headers=auth,
                json={"capability": "get_current_power"},
            )
            assert result.json()["ok"] is False, result.text
            for route, body in [("/verify", {}), ("/map", {"site_id": site["id"]})]:
                response = await client.post(path + route, headers=auth, json=body)
                assert response.status_code >= 400, response.text
                assert response.json()["error"]["code"] == "oauth_configuration_unavailable"
            assert len(requests) == before
            disconnected = await client.post(path + "/disconnect", headers=auth, json={})
            assert disconnected.status_code == 200, disconnected.text
            assert disconnected.json()["account"]["state"] == "revoked"
            assert disconnected.json()["upstream_revoked"] is None
            assert len(requests) == before
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()
