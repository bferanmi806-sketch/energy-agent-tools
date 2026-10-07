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
from energy_agent_tools.workspace_access import WorkspaceMemberGrants


@pytest.mark.asyncio
async def test_member_grant_revoked_during_managed_refresh_prevents_rotated_token_commit_and_energy_io(
    tmp_path: Path,
) -> None:
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    member = control.create_user("member", "Member")
    requests: list[httpx.Request] = []
    energy_reads: list[httpx.Request] = []
    refresh_grants: list[bool] = []
    removed_grants: list[WorkspaceMemberGrants] = []
    cleanup_attempts: list[tuple[bool, bool]] = []
    vault: AuthStore
    site_id = ""

    original_access = "synthetic-original-access"
    original_refresh = "synthetic-original-refresh"
    rotated_access = "synthetic-rotated-access"
    rotated_refresh = "synthetic-rotated-refresh"

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": original_access,
                        "refresh_token": original_refresh,
                        "expires_in": 0,
                        "token_type": "Bearer",
                    },
                )
            if form.get("grant_type") == ["refresh_token"]:
                refresh_grants.append(form.get("refresh_token") == [original_refresh])
                removed_grants.append(
                    control.set_member_grants(
                        owner.user.id,
                        owner.workspace.id,
                        member.id,
                        WorkspaceMemberGrants(site_ids=[site_id], connection_ids=[]),
                    ).grants
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": rotated_access,
                        "refresh_token": rotated_refresh,
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
            pytest.fail("OAuth provider received an unexpected token grant.")
        if request.url.path == "/auth/revoke":
            form = parse_qs(request.content.decode())
            cleanup_attempts.append(
                (
                    vault.pending_managed_oauth_cleanup(owner.user.id, owner.workspace.id, "home")
                    == 1,
                    form.get("token") == [rotated_refresh],
                )
            )
            return httpx.Response(200, json={"revoked": True})
        if request.url.path == "/api/states/sensor.power":
            energy_reads.append(request)
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
            )
        pytest.fail("OAuth provider received an unexpected request.")

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
    owner_auth = {"Authorization": "Bearer " + owner.key.token}
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
            member_created = await client.post(
                "/workspace/members", headers=owner_auth, json={"user_id": member.id}
            )
            assert member_created.status_code == 201, member_created.text

            site_created = await client.post(
                "/workspace/sites",
                headers=owner_auth,
                json={"name": "Home", "timezone": "UTC"},
            )
            assert site_created.status_code == 201, site_created.text
            site = site_created.json()["site"]
            site_id = site["id"]

            begun = await client.post(
                "/workspace/authorizations",
                headers=owner_auth,
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
                headers=owner_auth,
                json={"configuration_id": "home", "state": state, "code": "synthetic-code"},
            )
            assert completed.status_code == 201, completed.text
            account = completed.json()["account"]
            assert account["state"] == "pending_mapping"
            assert "synthetic-" not in completed.text

            mapped = await client.post(
                f"/workspace/connections/{account['id']}/map",
                headers=owner_auth,
                json={"site_id": site_id},
            )
            assert mapped.status_code == 200, mapped.text
            assert mapped.json()["account"]["state"] == "active"

            granted = await client.patch(
                f"/workspace/members/{member.id}",
                headers=owner_auth,
                json={"site_ids": [site_id], "connection_ids": [account["id"]]},
            )
            assert granted.status_code == 200, granted.text
            issued = await client.post(
                f"/workspace/members/{member.id}/keys",
                headers=owner_auth,
                json={"name": "Refresh race member", "site_ids": [site_id]},
            )
            assert issued.status_code == 201, issued.text
            member_token = issued.json()["token"]
            member_auth = {"Authorization": f"Bearer {member_token}"}
            session_response = await client.post(
                "/sessions", headers=member_auth, json={"site_id": site_id}
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["session_id"]

            before, revision = vault.managed_snapshot(
                owner.user.id, owner.workspace.id, account["id"]
            )
            assert before.state == "active" and before.enabled
            assert before.expires_at is not None and before.expires_at <= datetime.now(UTC)
            _, before_tokens = vault._credential_payload_allow_disabled(
                owner.user.id, account["id"], site_id
            )
            assert before_tokens["credential"] == original_access
            assert before_tokens["refresh_token"] == original_refresh
            energy_reads_before_execution = len(energy_reads)

            execution = await client.post(
                f"/sessions/{session_id}/execute",
                headers=member_auth,
                json={
                    "tool": "home_assistant.get_state",
                    "arguments": {"entity_id": "sensor.power"},
                    "account_id": account["id"],
                },
            )
            assert execution.status_code == 200, execution.text
            response = execution.json()
            assert response["ok"] is False, execution.text
            assert response["error"]["code"] == "workspace_forbidden", execution.text
            assert all(
                secret not in execution.text
                for secret in (
                    original_access,
                    original_refresh,
                    rotated_access,
                    rotated_refresh,
                    owner.key.token,
                    member_token,
                )
            )

            assert refresh_grants == [True]
            assert removed_grants == [WorkspaceMemberGrants(site_ids=[site_id], connection_ids=[])]
            assert len(energy_reads) == energy_reads_before_execution
            assert len(cleanup_attempts) == 1
            assert cleanup_attempts == [(True, True)]

            after, current_revision = vault.managed_snapshot(
                owner.user.id, owner.workspace.id, account["id"]
            )
            assert current_revision == revision
            assert after.state == "active" and after.enabled and after.site_id == site_id
            assert after.expires_at == before.expires_at
            _, after_tokens = vault._credential_payload_allow_disabled(
                owner.user.id, account["id"], site_id
            )
            assert after_tokens["credential"] == original_access
            assert after_tokens["refresh_token"] == original_refresh
            assert (
                vault.pending_managed_oauth_cleanup(owner.user.id, owner.workspace.id, "home") == 0
            )
            assert len(requests) == len(energy_reads) + 3
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()
