from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from test_host_connection_onboarding import PAYLOAD, _rpc
from test_hosting import _Lifespan

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import _MCPOwner, create_host
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import AuthConfig, ConnectedAccount, EnergyError


@pytest.mark.asyncio
async def test_mcp_shutdown_stops_all_owners_when_one_manager_exit_fails(tmp_path: Path):
    closed = []

    class Manager:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        @asynccontextmanager
        async def run(self):
            yield
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("fixture manager exit failure")

    agent = build_agent(tmp_path / "agent")
    agent.auth_store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    control = ControlStore(tmp_path / "control")
    host = create_host(
        agent, {}, control_store=control, managed_workspaces=True, close_agent_on_shutdown=True
    )
    owners = []
    try:
        with pytest.raises(RuntimeError, match="fixture manager exit failure"):
            async with host._app.router.lifespan_context(host._app):
                owners = [_MCPOwner(Manager("first", fail=True)), _MCPOwner(Manager("second"))]
                for owner in owners:
                    await owner.started
                host._mcp_owners = {
                    ("owner", str(index)): owner for index, owner in enumerate(owners)
                }
                host._mcp_pending["owner"] = 1
        assert set(closed) == {"first", "second"}
        assert all(owner.task.done() for owner in owners)
        assert not host._mcp_owners and not host._mcp_pending
        assert agent.http.is_closed
    finally:
        for owner in owners:
            if not owner.task.done():
                await owner.close()
        await agent.close()
        control.close()


def test_backup_restore_preserves_managed_scope_and_encrypted_mapping(tmp_path: Path):
    state = tmp_path / "state"
    controls = ControlStore(state / "control")
    owner = controls.bootstrap_workspace("Owner", "Home")
    site = controls.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    key = Fernet.generate_key()
    vault = AuthStore(state / "vault", key)
    pending = ConnectedAccount(
        id="fixture",
        user_id=owner.user.id,
        workspace_id=owner.workspace.id,
        toolkit="octopus-energy-account",
        state="pending_mapping",
        enabled=False,
        last_verified_at=datetime.now(UTC),
        auth=AuthConfig(scheme="basic"),
    )
    vault.stage_managed(pending, "synthetic-key")
    _, revision = vault.managed_snapshot(owner.user.id, owner.workspace.id, pending.id)
    vault.activate_managed(
        owner.user.id,
        owner.workspace.id,
        pending.id,
        site=site,
        expected_version=revision,
        verified_at=datetime.now(UTC),
    )
    vault.close()
    controls.close()
    archive = tmp_path / "backup.tar.gz"
    create_backup(state, archive)
    restored_root = tmp_path / "restored"
    restore_backup(archive, restored_root)
    restored = ControlStore(restored_root / "control")
    restored_vault = AuthStore(restored_root / "vault", key)
    try:
        identity = restored.authenticate(owner.key.token)
        assert identity is not None and identity.workspace_mode == "managed"
        assert identity.workspace_id == owner.workspace.id
        assert restored.sites(owner.user.id, owner.workspace.id)[0].id == site.id
        assert restored_vault.workspace_accounts(owner.user.id, None) == []
        account, _ = restored_vault.managed_snapshot(owner.user.id, owner.workspace.id, pending.id)
        assert account.state == "active" and account.site_id == site.id
        assert restored_vault.credential(owner.user.id, pending.id, site.id) == "synthetic-key"
    finally:
        restored.close()
        restored_vault.close()


@pytest.mark.asyncio
async def test_managed_host_does_not_promote_operator_workspace_keys(tmp_path: Path):
    control = ControlStore(tmp_path / "control")
    user = control.create_user("existing-owner", "Owner")
    workspace = control.create_workspace(user.id, "Existing")
    key = control.create_key(user.id, workspace.id, "Existing manager", access=ManageKeyAccess())
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent = build_agent(tmp_path / "agent")
    agent.auth_store = vault
    host = create_host(agent, {}, control_store=control, managed_workspaces=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
        ) as client:
            auth = {"Authorization": "Bearer " + key.token}
            assert (await client.get("/me", headers=auth)).status_code == 401
            assert (
                await client.post(
                    "/workspace/sites",
                    headers=auth,
                    json={"name": "Unauthorized", "timezone": "UTC"},
                )
            ).status_code == 401
            assert control.sites(user.id, workspace.id) == []
    finally:
        await agent.close()
        control.close()


@pytest.mark.asyncio
async def test_system_first_map_and_dynamic_mcp_with_live_revocation(tmp_path: Path):
    probes = []

    def provider(request):
        probes.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "consumption": 1.25,
                        "interval_start": "2026-09-30T00:00:00Z",
                        "interval_end": "2026-09-30T00:30:00Z",
                    }
                ]
            },
        )

    control = ControlStore(tmp_path / "control")
    bootstrap = control.bootstrap_workspace("Owner", "Home")
    second = control.create_workspace(bootstrap.user.id, "Second", mode="managed")
    second_key = control.create_key(
        bootstrap.user.id, second.id, "Second manager", access=ManageKeyAccess()
    )
    foreign_site = control.create_site(bootstrap.user.id, second.id, name="Other", timezone="UTC")
    vault_key = Fernet.generate_key()
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "vault.key").write_bytes(vault_key)
    vault = AuthStore(agent_root / "vault", vault_key)
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http = upstream
    agent._owns_http = False
    agent.auth_store = vault
    host = create_host(
        agent, {}, control_store=control, managed_workspaces=True, max_requests_per_minute=200
    )
    auth = {"Authorization": "Bearer " + bootstrap.key.token}
    other = {"Authorization": "Bearer " + second_key.token}
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            me = await client.get("/me", headers=auth)
            assert me.status_code == 200, me.text
            assert me.json()["sites"] == []
            assert me.json()["can_manage_workspace"] is True
            assert (await client.post("/sessions", headers=auth, json={})).status_code == 400
            assert (await client.get("/workspace/toolkits", headers=auth)).status_code == 200
            assert (await client.get("/workspace/connection-setups", headers=auth)).json()[
                "setups"
            ][0]["enabled"]
            staged = await client.post("/workspace/connections", headers=auth, json=PAYLOAD)
            assert staged.status_code == 201, staged.text
            account = staged.json()["account"]
            assert account["state"] == "pending_mapping" and account["site_id"] is None
            assert PAYLOAD["credential"] not in staged.text
            assert PAYLOAD["credential"].encode() not in vault.path.read_bytes()
            with pytest.raises(EnergyError):
                vault.credential(bootstrap.user.id, account["id"], None)
            before = len(probes)
            denied = await client.post(
                f"/workspace/connections/{account['id']}/map",
                headers=auth,
                json={"site_id": foreign_site.id},
            )
            assert denied.status_code == 403 and len(probes) == before
            created = await client.post(
                "/workspace/sites", headers=auth, json={"name": "Home", "timezone": "Europe/London"}
            )
            assert created.status_code == 201, created.text
            site_id = created.json()["site"]["id"]
            mapped = await client.post(
                f"/workspace/connections/{account['id']}/map",
                headers=auth,
                json={"site_id": site_id},
            )
            assert mapped.status_code == 200, mapped.text
            assert mapped.json()["account"]["state"] == "active"
            before = len(probes)
            assert (
                await client.post(
                    f"/workspace/connections/{account['id']}/map",
                    headers=auth,
                    json={"site_id": site_id},
                )
            ).status_code == 200
            assert len(probes) == before
            session = (
                await client.post("/sessions", headers=auth, json={"site_id": site_id})
            ).json()["session_id"]
            assert (
                await client.post(
                    f"/sessions/{session}/resolve",
                    headers=other,
                    json={"capability": "get_energy_consumption"},
                )
            ).status_code == 404
            executed = await client.post(
                f"/sessions/{session}/capability",
                headers=auth,
                json={"capability": "get_energy_consumption"},
            )
            assert executed.json()["ok"] is True, executed.text
            assert executed.json()["result"]["data"][0]["value"] == 1.25
            asset = await client.post(
                "/workspace/assets",
                headers=auth,
                json={
                    "site_id": site_id,
                    "name": "Meter",
                    "kind": "meter",
                    "account_ids": [account["id"]],
                },
            )
            assert asset.status_code == 201, asset.text
            issued = await client.post(
                "/workspace/keys", headers=auth, json={"name": "Agent", "site_ids": [site_id]}
            )
            assert issued.status_code == 201, issued.text
            key = issued.json()
            assert issued.headers["cache-control"] == "no-store"
            listed = await client.get("/workspace/keys", headers=auth)
            assert key["token"] not in listed.text
            scoped = {"Authorization": "Bearer " + key["token"]}
            assert (
                await client.post(
                    "/workspace/sites", headers=scoped, json={"name": "Denied", "timezone": "UTC"}
                )
            ).status_code == 403
            mcp_headers = {**scoped, "Accept": "application/json, text/event-stream"}
            initialized = await client.post(
                f"/mcp/{site_id}",
                headers=mcp_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "managed-fixture", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            mcp_headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            await client.post(
                f"/mcp/{site_id}",
                headers=mcp_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            result = _rpc(
                await client.post(
                    f"/mcp/{site_id}",
                    headers=mcp_headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
            )
            assert result["result"]["tools"]
            assert (
                await client.delete(f"/workspace/keys/{key['key']['id']}", headers=auth)
            ).status_code == 200
            assert (
                await client.post(
                    f"/mcp/{site_id}",
                    headers=mcp_headers,
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                )
            ).status_code == 401
            host.rotate_principals({})
            assert (await client.get("/me", headers=auth)).status_code == 200
            verified = await client.post(
                f"/workspace/connections/{account['id']}/verify", headers=auth, json={}
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["health"]["status"] == "healthy"
            disconnected = await client.post(
                f"/workspace/connections/{account['id']}/disconnect", headers=auth, json={}
            )
            assert disconnected.status_code == 200, disconnected.text
            assert disconnected.json()["account"]["state"] == "revoked"
            restaged = await client.post("/workspace/connections", headers=auth, json=PAYLOAD)
            assert restaged.status_code == 201, restaged.text
            assert (
                await client.post(
                    f"/workspace/connections/{account['id']}/map",
                    headers=auth,
                    json={"site_id": site_id},
                )
            ).status_code == 200
        assert not host._mcp_owners
        await agent.close()
        control.close()
        control = ControlStore(tmp_path / "control")
        agent = build_agent(agent_root, {"vault": {"master_key_file": "vault.key"}})
        assert not agent.sites
        assert not any(item.workspace_id is not None for item in agent.accounts.values())
        await agent.http.aclose()
        agent.http = upstream
        agent._owns_http = False
        restarted = create_host(agent, {}, control_store=control, managed_workspaces=True)
        async with (
            _Lifespan(restarted),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            identity = await client.get("/me", headers=auth)
            assert identity.status_code == 200, identity.text
            assert identity.json()["sites"][0]["id"] == site_id
            session = (
                await client.post("/sessions", headers=auth, json={"site_id": site_id})
            ).json()["session_id"]
            executed = await client.post(
                f"/sessions/{session}/capability",
                headers=auth,
                json={"capability": "get_energy_consumption"},
            )
            assert executed.json()["ok"] is True, executed.text
            assert executed.json()["result"]["data"][0]["value"] == 1.25
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()
