from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.managed_oauth import HomeAssistantOAuthConfiguration


@pytest.mark.asyncio
async def test_failed_authorization_cleanup_is_visible_scoped_and_retryable(tmp_path):
    calls = []
    revocation_available = False

    def provider(request):
        calls.append(request)
        if request.url.path == "/auth/token":
            assert parse_qs(request.content.decode())["code"] == ["fixture-code"]
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-private-access",
                    "refresh_token": "fixture-private-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/auth/revoke":
            assert parse_qs(request.content.decode())["token"] == ["fixture-private-refresh"]
            return httpx.Response(200 if revocation_available else 503)
        return httpx.Response(401, json={"message": "private-provider-diagnostic"})

    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    second = control.create_workspace(owner.user.id, "Second", mode="managed")
    foreign_key = control.create_key(
        owner.user.id, second.id, "Second manager", access=ManageKeyAccess()
    )
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    agent_key = control.create_key(
        owner.user.id, owner.workspace.id, "Agent", access=AgentKeyAccess(site_ids=[site.id])
    )
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=upstream)
    agent.auth_store = vault
    configuration = HomeAssistantOAuthConfiguration(
        id="home",
        name="Home Assistant",
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
    manager = {"Authorization": "Bearer " + owner.key.token}
    foreign = {"Authorization": "Bearer " + foreign_key.token}
    execution = {"Authorization": "Bearer " + agent_key.token}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost"
        ) as client:
            begun = await client.post(
                "/workspace/authorizations",
                headers=manager,
                json={"configuration_id": "home", "entity_id": "sensor.power"},
            )
            assert begun.status_code == 201, begun.text
            completion = await client.post(
                "/workspace/authorizations/complete",
                headers=manager,
                json={
                    "configuration_id": "home",
                    "state": begun.json()["authorization"]["state"],
                    "code": "fixture-code",
                },
            )
            assert completion.status_code >= 400, completion.text
            assert completion.json()["error"]["code"] == "provider_verification_failed"
            assert len([call for call in calls if call.url.path == "/auth/revoke"]) == 1
            assert (await client.get("/workspace/connections", headers=manager)).json()[
                "connections"
            ] == []
            configurations = await client.get("/workspace/auth-configurations", headers=manager)
            assert configurations.json()["configurations"][0]["pending_cleanup"] == 1
            assert "fixture-private" not in configurations.text + completion.text
            assert b"fixture-private-refresh" not in vault.path.read_bytes()

            before = len(calls)
            denied = await client.post(
                "/workspace/authorizations/cleanup",
                headers=execution,
                json={"configuration_id": "home"},
            )
            assert denied.status_code == 403
            other = await client.post(
                "/workspace/authorizations/cleanup",
                headers=foreign,
                json={"configuration_id": "home"},
            )
            assert other.json()["cleanup"] == {"attempted": 0, "succeeded": 0, "pending": 0}
            unknown = await client.post(
                "/workspace/authorizations/cleanup",
                headers=manager,
                json={"configuration_id": "unapproved"},
            )
            assert unknown.status_code >= 400
            assert len(calls) == before

            revocation_available = True
            cleaned = await client.post(
                "/workspace/authorizations/cleanup",
                headers=manager,
                json={"configuration_id": "home"},
            )
            assert cleaned.status_code == 200, cleaned.text
            assert cleaned.json()["cleanup"] == {"attempted": 1, "succeeded": 1, "pending": 0}
            assert "fixture-private" not in cleaned.text
            configurations = await client.get("/workspace/auth-configurations", headers=manager)
            assert configurations.json()["configurations"][0]["pending_cleanup"] == 0
            assert vault.workspace_accounts(owner.user.id, owner.workspace.id) == []
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()
