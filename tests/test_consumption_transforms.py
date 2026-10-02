from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools.connectors import local
from energy_agent_tools.models import DataKind, EnergyResult, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent
from energy_agent_tools.workflows import run_skill


def _agent(root):
    registry = Registry()
    local.register(registry)
    return EnergyAgent(
        registry, root, sites=[Site(id="home", user_id="owner", name="Home", timezone="UTC")]
    )


def _source(agent, session, *, shape="counter", kind=DataKind.METERED, unit="kWh"):
    start = datetime(2026, 9, 29, tzinfo=UTC)
    values = [100 + i for i in range(48)] + [150] if shape == "counter" else [2] * 49
    result = EnergyResult(
        data=[
            {"timestamp": (start + timedelta(minutes=30 * i)).isoformat(), "value": value}
            for i, value in enumerate(values)
        ],
        kind=kind,
        unit=unit,
        quantity_shape=shape,
        resolution="30min",
        source="meter-fixture",
        provenance=[{"sample": True, "physical_meter": False}],
        site_id="home",
    )
    return agent.workbench.persist(session, result)["artifact_id"]


def _parameters(ref, operation="counter"):
    return {
        "start": "2026-09-29T00:00:00Z",
        "end": "2026-09-30T00:00:00Z",
        "artifacts": {"get_energy_consumption": ref},
        "consumption_transform": {"operation": operation, "parameters": {"column": "value"}},
    }


async def test_counter_workflow_transforms_before_window_and_retains_calculated_lineage(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        ref = _source(agent, session)
        response = await run_skill(agent, session, "yesterday-consumption", _parameters(ref))
        assert response["ok"], response
        analysis = response["evidence"][-1]["analysis"]["result"]
        assert analysis["data"]["sum"] == 50
        assert analysis["provenance"][0]["input_kind"] == "calculated"
        transform = next(item["transform"] for item in response["evidence"] if "transform" in item)
        transformed = agent.workbench.read(session, transform["result"]["data"]["artifact_id"])
        assert transformed.kind == DataKind.CALCULATED
        assert transformed.quantity_shape == "interval"
        assert transformed.provenance[0]["inputs"][0]["artifact_id"] == ref
        assert transformed.provenance[0]["inputs"][0]["kind"] == "metered"
        assert agent.workbench.read(session, ref).quantity_shape == "counter"
    finally:
        await agent.close()


async def test_power_workflow_integrates_explicitly(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        ref = _source(agent, session, shape="instantaneous", unit="kW")
        response = await run_skill(
            agent, session, "yesterday-consumption", _parameters(ref, "integrate_power")
        )
        assert response["ok"], response
        assert response["evidence"][-1]["analysis"]["result"]["data"]["sum"] == 48
    finally:
        await agent.close()


@pytest.mark.parametrize(
    "kind,shape,operation",
    [
        (DataKind.SIMULATED, "counter", "counter"),
        (DataKind.METERED, "interval", "counter"),
        (DataKind.METERED, "counter", "integrate_power"),
    ],
)
async def test_transform_refuses_wrong_source_contract(tmp_path, kind, shape, operation):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        ref = _source(agent, session, kind=kind, shape=shape)
        response = await run_skill(
            agent, session, "yesterday-consumption", _parameters(ref, operation)
        )
        assert not response["ok"]
        assert response["error"]["code"] == "incompatible_source"
        assert response["evidence"] == []
    finally:
        await agent.close()


@pytest.mark.parametrize(
    "transform",
    [
        {"operation": "shell"},
        {"operation": "counter", "extra": True},
        {"operation": "counter", "parameters": []},
    ],
)
async def test_transform_contract_is_closed_at_boundary(tmp_path, transform):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        ref = _source(agent, session)
        parameters = _parameters(ref)
        parameters["consumption_transform"] = transform
        response = await run_skill(agent, session, "yesterday-consumption", parameters)
        assert not response["ok"]
        assert response["error"]["code"] == "invalid_skill_parameters"
        assert response["evidence"] == []
    finally:
        await agent.close()


async def test_transformed_power_cost_uses_energy_and_price_columns(tmp_path):
    agent = _agent(tmp_path)
    try:
        session = agent.session("owner", "home")
        ref = _source(agent, session, shape="instantaneous", unit="kW")
        start = datetime(2026, 9, 29, tzinfo=UTC)
        tariff = EnergyResult(
            data=[
                {
                    "timestamp": (start + timedelta(minutes=30 * i)).isoformat(),
                    "to": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
                    "value": 0.2,
                }
                for i in range(48)
            ],
            kind=DataKind.FORECAST,
            unit="GBP/kWh",
            source="tariff-fixture",
            resolution="30min",
        )
        tariff_ref = agent.workbench.persist(session, tariff)["artifact_id"]
        parameters = _parameters(ref, "integrate_power")
        parameters["artifacts"]["get_tariff"] = tariff_ref
        response = await run_skill(agent, session, "electricity-cost", parameters)
        assert response["ok"], response
        rows = response["evidence"][-1]["analysis"]["result"]["data"]
        assert sum(row["cost"] for row in rows) == pytest.approx(9.6)
    finally:
        await agent.close()


async def test_mcp_executes_transform_without_promoting_derived_energy_to_metered(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    from energy_agent_tools.server import create_server

    agent = _agent(tmp_path)
    session = agent.session("owner", "home")
    ref = _source(agent, session)
    async with create_connected_server_and_client_session(create_server(agent, session)) as client:
        response = await client.call_tool(
            "ENERGY_RUN_SKILL",
            {"skill_id": "yesterday-consumption", "parameters": _parameters(ref)},
        )
        result = response.structuredContent
        assert result["ok"], result
        analysis = result["evidence"][-1]["analysis"]["result"]
        assert analysis["data"]["sum"] == 50
        assert analysis["provenance"][0]["input_kind"] == "calculated"


async def test_foreign_user_cannot_transform_owner_artifact(tmp_path):
    agent = _agent(tmp_path)
    try:
        owner = agent.session("owner", "home")
        ref = _source(agent, owner)
        outsider = agent.session("outsider")
        result = await run_skill(agent, outsider, "yesterday-consumption", _parameters(ref))
        assert not result["ok"]
        assert result["error"]["code"] == "artifact_not_found"
        assert result["evidence"] == []
    finally:
        await agent.close()
