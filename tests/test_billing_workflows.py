from decimal import Decimal

from mcp.shared.memory import create_connected_server_and_client_session

from energy_agent_tools.connectors import local
from energy_agent_tools.models import DataKind, EnergyResult, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent
from energy_agent_tools.server import create_server
from energy_agent_tools.workflows import run_skill


def _agent(root):
    registry = Registry()
    local.register(registry)
    return EnergyAgent(
        registry, root, sites=[Site(id="home", user_id="owner", name="Home", timezone="UTC")]
    )


def _inputs(agent, session, *, gap=False, rate=0.2):
    rows = [
        {
            "timestamp": f"2026-01-0{day}T00:00:00Z",
            "end": f"2026-01-0{day + 1}T00:00:00Z",
            "value": 5,
        }
        for day in ((1,) if gap else (1, 2))
    ]
    energy = EnergyResult(
        data=rows,
        kind=DataKind.METERED,
        unit="kWh",
        source="synthetic-meter",
        timezone="UTC",
        quantity_shape="interval",
        resolution="1D",
        site_id="home",
    )
    tariff = energy.model_copy(
        update={
            "data": [{**row, "value": rate} for row in rows],
            "kind": DataKind.FORECAST,
            "unit": "GBP/kWh",
            "source": "synthetic-tariff",
        }
    )
    return {
        "get_energy_consumption": agent.workbench.persist(session, energy)["artifact_id"],
        "get_tariff": agent.workbench.persist(session, tariff)["artifact_id"],
    }


def _schedule(amount=0.5, tax=0.05):
    return {
        "standing_charge": {"amount_per_day": amount, "currency": "GBP", "taxable": True},
        "tax": {"rate": tax, "energy_taxable": True},
        "source": "caller-defined synthetic tariff, unit rates exclude this tax",
    }


def _params(inputs):
    return {
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-03T00:00:00Z",
        "artifacts": inputs,
        "billing": _schedule(),
    }


async def test_cost_workflow_mcp_returns_a_bill_and_traceable_components(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        inputs = _inputs(agent, session)
        server = create_server(agent, session)
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()
            result = await client.call_tool(
                "ENERGY_RUN_SKILL", {"skill_id": "electricity-cost", "parameters": _params(inputs)}
            )
            response = result.structuredContent
        assert response["ok"], response
        bill = response["evidence"][-1]["analysis"]["result"]
        assert bill["kind"] == "calculated"
        assert Decimal(bill["data"]["total"]) == Decimal("3.15")
        assert Decimal(bill["data"]["energy_cost"]) == 2
        assert Decimal(bill["data"]["standing_charge"]) == 1
        assert bill["data"]["chargeable_days"] == 2
        cost = next(item["energy_cost"] for item in response["evidence"] if "energy_cost" in item)
        ref = cost["result"]["data"]["artifact_id"]
        artifact = agent.workbench.read(session, ref)
        assert artifact.data[-1]["end"] == "2026-01-03T00:00:00+00:00"
        assert artifact.quantity_shape == "interval"
        lineage = bill["provenance"][0]["inputs"][0]
        assert lineage["artifact_id"] == ref
        assert lineage["source_kind"] == "calculated"
        assert (
            agent.workbench.read(session, inputs["get_energy_consumption"]).kind == DataKind.METERED
        )
        foreign = agent.session("outsider")
        assert not (
            await agent.execute(
                foreign,
                "WORKBENCH_ENERGY_OPERATION",
                {
                    "operation": "bill",
                    "artifact_ids": [ref],
                    "parameters": {
                        **_schedule(),
                        "start": "2026-01-01T00:00:00Z",
                        "finish": "2026-01-03T00:00:00Z",
                        "timezone": "UTC",
                    },
                },
            )
        )["ok"]
    finally:
        await agent.close()


async def test_tariff_comparison_requires_distinct_component_schedules(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        inputs = _inputs(agent, session)
        alt = _inputs(agent, session, rate=0.3)["get_tariff"]
        parameters = {**_params(inputs), "alternative_tariff": alt}
        missing = await run_skill(agent, session, "tariff-comparison", parameters)
        assert not missing["ok"]
        parameters["alternative_billing"] = _schedule(0.25, 0)
        response = await run_skill(agent, session, "tariff-comparison", parameters)
        assert response["ok"], response
        primary = response["evidence"][-1]["analysis"]["result"]["data"]
        alternative = next(
            item["alternative_tariff"]
            for item in response["evidence"]
            if "alternative_tariff" in item
        )["result"]["data"]
        assert Decimal(primary["total"]) == Decimal("3.15")
        assert Decimal(alternative["total"]) == Decimal("3.5")
    finally:
        await agent.close()


async def test_bill_refuses_incomplete_coverage_instead_of_charging_a_complete_day(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        response = await run_skill(
            agent, session, "electricity-cost", _params(_inputs(agent, session, gap=True))
        )
        assert not response["ok"], response
        assert response["evidence"][-1]["analysis"]["error"]["code"] == "insufficient_coverage"
    finally:
        await agent.close()
