import csv

import pytest

from energy_agent_tools import EnergyAgentTools


async def _forecast(tmp_path, *, wind_value="3.6", temperature_unit="°C"):
    data = tmp_path / "data"
    data.mkdir()
    with (data / "weather.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "variable", "value", "unit"])
        writer.writeheader()
        for hour, ghi in [(12, 0), (13, 900)]:
            for variable, value, unit in (
                ("shortwave_radiation", ghi, "W/m²"),
                ("temperature_2m", 20, temperature_unit),
                ("wind_speed_10m", wind_value, "km/h"),
            ):
                writer.writerow(
                    {
                        "timestamp": f"2026-09-29T{hour}:00:00Z",
                        "variable": variable,
                        "value": value,
                        "unit": unit,
                    }
                )
    config = {
        "sites": [
            {
                "id": "home",
                "user_id": "owner",
                "name": "Synthetic solar site",
                "timezone": "UTC",
                "latitude": 51.45,
                "longitude": -2.59,
            }
        ]
    }
    async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
        session = energy.session("owner", "home")
        source = await session.execute(
            "CSV_READ_TIMESERIES",
            {
                "file": "weather.csv",
                "kind": "forecast",
                "unit": "mixed",
                "timezone": "UTC",
                "resolution": "1h",
            },
            persist=True,
        )
        assert source["ok"], source
        return await session.skill(
            "solar-forecast",
            {
                "artifacts": {"get_weather": source["result"]["data"]["artifact_id"]},
                "solar": {"dc_capacity_kw": 3},
            },
        )


async def test_csv_weather_forecast_executes_real_pv_model(tmp_path):
    response = await _forecast(tmp_path)
    assert response["ok"], response
    result = response["evidence"][-1]["analysis"]["result"]
    assert result["kind"] == "estimated"
    assert result["source"] == "pvlib"
    intervals = result["data"]["intervals"]
    assert intervals[0]["ac_power_kw"] == pytest.approx(0, abs=1e-9)
    assert 0 < intervals[1]["ac_power_kw"] <= 3
    assert {row["source"] for row in result["provenance"] if "artifact_id" in row} == {
        "local-csv",
        "workbench",
    }


@pytest.mark.parametrize("value", ["", "not-a-number", "inf"])
async def test_solar_refuses_invalid_csv_wind_values(tmp_path, value):
    response = await _forecast(tmp_path, wind_value=value)
    assert not response["ok"], response
    assert response["error"]["code"] == "invalid_value"


async def test_solar_refuses_temperature_with_incompatible_units(tmp_path):
    response = await _forecast(tmp_path, temperature_unit="K")
    assert not response["ok"], response
    assert response["error"]["code"] == "unit_incompatible"
