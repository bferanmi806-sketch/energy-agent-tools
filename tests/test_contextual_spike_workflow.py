from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.models import DataKind, EnergyResult


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_spike_workflow_combines_scoped_observed_context(tmp_path, interface):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=24)
    config = {"sites": [{"id": "home", "user_id": "owner", "name": "Home", "timezone": "UTC"}]}

    def source(values, unit, asset, kind=DataKind.METERED, endpoint="end"):
        return EnergyResult(
            data=[
                {
                    "timestamp": (start + timedelta(hours=i)).isoformat(),
                    endpoint: (start + timedelta(hours=i + 1)).isoformat(),
                    "value": value,
                    "physical_meter": False,
                }
                for i, value in enumerate(values)
            ],
            kind=kind,
            unit=unit,
            source="synthetic-context-contract",
            site_id="home",
            asset_id=asset,
            quantity_shape="interval",
        )

    async with EnergyAgentTools(tmp_path / "state", config) as energy:
        session = energy.session("owner", "home")
        load = [1 + i * 0.01 if i != 12 else 5 for i in range(24)]
        equipment = [0.2 if i != 12 else 4.2 for i in range(24)]
        weather = [15 + i if i != 12 else 45 for i in range(24)]
        refs = [
            energy.agent.workbench.persist(session.context, result)["artifact_id"]
            for result in (
                source(load, "kWh", "meter", endpoint="to"),
                source(equipment, "kWh", "oven"),
                source(weather, "°C", "weather-station"),
            )
        ]
        parameters = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "artifacts": {"get_energy_consumption": refs[0]},
            "spike_context": {
                "weather_artifact": refs[2],
                "equipment_artifacts": [refs[1]],
                "parameters": {"equipment_end_column": "end"},
            },
        }
        if interface == "sdk":
            output = await session.skill("building-spike", parameters)
        else:
            output = await session.dispatch(
                "ENERGY_RUN_SKILL",
                {
                    "skill_id": "building-spike",
                    "parameters": parameters,
                },
            )
        assert output["ok"], output
        result = output["evidence"][-1]["analysis"]["result"]
        assert result["kind"] == "calculated"
        assert result["data"]["summary"]["spike_count"] == 1
        spike = result["data"]["spikes"][0]
        assert spike["baseline_kwh"] == pytest.approx(1.095)
        assert spike["excess_kwh"] == pytest.approx(3.905)
        evidence = {item["type"]: item for item in spike["supported_explanations"]}
        assert set(evidence) == {"coincident_equipment_increase", "observed_weather_association"}
        assert evidence["coincident_equipment_increase"]["asset_id"] == "oven"
        assert evidence["coincident_equipment_increase"][
            "overlap_with_site_excess_kwh"
        ] == pytest.approx(3.905)
        assert evidence["observed_weather_association"][
            "temperature_load_correlation"
        ] == pytest.approx(1)
        assert all(
            "does not establish cause" in item["interpretation"] for item in evidence.values()
        )

        foreign = energy.agent.workbench.persist(
            energy.session("other").context, source(weather, "°C", "foreign")
        )["artifact_id"]
        parameters["spike_context"]["weather_artifact"] = foreign
        rejected = await session.skill("building-spike", parameters)
        assert not rejected["ok"] and rejected["error"]["code"] == "artifact_not_found"
