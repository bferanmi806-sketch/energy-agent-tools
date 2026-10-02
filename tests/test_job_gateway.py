"""Prove policy enforcement and authenticated job recovery across a host restart."""

import asyncio

import httpx
import pytest

from energy_agent_tools.app import build_agent
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import Action
from energy_agent_tools.sdk import EnergyAgentTools

ARGS = {
    "indoor_temp_c": 21,
    "outdoor_temp_c": 2,
    "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
    "air_changes_per_hour": 0.4,
}
CONFIG = {
    "sites": [
        {"id": "home", "user_id": "alice", "name": "Home", "timezone": "UTC"},
        {"id": "other", "user_id": "alice", "name": "Other", "timezone": "UTC"},
    ]
}


async def test_job_policy_schema_and_sdk_mcp_scope(tmp_path):
    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        session = energy.session("alice", "home")
        denied = energy.session("alice", "home", allowed_actions={Action.READ})
        assert (await denied.job("submit", simulation="heat_loss", arguments=ARGS))["error"][
            "code"
        ] == "policy_denied"
        invalid = await session.job("submit", simulation="heat_loss", arguments={"command": "evil"})
        assert not invalid["ok"]
        submitted = await session.dispatch(
            "ENERGY_SIMULATION_JOB",
            {
                "operation": "submit",
                "simulation": "heat_loss",
                "arguments": ARGS,
            },
        )
        assert submitted["ok"], submitted
        job_id = submitted["job"]["job_id"]
        outsider = energy.session("bob")
        assert not (await outsider.job("resume", job_id=job_id))["ok"]
        assert not (await energy.session("alice", "other").job("resume", job_id=job_id))["ok"]
        wrong_site = energy.session("alice", "other", id=session.id)
        assert (await wrong_site.job("result", job_id=job_id))["error"]["code"] == "site_forbidden"
        await asyncio.wait_for(energy.agent._job_task, timeout=30)
        result = await session.job("result", job_id=job_id)
        assert result["ok"], result
        assert result["result"]["data"]["gross_heat_loss_kw"] == 0.38
        assert result["result"]["kind"] == "calculated"


async def test_rest_recovers_completed_job_after_restart(tmp_path):
    principals = {"alice": Principal("alice", {"home"}, token_digest("local-test"))}
    headers = {"Authorization": "Bearer local-test"}
    agent = build_agent(tmp_path, CONFIG)
    try:
        host = create_host(agent, principals)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost", headers=headers
        ) as client:
            sid = (await client.post("/sessions", json={"site_id": "home"})).json()["session_id"]
            response = await client.post(
                f"/sessions/{sid}/jobs",
                json={"operation": "submit", "simulation": "heat_loss", "arguments": ARGS},
            )
            job_id = response.json()["job"]["job_id"]
            await asyncio.wait_for(agent._job_task, timeout=30)
    finally:
        await agent.close()
    agent = build_agent(tmp_path, CONFIG)
    try:
        host = create_host(agent, principals)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost", headers=headers
        ) as client:
            response = await client.post(
                "/sessions", json={"site_id": "home", "resume_job_id": job_id}
            )
            assert response.status_code == 200, response.text
            assert response.json()["session_id"] == sid
            result = (
                await client.post(
                    f"/sessions/{sid}/jobs", json={"operation": "result", "job_id": job_id}
                )
            ).json()
            assert result["ok"], result
            assert result["result"]["data"]["gross_heat_loss_kw"] == 0.38
    finally:
        await agent.close()


async def test_completed_jobs_obey_current_toolkit_and_site_scope(tmp_path):
    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        session = energy.session("alice", "home")
        submitted = await session.job("submit", simulation="heat_loss", arguments=ARGS)
        job_id = submitted["job"]["job_id"]
        await asyncio.wait_for(energy.agent._job_task, timeout=30)
        limited = energy.session("alice", "home", id=session.id, toolkits={"workbench"})
        for operation in ("status", "result", "cancel", "delete", "resume"):
            response = await limited.job(operation, job_id=job_id)
            assert response["error"]["code"] == "tool_forbidden", response
        assert (await limited.job("list"))["jobs"] == []
        wrong_site = energy.session("alice", "other", id=session.id)
        assert (await wrong_site.job("list"))["jobs"] == []
        assert (await session.job("result", job_id=job_id))["ok"]


async def test_pypsa_jobs_use_real_workers_and_enforce_scope(tmp_path):
    from benchmarks.engineering_environments import _pypsa_args

    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        session = energy.session("alice", "home")
        flow = _pypsa_args(1.0, line_id="feeder")
        dispatch = {
            "network": {
                "buses": flow["network"]["buses"],
                "lines": [
                    {
                        "id": "feeder",
                        "from_bus": "grid",
                        "to_bus": "load",
                        "x_ohm": 0.4,
                        "s_nom_mva": 1.0,
                    }
                ],
                "loads": [{"id": "demand", "bus": "load", "p_mw": 2.0}],
                "generators": [
                    {"id": "cheap", "bus": "grid", "p_nom_mw": 10.0, "marginal_cost": 10.0},
                    {"id": "local", "bus": "load", "p_nom_mw": 10.0, "marginal_cost": 50.0},
                ],
            }
        }
        ids = []
        for simulation, arguments in (("network_power_flow", flow), ("network_dispatch", dispatch)):
            denied = energy.session("alice", "home", allowed_actions={Action.READ})
            assert (await denied.job("submit", simulation=simulation, arguments=arguments))[
                "error"
            ]["code"] == "policy_denied"
            submitted = await session.job("submit", simulation=simulation, arguments=arguments)
            assert submitted["ok"], submitted
            ids.append(submitted["job"]["job_id"])
        await asyncio.wait_for(energy.agent._job_task, timeout=40)
        results = []
        for job_id in ids:
            response = await session.job("result", job_id=job_id)
            assert response["ok"], response
            results.append(response["result"])
            assert response["result"]["kind"] == "simulated"
            assert any(item.get("library") == "pypsa" for item in response["result"]["provenance"])
            assert (await energy.session("bob").job("resume", job_id=job_id))["error"][
                "code"
            ] == "job_access_denied"
            limited = energy.session("alice", "home", id=session.id, toolkits={"engineering"})
            assert (await limited.job("result", job_id=job_id))["error"]["code"] == "tool_forbidden"
        assert results[0]["data"]["totals"]["balance_error_mw"] == pytest.approx(0, abs=1e-6)
        assert results[1]["data"]["objective_currency_per_hour"] == pytest.approx(60, abs=1e-6)
        assert {row["id"]: row["dispatch_mw"] for row in results[1]["data"]["generators"]} == {
            "cheap": 1.0,
            "local": 1.0,
        }
