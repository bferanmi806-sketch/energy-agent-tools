import pytest

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.models import DataKind


async def _run(
    tmp_path, skill, *, carbon_end="02:00:00", missing_carbon=False, requested_end="02:00:00"
):
    data = tmp_path / "data"
    data.mkdir()
    (data / "price.csv").write_text(
        "timestamp,end,value\n2026-09-29T00:00:00Z,2026-09-29T01:00:00Z,0.1\n2026-09-29T01:00:00Z,2026-09-29T02:00:00Z,0.3\n"
    )
    (data / "carbon.csv").write_text(
        f"timestamp,end,value\n2026-09-29T00:00:00Z,2026-09-29T01:00:00Z,180\n2026-09-29T01:00:00Z,2026-09-29T{carbon_end}Z,{'' if missing_carbon else '60'}\n"
    )
    config = {
        "sites": [{"id": "home", "user_id": "owner", "name": "Synthetic home", "timezone": "UTC"}]
    }
    async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
        session = energy.session("owner", "home")
        artifacts = {}
        for capability, filename, unit in (
            ("get_tariff", "price.csv", "GBP/kWh"),
            ("get_carbon_intensity", "carbon.csv", "gCO2e/kWh"),
        ):
            response = await session.execute(
                "CSV_READ_TIMESERIES",
                {
                    "file": filename,
                    "kind": "forecast",
                    "unit": unit,
                    "timezone": "UTC",
                    "quantity_shape": "interval",
                    "resolution": "1h",
                },
                persist=True,
            )
            assert response["ok"], response
            ref = response["result"]["data"]["artifact_id"]
            assert energy.agent.workbench.read(session.context, ref).kind == DataKind.FORECAST
            artifacts[capability] = ref
        return await session.skill(
            skill,
            {
                "start": "2026-09-29T00:00:00Z",
                "end": f"2026-09-29T{requested_end}Z",
                "artifacts": artifacts,
                "battery": {
                    "capacity_kwh": 2,
                    "initial_soc_kwh": 0,
                    "target_final_soc_kwh": 1,
                    "max_charge_kw": 1,
                    "max_discharge_kw": 1,
                    "charge_efficiency": 1,
                    "discharge_efficiency": 1,
                },
            },
        )


@pytest.mark.parametrize(
    "skill,expected_hour", [("cheapest-battery", "00:00:00"), ("cleanest-battery", "01:00:00")]
)
async def test_csv_forecasts_plan_distinct_cheapest_and_cleanest_charge_hours(
    tmp_path, skill, expected_hour
):
    response = await _run(tmp_path, skill)
    assert response["ok"], response
    result = response["evidence"][-1]["analysis"]["result"]
    assert result["kind"] == "simulated"
    alignment = next(item["alignment"] for item in response["evidence"] if "alignment" in item)
    alignment_id = alignment["result"]["data"]["artifact_id"]
    aligned_input = next(
        row for row in result["provenance"] if row.get("artifact_id") == alignment_id
    )
    assert aligned_input["input_kind"] == "calculated"
    assert aligned_input["source"] == "workbench"
    assert {row["source"] for row in aligned_input["provenance"] if "artifact_id" in row} == {
        "local-csv"
    }
    schedule = result["data"]["schedule"]
    assert schedule[-1]["soc_kwh"] == pytest.approx(1, abs=1e-6)
    assert sum(row["charge_kw"] * row["duration_hours"] for row in schedule) == pytest.approx(
        1, abs=1e-6
    )
    charged = [row for row in schedule if row["charge_kw"] > 0.99]
    assert len(charged) == 1
    assert expected_hour in charged[0]["timestamp"]


async def test_battery_refuses_different_tariff_and_carbon_interval_ends(tmp_path):
    response = await _run(tmp_path, "cheapest-battery", carbon_end="01:30:00")
    assert not response["ok"], response
    assert response["error"]["code"] == "interval_mismatch"


async def test_battery_refuses_empty_csv_carbon_cells(tmp_path):
    response = await _run(tmp_path, "cleanest-battery", missing_carbon=True)
    assert not response["ok"], response
    assert response["error"]["code"] == "invalid_value"


async def test_battery_refuses_forecasts_shorter_than_requested_horizon(tmp_path):
    response = await _run(tmp_path, "cheapest-battery", requested_end="03:00:00")
    assert not response["ok"], response
    assert response["error"]["code"] == "incomplete_coverage"
