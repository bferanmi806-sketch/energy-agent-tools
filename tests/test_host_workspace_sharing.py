from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from test_host_connection_onboarding import _rpc
from test_hosting import _Lifespan
from test_jobs import heat_loss_args

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.models import (
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
)
from energy_agent_tools.onboarding import reviewed_provider_bindings


@pytest.fixture
async def sharing(tmp_path):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    member = control.create_user("member", "Member")
    foreign = control.bootstrap_workspace("Foreign", "Other")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    vault = AuthStore(tmp_path / "vault", Fernet.generate_key())
    for identifier in ["shared", "private"]:
        settings = {
            "base_url": "http://127.0.0.1:8123",
            "entity_id": f"sensor.{identifier}",
            "telemetry_role": "current_power",
            "unit": "kW",
            "quantity_shape": "instantaneous",
            "measurement_kind": "metered",
        }
        settings["capability_bindings"] = reviewed_provider_bindings("home_assistant", settings)
        vault.stage_managed(
            ConnectedAccount(
                id=identifier,
                user_id=owner.user.id,
                workspace_id=owner.workspace.id,
                toolkit="home-assistant",
                settings=settings,
                state="pending_mapping",
                enabled=False,
                last_verified_at=datetime.now(UTC),
                auth=AuthConfig(scheme="bearer"),
            ),
            "private-fixture-credential",
        )
        _, revision = vault.managed_snapshot(owner.user.id, owner.workspace.id, identifier)
        vault.activate_managed(
            owner.user.id,
            owner.workspace.id,
            identifier,
            site=site,
            expected_version=revision,
            verified_at=datetime.now(UTC),
        )
    probes = []

    def provider(request):
        probes.append(request)
        assert request.headers["authorization"] == "Bearer private-fixture-credential"
        assert request.url.path == "/api/states/sensor.shared"
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.shared",
                "state": "1.75",
                "attributes": {"unit_of_measurement": "kW", "state_class": "measurement"},
                "last_updated": datetime.now(UTC).isoformat(),
            },
        )

    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent.http, agent._owns_http, agent.auth_store = upstream, False, vault
    host = create_host(
        agent, {}, control_store=control, managed_workspaces=True, max_requests_per_minute=500
    )
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            yield SimpleNamespace(
                client=client,
                host=host,
                control=control,
                agent=agent,
                owner=owner,
                member=member,
                site=site,
                probes=probes,
                auth={"Authorization": "Bearer " + owner.key.token},
                foreign_auth={"Authorization": "Bearer " + foreign.key.token},
            )
    finally:
        await agent.close()
        await upstream.aclose()
        control.close()


async def grant_and_issue(fixture):
    client, auth = fixture.client, fixture.auth
    created = await client.post("/workspace/members", headers=auth, json={"user_id": "member"})
    assert created.status_code == 201, created.text
    assert created.json()["member"]["grants"] == {"site_ids": [], "connection_ids": []}
    granted = await client.patch(
        "/workspace/members/member",
        headers=auth,
        json={"site_ids": [fixture.site.id], "connection_ids": ["shared"]},
    )
    assert granted.status_code == 200, granted.text
    issued = await client.post(
        "/workspace/members/member/keys",
        headers=auth,
        json={"name": "Member agent", "site_ids": [fixture.site.id]},
    )
    assert issued.status_code == 201, issued.text
    assert issued.json()["key"]["user_id"] == "member"
    return {"Authorization": "Bearer " + issued.json()["token"]}


async def create_session(fixture, auth):
    response = await fixture.client.post(
        "/sessions", headers=auth, json={"site_id": fixture.site.id}
    )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


async def test_member_rest_execution_keeps_actor_and_hides_ungranted_connection(sharing):
    auth = await grant_and_issue(sharing)
    me = await sharing.client.get("/me", headers=auth)
    assert me.status_code == 200 and me.json()["user_id"] == "member"
    assert me.json()["can_manage_workspace"] is False
    assert me.json()["workspace"]["user_id"] == sharing.owner.user.id
    session_id = await create_session(sharing, auth)
    prefix = f"/sessions/{session_id}"
    connections = await sharing.client.get(prefix + "/connections", headers=auth)
    assert [account["id"] for account in connections.json()["connections"]] == ["shared"]
    resolution = await sharing.client.post(
        prefix + "/resolve", headers=auth, json={"capability": "get_current_power"}
    )
    assert resolution.json()["selected"]["account_id"] == "shared", resolution.text
    assert "private" not in resolution.text
    denied = await sharing.client.post(
        prefix + "/execute",
        headers=auth,
        json={
            "tool": "home_assistant.get_state",
            "account_id": "private",
            "arguments": {"entity_id": "sensor.private"},
        },
    )
    assert denied.json()["error"]["code"] == "account_forbidden" and not sharing.probes
    executed = await sharing.client.post(
        prefix + "/capability", headers=auth, json={"capability": "get_current_power"}
    )
    assert executed.json()["ok"] is True, executed.text
    assert len(sharing.probes) == 1
    assert "private-fixture-credential" not in executed.text
    event = sharing.agent.events[-1]
    assert event["user_id"] == "member" and event["workspace_id"] == sharing.owner.workspace.id
    for path in ["/workspace", "/workspace/members", "/workspace/keys", "/workspace/connections"]:
        assert (await sharing.client.get(path, headers=auth)).status_code == 403


async def test_existing_rest_session_loses_access_before_provider_io(sharing):
    auth = await grant_and_issue(sharing)
    session_id = await create_session(sharing, auth)
    replaced = await sharing.client.patch(
        "/workspace/members/member",
        headers=sharing.auth,
        json={"site_ids": [sharing.site.id], "connection_ids": []},
    )
    assert replaced.status_code == 200
    denied = await sharing.client.post(
        f"/sessions/{session_id}/execute",
        headers=auth,
        json={"tool": "home_assistant.get_state", "arguments": {"entity_id": "sensor.shared"}},
    )
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "workspace_forbidden"
    fresh = await create_session(sharing, auth)
    denied = await sharing.client.post(
        f"/sessions/{fresh}/execute",
        headers=auth,
        json={"tool": "home_assistant.get_state", "arguments": {"entity_id": "sensor.shared"}},
    )
    assert denied.json()["ok"] is False and not sharing.probes


async def test_foreign_connection_grants_and_member_key_escalation_are_denied(sharing):
    auth = await grant_and_issue(sharing)
    denied = await sharing.client.patch(
        "/workspace/members/member",
        headers=sharing.foreign_auth,
        json={"site_ids": [sharing.site.id], "connection_ids": ["shared"]},
    )
    assert denied.status_code >= 400 and not sharing.probes
    denied = await sharing.client.patch(
        "/workspace/members/member",
        headers=auth,
        json={"site_ids": [sharing.site.id], "connection_ids": ["private"]},
    )
    assert denied.status_code == 403 and not sharing.probes
    deleted = await sharing.client.delete("/workspace/members/member", headers=sharing.auth)
    assert deleted.status_code == 200
    assert (await sharing.client.get("/me", headers=auth)).status_code == 401
    created = await sharing.client.post(
        "/workspace/members", headers=sharing.auth, json={"user_id": "member"}
    )
    assert created.status_code == 201
    assert (await sharing.client.get("/me", headers=auth)).status_code == 401


async def initialize_mcp(sharing, auth):
    headers = {**auth, "Accept": "application/json, text/event-stream"}
    response = await sharing.client.post(
        f"/mcp/{sharing.site.id}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "sharing-fixture", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    await sharing.client.post(
        f"/mcp/{sharing.site.id}",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return headers


async def test_member_mcp_execution_revocation_and_same_actor_key_isolation(sharing):
    auth = await grant_and_issue(sharing)
    headers = await initialize_mcp(sharing, auth)
    session_id = await create_session(sharing, auth)
    second = sharing.control.create_member_key(
        sharing.owner.user.id,
        sharing.owner.workspace.id,
        "member",
        "Second key",
        access=AgentKeyAccess(site_ids=[sharing.site.id]),
    )
    second_auth = {"Authorization": "Bearer " + second.token}
    assert (
        await sharing.client.get(f"/sessions/{session_id}/connections", headers=second_auth)
    ).status_code == 404
    stolen = await sharing.client.post(
        f"/mcp/{sharing.site.id}",
        headers={**headers, **second_auth},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert stolen.status_code == 404, stolen.text
    response = await sharing.client.post(
        f"/mcp/{sharing.site.id}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "ENERGY_MULTI_EXECUTE_TOOL",
                "arguments": {
                    "calls": [
                        {
                            "tool": "home_assistant.get_state",
                            "arguments": {"entity_id": "sensor.shared"},
                        }
                    ],
                },
            },
        },
    )
    payload = _rpc(response)
    assert not payload["result"].get("isError"), payload
    assert len(sharing.probes) == 1
    await sharing.client.patch(
        "/workspace/members/member",
        headers=sharing.auth,
        json={"site_ids": [sharing.site.id], "connection_ids": []},
    )
    denied = await sharing.client.post(
        f"/mcp/{sharing.site.id}",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    )
    assert denied.status_code >= 400 and len(sharing.probes) == 1
    assert len(sharing.host._managed_mounts) == 1


async def test_member_datasets_and_jobs_remain_private_to_the_actor(sharing):
    auth = await grant_and_issue(sharing)
    member_id = await create_session(sharing, auth)
    owner_id = await create_session(sharing, sharing.auth)
    member_session = sharing.host._sessions[member_id].session
    owner_session = sharing.host._sessions[owner_id].session
    store = sharing.agent.workbench.partitioned
    dataset = store.ingest(
        member_session,
        EnergyResult(data=[], kind=DataKind.METERED, unit="kWh", source="fixture"),
        [{"value": 1}, {"value": 2}],
    )
    assert store.read_page(member_session, dataset["artifact_id"], limit=2)["rows"] == [
        {"value": 1},
        {"value": 2},
    ]
    with pytest.raises(EnergyError):
        store.read_page(owner_session.model_copy(update={"id": member_id}), dataset["artifact_id"])
    submitted = await sharing.agent.job(
        member_session, "submit", simulation="heat_loss", arguments=heat_loss_args()
    )
    assert submitted["ok"] is True
    job_id = submitted["job"]["job_id"]
    denied = await sharing.agent.job(owner_session, "status", job_id=job_id)
    assert denied["ok"] is False
    await sharing.agent._job_task
    result = await sharing.agent.job(member_session, "result", job_id=job_id)
    assert result["ok"] is True
    assert result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
    assert not sharing.probes
