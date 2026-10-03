"""Real persisted keys narrow configured gateway scopes and revoke immediately."""

from pathlib import Path

import httpx
import pytest

from energy_agent_tools.control_contracts import AgentKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


@pytest.mark.asyncio
async def test_persisted_keys_scope_rest_and_mcp_and_revoke_on_open_host(tmp_path: Path):
    store = ControlStore(tmp_path / "control")
    store.create_user("one", "One")
    first = store.create_workspace("one", "Home")
    second = store.create_workspace("one", "Workshop")
    sites = [
        Site(id="home", user_id="one", name="Home", timezone="UTC"),
        Site(id="workshop", user_id="one", name="Workshop", timezone="UTC"),
    ]
    store.put_site("one", first.id, sites[0])
    store.put_site("one", second.id, sites[1])
    first_key = store.create_key(
        "one", first.id, "First agent", access=AgentKeyAccess(site_ids=["home"])
    )
    second_key = store.create_key(
        "one", second.id, "Second agent", access=AgentKeyAccess(site_ids=["workshop"])
    )
    agent = EnergyAgent(Registry(), tmp_path / "agent", sites=sites)
    policy = {"one": Principal("one", {"home", "workshop"}, token_digest("operator-test-token"))}
    host = create_host(agent, policy, control_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            auth = {"Authorization": f"Bearer {first_key.token}"}
            identity = await client.get("/me", headers=auth)
            assert identity.status_code == 200
            assert [s["id"] for s in identity.json()["sites"]] == ["home"]
            assert (
                await client.post("/sessions", headers=auth, json={"site_id": "workshop"})
            ).status_code == 403
            assert (await client.post("/mcp/workshop", headers=auth, json={})).status_code == 403
            created = await client.post("/sessions", headers=auth, json={"site_id": "home"})
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            assert (
                await client.get(
                    f"/sessions/{session_id}/artifacts",
                    headers={"Authorization": f"Bearer {second_key.token}"},
                )
            ).status_code == 403
            other = ControlStore(tmp_path / "control")
            other.revoke_key("one", first.id, first_key.key.id)
            other.close()
            assert (await client.get("/me", headers=auth)).status_code == 401
            assert (
                await client.get(f"/sessions/{session_id}/artifacts", headers=auth)
            ).status_code == 401
            assert (await client.post("/mcp/home", headers=auth, json={})).status_code == 401
            assert (
                await client.get("/me", headers={"Authorization": f"Bearer {second_key.token}"})
            ).status_code == 200
    finally:
        await agent.close()
        store.close()


@pytest.mark.asyncio
async def test_persisted_key_cannot_widen_operator_policy_or_get_siteless_mount(tmp_path: Path):
    store = ControlStore(tmp_path / "control")
    store.create_user("one", "One")
    workspace = store.create_workspace("one", "Unmapped")
    key = store.create_key("one", workspace.id, "Agent", access=ManageKeyAccess())
    home = Site(id="home", user_id="one", name="Home", timezone="UTC")
    denied = Site(id="denied", user_id="one", name="Private site", timezone="UTC")
    agent = EnergyAgent(Registry(), tmp_path / "agent", sites=[home, denied])
    host = create_host(
        agent,
        {"one": Principal("one", {"home"}, token_digest("operator-test-token"))},
        control_store=store,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            auth = {"Authorization": f"Bearer {key.token}"}
            assert (await client.get("/me", headers=auth)).status_code == 401
            store.put_site("one", workspace.id, denied)
            assert (await client.get("/me", headers=auth)).status_code == 401
            store.put_site("one", workspace.id, home)
            response = await client.get("/me", headers=auth)
            assert response.status_code == 200
            assert [s["id"] for s in response.json()["sites"]] == ["home"]
    finally:
        await agent.close()
        store.close()


@pytest.mark.asyncio
async def test_store_key_digest_in_operator_config_cannot_bypass_revocation(tmp_path: Path):
    store = ControlStore(tmp_path / "control")
    store.create_user("one", "One")
    workspace = store.create_workspace("one", "Home")
    site = Site(id="home", user_id="one", name="Home", timezone="UTC")
    store.put_site("one", workspace.id, site)
    key = store.create_key("one", workspace.id, "Agent", access=AgentKeyAccess(site_ids=["home"]))
    agent = EnergyAgent(Registry(), tmp_path / "agent", sites=[site])
    host = create_host(
        agent, {"one": Principal("one", {"home"}, token_digest(key.token))}, control_store=store
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            auth = {"Authorization": f"Bearer {key.token}"}
            assert (await client.get("/me", headers=auth)).status_code == 200
            store.revoke_key("one", workspace.id, key.key.id)
            assert (await client.get("/me", headers=auth)).status_code == 401
    finally:
        await agent.close()
        store.close()
