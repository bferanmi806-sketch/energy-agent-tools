from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.cloud_oauth import CloudEnergyOAuthConfiguration
from energy_agent_tools.connectors.tesla_energy import TESLA_ENERGY_DOCS
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host

_RESOURCE_IDS = {"tesla": "1234567890", "enphase": "698910067"}
_TOOL_NAMES = {
    "tesla": "tesla_energy.get_site_info",
    "enphase": "enphase_energy.get_summary",
}


def _site_response(site_id: str) -> dict[str, object]:
    return {
        "response": {
            "id": site_id,
            "site_name": "Private Tesla Home",
            "installation_time_zone": "Europe/London",
            "nameplate_power": 10000,
            "nameplate_energy": 27000,
            "components": {"solar": True, "battery": True, "grid": True},
        }
    }


def _enphase_summary(system_id: int) -> dict[str, object]:
    return {
        "system_id": system_id,
        "current_power": 4200,
        "energy_lifetime": 15872000,
        "energy_today": 19200,
        "last_interval_end_at": 1791460500,
        "last_report_at": 1791460530,
        "modules": 30,
        "operational_at": 1557400231,
        "size_w": 12500,
        "source": "meter",
        "status": "normal",
        "summary_date": "2026-10-08",
    }


def _configuration(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CloudEnergyOAuthConfiguration, str, str, str, str]:
    configuration_id = f"{provider}-home"
    secret_name = f"{provider.upper()}_OAUTH_CLIENT_SECRET"
    client_secret = f"private-{provider}-client-secret"
    monkeypatch.setenv(secret_name, client_secret)
    api_key_name = "ENPHASE_APPLICATION_API_KEY" if provider == "enphase" else None
    api_key = "private-enphase-application-key" if provider == "enphase" else ""
    if api_key_name:
        monkeypatch.setenv(api_key_name, api_key)
    values: dict[str, object] = {
        "id": configuration_id,
        "name": f"{provider.title()} account",
        "provider": provider,
        "client_id": f"energy-agent-{provider}-client",
        "client_secret_env": secret_name,
        "redirect_uri": "https://energy.example.test/api/workspace/oauth/callback",
    }
    if provider == "tesla":
        values["region"] = "eu"
    else:
        values["api_key_env"] = api_key_name
    return (
        CloudEnergyOAuthConfiguration.model_validate(values),
        configuration_id,
        client_secret,
        api_key,
        secret_name,
    )


def _assert_no_secrets(value: str, *secrets: str) -> None:
    for secret in secrets:
        if secret:
            assert secret not in value


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["tesla", "enphase"])
async def test_cloud_oauth_enrolls_selected_resource_refreshes_executes_and_revokes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    configuration, configuration_id, client_secret, api_key, _secret_name = _configuration(
        provider_name, monkeypatch
    )
    resource_id = _RESOURCE_IDS[provider_name]
    access_token = f"private-{provider_name}-access"
    refresh_token = f"private-{provider_name}-refresh"
    rotated_access = f"private-{provider_name}-rotated-access"
    rotated_refresh = f"private-{provider_name}-rotated-refresh"
    token_requests: list[httpx.Request] = []
    resource_reads: list[httpx.Request] = []
    revoked_tokens: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            token_requests.append(request)
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["authorization_code"]:
                if provider_name == "tesla":
                    assert form.get("audience") == ["https://fleet-api.prd.eu.vn.cloud.tesla.com"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 0,
                    },
                    request=request,
                )
            if form.get("grant_type") == ["refresh_token"]:
                assert form.get("refresh_token") == [refresh_token]
                if provider_name == "tesla":
                    assert "audience" not in form
                    assert "client_secret" not in form
                    assert "authorization" not in request.headers
                return httpx.Response(
                    200,
                    json={
                        "access_token": rotated_access,
                        "refresh_token": rotated_refresh,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                    request=request,
                )
            pytest.fail("The OAuth provider received an unexpected token grant.")

        if request.url.path.endswith("/revoke"):
            form = parse_qs(request.content.decode())
            revoked_tokens.extend(form.get("token", []))
            assert form.get("token_type_hint") == ["refresh_token"]
            return httpx.Response(200, json={"revoked": True}, request=request)

        resource_reads.append(request)
        assert request.headers.get("authorization") in {
            f"Bearer {access_token}",
            f"Bearer {rotated_access}",
        }
        if provider_name == "tesla":
            assert request.url.host == "fleet-api.prd.eu.vn.cloud.tesla.com"
            assert request.url.path == f"/api/1/energy_sites/{resource_id}/site_info"
            return httpx.Response(200, json=_site_response(resource_id), request=request)

        assert request.url.host == "api.enphaseenergy.com"
        assert request.url.path == f"/api/v4/systems/{resource_id}/summary"
        assert dict(request.url.params) == {"key": api_key}
        return httpx.Response(
            200,
            json=_enphase_summary(int(resource_id)),
            request=request,
        )

    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    other_owner = control.bootstrap_workspace("Other owner", "Other home")
    owner_auth = {"Authorization": f"Bearer {owner.key.token}"}
    other_auth = {"Authorization": f"Bearer {other_owner.key.token}"}

    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault_key = Fernet.generate_key()
    vault = AuthStore(tmp_path / "vault", vault_key, http=upstream)
    agent.auth_store = vault
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
            configurations = await client.get("/workspace/auth-configurations", headers=owner_auth)
            assert configurations.status_code == 200, configurations.text
            public_config = configurations.json()["configurations"][0]
            assert public_config["id"] == configuration_id
            assert public_config["toolkit"] == configuration.toolkit
            _assert_no_secrets(configurations.text, client_secret, api_key)

            other_site = await client.post(
                "/workspace/sites",
                headers=other_auth,
                json={"name": "Other home", "timezone": "UTC"},
            )
            assert other_site.status_code == 201, other_site.text
            foreign_site_id = other_site.json()["site"]["id"]

            begun = await client.post(
                "/workspace/provider-authorizations",
                headers=owner_auth,
                json={"configuration_id": configuration_id, "resource_id": resource_id},
            )
            assert begun.status_code == 201, begun.text
            authorization = begun.json()["authorization"]
            assert authorization["connection_id"]
            authorization_url = urlparse(authorization["authorization_url"])
            if provider_name == "tesla":
                assert authorization_url.hostname == "auth.tesla.com"
                assert authorization_url.path == "/oauth2/v3/authorize"
            else:
                assert authorization_url.hostname == "api.enphaseenergy.com"
                assert authorization_url.path == "/oauth/authorize"
            authorization_query = parse_qs(authorization_url.query)
            assert authorization_query["state"] == [authorization["state"]]
            assert authorization_query["client_id"] == [configuration.client_id]
            _assert_no_secrets(begun.text, client_secret, api_key)

            completed = await client.post(
                "/workspace/authorizations/complete",
                headers=owner_auth,
                json={
                    "configuration_id": configuration_id,
                    "state": authorization["state"],
                    "code": "synthetic-one-time-code",
                },
            )
            assert completed.status_code == 201, completed.text
            account = completed.json()["account"]
            assert account["toolkit"] == configuration.toolkit
            assert account["state"] == "pending_mapping"
            assert account["site_id"] is None and account["enabled"] is False
            assert account["verified"] is True
            assert len(resource_reads) == 1
            _assert_no_secrets(completed.text, client_secret, api_key, access_token, refresh_token)

            exchanged = len(token_requests)
            replay = await client.post(
                "/workspace/authorizations/complete",
                headers=owner_auth,
                json={
                    "configuration_id": configuration_id,
                    "state": authorization["state"],
                    "code": "synthetic-one-time-code",
                },
            )
            assert replay.status_code >= 400, replay.text
            assert len(token_requests) == exchanged

            foreign_mapping = await client.post(
                f"/workspace/connections/{account['id']}/map",
                headers=owner_auth,
                json={"site_id": foreign_site_id},
            )
            assert foreign_mapping.status_code == 403, foreign_mapping.text
            assert len(resource_reads) == 1

            foreign_verify = await client.post(
                f"/workspace/connections/{account['id']}/verify",
                headers=other_auth,
                json={},
            )
            assert foreign_verify.status_code >= 400, foreign_verify.text
            assert len(resource_reads) == 1

            site = await client.post(
                "/workspace/sites",
                headers=owner_auth,
                json={"name": "Owner home", "timezone": "UTC"},
            )
            assert site.status_code == 201, site.text
            site_id = site.json()["site"]["id"]
            mapped = await client.post(
                f"/workspace/connections/{account['id']}/map",
                headers=owner_auth,
                json={"site_id": site_id},
            )
            assert mapped.status_code == 200, mapped.text
            assert mapped.json()["account"]["state"] == "active"
            assert mapped.json()["account"]["site_id"] == site_id

            # The authorization response is immediately expired. A managed health
            # check must refresh it before performing the provider read.
            verified = await client.post(
                f"/workspace/connections/{account['id']}/verify",
                headers=owner_auth,
                json={},
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["health"]["status"] == "healthy"
            assert len(token_requests) == exchanged + 1
            assert token_requests[-1].headers.get("authorization", "").startswith("Basic ") == (
                provider_name == "enphase"
            )
            _assert_no_secrets(
                verified.text, client_secret, api_key, rotated_access, rotated_refresh
            )

            issued = await client.post(
                "/workspace/keys",
                headers=owner_auth,
                json={"name": "Cloud energy agent", "site_ids": [site_id]},
            )
            assert issued.status_code == 201, issued.text
            agent_auth = {"Authorization": f"Bearer {issued.json()['token']}"}
            session = await client.post("/sessions", headers=agent_auth, json={"site_id": site_id})
            assert session.status_code == 200, session.text
            executed = await client.post(
                f"/sessions/{session.json()['session_id']}/execute",
                headers=agent_auth,
                json={
                    "tool": _TOOL_NAMES[provider_name],
                    "arguments": {},
                    "account_id": account["id"],
                },
            )
            assert executed.status_code == 200, executed.text
            assert executed.json()["ok"] is True, executed.text
            assert executed.json()["result"]["site_id"] == site_id
            assert resource_reads[-1].headers["authorization"] == f"Bearer {rotated_access}"
            _assert_no_secrets(
                executed.text, client_secret, api_key, rotated_access, rotated_refresh
            )

            # A different workspace cannot address this connection. Its rejection
            # occurs before the provider sees another request.
            before_foreign_execution = len(resource_reads)
            foreign_session = await client.post(
                "/sessions", headers=other_auth, json={"site_id": foreign_site_id}
            )
            assert foreign_session.status_code == 200, foreign_session.text
            foreign_execution = await client.post(
                f"/sessions/{foreign_session.json()['session_id']}/execute",
                headers=other_auth,
                json={
                    "tool": _TOOL_NAMES[provider_name],
                    "arguments": {},
                    "account_id": account["id"],
                },
            )
            assert foreign_execution.status_code == 200, foreign_execution.text
            assert foreign_execution.json()["ok"] is False
            assert len(resource_reads) == before_foreign_execution

            disconnected = await client.post(
                f"/workspace/connections/{account['id']}/disconnect",
                headers=owner_auth,
                json={},
            )
            assert disconnected.status_code == 200, disconnected.text
            assert disconnected.json()["account"]["state"] == "revoked"
            # Tesla and Enphase expose no revocation endpoint in this managed
            # contract. Disconnect still disables locally and clears both tokens.
            assert disconnected.json()["upstream_revoked"] is None
            assert revoked_tokens == []
            _assert_no_secrets(
                disconnected.text, client_secret, api_key, rotated_access, rotated_refresh
            )

            _, stored = vault._credential_payload_allow_disabled(
                owner.user.id, account["id"], site_id
            )
            assert stored["credential"] is None
            assert stored["refresh_token"] is None
            database = vault.path.read_bytes()
            for secret in (
                client_secret,
                api_key,
                access_token,
                refresh_token,
                rotated_access,
                rotated_refresh,
            ):
                if secret:
                    assert secret.encode() not in database

            all_responses = (
                configurations.text
                + begun.text
                + completed.text
                + replay.text
                + foreign_mapping.text
                + foreign_verify.text
                + mapped.text
                + verified.text
                + executed.text
                + foreign_execution.text
                + disconnected.text
            )
            _assert_no_secrets(
                all_responses,
                client_secret,
                api_key,
                access_token,
                refresh_token,
                rotated_access,
                rotated_refresh,
            )

            # Keep the provider's documented source referenced in the fixture so
            # the connector and host journey stay tied to the same provider.
            if provider_name == "tesla":
                assert TESLA_ENERGY_DOCS in json.dumps(executed.json(), sort_keys=True)
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()


@pytest.mark.asyncio
async def test_enphase_mismatched_system_id_is_rejected_before_connection_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration, configuration_id, client_secret, api_key, _secret_name = _configuration(
        "enphase", monkeypatch
    )
    resource_id = _RESOURCE_IDS["enphase"]
    access_token = "private-enphase-mismatch-access"
    token_requests: list[httpx.Request] = []
    resource_reads: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            token_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "refresh_token": "private-enphase-mismatch-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                request=request,
            )
        resource_reads.append(request)
        assert request.url.host == "api.enphaseenergy.com"
        assert request.url.path == f"/api/v4/systems/{resource_id}/summary"
        assert request.url.params.get("key") == api_key
        assert request.headers.get("authorization") == f"Bearer {access_token}"
        return httpx.Response(
            200,
            json={"system_id": int(resource_id) + 1, "current_power": 4200},
            request=request,
        )

    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    owner_auth = {"Authorization": f"Bearer {owner.key.token}"}
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key(), http=upstream)
    agent.auth_store = vault
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
                "/workspace/provider-authorizations",
                headers=owner_auth,
                json={"configuration_id": configuration_id, "resource_id": resource_id},
            )
            assert begun.status_code == 201, begun.text
            authorization = begun.json()["authorization"]
            completed = await client.post(
                "/workspace/authorizations/complete",
                headers=owner_auth,
                json={
                    "configuration_id": configuration_id,
                    "state": authorization["state"],
                    "code": "synthetic-enphase-mismatch-code",
                },
            )
            assert completed.status_code == 400, completed.text
            assert completed.json()["error"]["code"] == "provider_verification_failed"
            assert len(token_requests) == 1
            assert len(resource_reads) == 1
            connections = await client.get("/workspace/connections", headers=owner_auth)
            assert connections.status_code == 200, connections.text
            assert connections.json()["connections"] == []
            _assert_no_secrets(
                completed.text + connections.text,
                client_secret,
                api_key,
                access_token,
                "private-enphase-mismatch-refresh",
            )
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()
