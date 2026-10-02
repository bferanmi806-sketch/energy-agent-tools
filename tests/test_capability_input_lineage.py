import pytest

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.models import DataKind, EnergyResult, Tool, Toolkit, schema
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def _platform(tmp_path):
    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="fixture",
            name="Fixture",
            description="Local arithmetic",
            runtime="native",
            status="stable",
        )
    )
    calls = []

    async def calculate(arguments, context):
        calls.append(arguments)
        return EnergyResult(
            data={"total": arguments["load"] * arguments["hours"]},
            kind=DataKind.CALCULATED,
            unit="kWh",
            source="fixture-calculation",
        )

    registry.add(
        Tool(
            name="fixture.calculate",
            toolkit="fixture",
            description="Calculate explicit energy",
            capabilities=["calculate_energy"],
            input_schema=schema(
                {"load": {"type": "number"}, "hours": {"type": "number"}}, ["load", "hours"]
            ),
        ),
        calculate,
    )
    agent = EnergyAgent(
        registry,
        tmp_path,
        bindings=[
            CapabilityBinding(
                capability="calculate_energy",
                tool="fixture.calculate",
                reviewed=True,
                kind=DataKind.CALCULATED,
                unit="kWh",
            )
        ],
    )
    return EnergyAgentTools(tmp_path, agent=agent), calls


def _source(session):
    source = EnergyResult(
        data=[{"value": 3}],
        kind=DataKind.METERED,
        unit="kW",
        source="synthetic-load",
        quantity_shape="instantaneous",
    )
    return session.agent.workbench.persist(session.context, source)["artifact_id"]


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_capability_execution_retains_scoped_input_artifact_lineage(tmp_path, interface):
    energy, calls = _platform(tmp_path)
    async with energy:
        session = energy.session("owner")
        artifact_id = _source(session)
        arguments = {"load": 3, "hours": 2}
        if interface == "sdk":
            response = await session.capability(
                "calculate_energy", arguments, input_artifacts=[artifact_id]
            )
        else:
            response = await session.dispatch(
                "ENERGY_EXECUTE_CAPABILITY",
                {
                    "capability": "calculate_energy",
                    "arguments": arguments,
                    "input_artifacts": [artifact_id],
                },
            )
        assert response["ok"], response
        assert response["result"]["data"]["total"] == 6
        assert calls == [arguments]
        lineage = next(
            row for row in response["result"]["provenance"] if row.get("artifact_id") == artifact_id
        )
        assert lineage["input_kind"] == "metered"
        assert lineage["source"] == "synthetic-load"
        assert lineage["unit"] == "kW"


async def test_foreign_input_artifact_blocks_execution_before_handler_runs(tmp_path):
    energy, calls = _platform(tmp_path)
    async with energy:
        artifact_id = _source(energy.session("other"))
        response = await energy.session("owner").capability(
            "calculate_energy", {"load": 3, "hours": 2}, input_artifacts=[artifact_id]
        )
        assert not response["ok"], response
        assert response["error"]["code"] == "artifact_not_found"
        assert calls == []


async def test_input_artifacts_are_not_silently_ignored_by_unrelated_skills(tmp_path):
    energy, calls = _platform(tmp_path)
    async with energy:
        session = energy.session("owner")
        response = await session.skill("building-spike", {"input_artifacts": [_source(session)]})
        assert not response["ok"], response
        assert response["error"]["code"] == "invalid_skill_parameters"
        assert calls == []


async def test_capability_rejects_more_than_ten_input_artifacts_before_execution(tmp_path):
    energy, calls = _platform(tmp_path)
    async with energy:
        session = energy.session("owner")
        with pytest.raises(ValueError):
            await session.capability(
                "calculate_energy", {"load": 3, "hours": 2}, input_artifacts=[_source(session)] * 11
            )
        assert calls == []
