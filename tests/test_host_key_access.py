"""Workspace agent keys cannot manage connections or escape their site grants."""

from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess, LegacyKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import Site


def test_static_agent_principal_also_intersects_explicit_grants():
    access = AgentKeyAccess(site_ids=["home"])
    principal = Principal("one", {"home", "workshop"}, token_digest("fixture"), key_access=access)
    access.site_ids.append("workshop")
    assert principal.allowed_site_ids == {"home"}
    assert principal.key_access == AgentKeyAccess(site_ids=["home"])
    with pytest.raises(ValueError):
        Principal(
            "one",
            {"home"},
            token_digest("fixture"),
            key_access=AgentKeyAccess.model_construct(site_ids=[]),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access", [AgentKeyAccess(site_ids=["home"]), ManageKeyAccess(), LegacyKeyAccess()]
)
async def test_explicit_key_roles_never_receive_a_site_free_session(tmp_path: Path, access):
    agent = build_agent(tmp_path / "agent")
    host = create_host(
        agent,
        {"one": Principal("one", set(), token_digest("fixture"), key_access=access)},
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host),
            base_url="http://127.0.0.1:8000",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            assert (await client.get("/me")).status_code == 200
            result = await client.post("/sessions", json={})
            assert result.status_code == 400 and result.json()["error"]["code"] == "site_required"
            mcp = await client.post("/mcp", json={})
            assert mcp.status_code == 409 and mcp.json()["error"]["code"] == "site_required"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_key_is_site_bound_and_cannot_connect_or_disconnect(tmp_path: Path):
    probes = []

    def provider(request: httpx.Request):
        probes.append(request)
        return httpx.Response(200, json={"results": []})

    controls = ControlStore(tmp_path / "control")
    controls.create_user("one", "Owner")
    workspace = controls.create_workspace("one", "Energy")
    sites = [
        Site(id=name, user_id="one", name=name, timezone="UTC") for name in ["home", "workshop"]
    ]
    for site in sites:
        controls.put_site("one", workspace.id, site)
    manager = controls.create_key("one", workspace.id, "Manage", access=ManageKeyAccess())
    agent_key = controls.create_key(
        "one", workspace.id, "Agent", access=AgentKeyAccess(site_ids=["home"])
    )
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    agent.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.auth_store = vault
    agent.sites = {site.id: site for site in sites}
    host = create_host(
        agent,
        {"one": Principal("one", {"home", "workshop"}, token_digest("operator-fixture"))},
        control_store=controls,
    )
    owner_auth = {"Authorization": "Bearer " + manager.token}
    agent_auth = {"Authorization": "Bearer " + agent_key.token}
    payload = {
        "provider": "octopus",
        "credential": "fictional-key",
        "mpan": "1234567890123",
        "serial_number": "TEST123",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
        ) as client:
            identity = (await client.get("/me", headers=agent_auth)).json()
            assert [site["id"] for site in identity["sites"]] == ["home"]
            assert identity["can_manage_connections"] is False
            assert (
                await client.post("/sessions", headers=agent_auth, json={"site_id": "workshop"})
            ).status_code == 403
            assert (
                await client.post("/mcp/workshop", headers=agent_auth, json={})
            ).status_code == 403
            session = (
                await client.post("/sessions", headers=agent_auth, json={"site_id": "home"})
            ).json()["session_id"]
            setup = (
                await client.get(f"/sessions/{session}/connection-setup", headers=agent_auth)
            ).json()["setups"][0]
            assert (
                setup["enabled"] is False
                and setup["unavailable_reason"] == "management_key_required"
            )
            assert (
                await client.post(
                    f"/sessions/{session}/connections", headers=agent_auth, json=payload
                )
            ).status_code == 403
            assert not probes and not vault.accounts("one")
            owner_session = (
                await client.post("/sessions", headers=owner_auth, json={"site_id": "home"})
            ).json()["session_id"]
            created = await client.post(
                f"/sessions/{owner_session}/connections", headers=owner_auth, json=payload
            )
            assert created.status_code == 201
            account_id = created.json()["account"]["id"]
            path = f"/sessions/{session}/connections/{account_id}"
            assert (
                await client.post(path + "/disconnect", headers=agent_auth, json={})
            ).status_code == 403
            assert vault.get_account("one", account_id, "home").enabled is True
            checked = await client.post(path + "/verify", headers=agent_auth, json={})
            assert checked.status_code == 200 and checked.json()["health"]["status"] == "healthy"
            assert len(probes) == 2
    finally:
        await agent.close()
        vault.close()
        controls.close()
