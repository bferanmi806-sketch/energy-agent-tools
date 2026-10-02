"""Home meter, PV estimate, and battery advisory using only local fixtures."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.reference_projects._shared import (
    USER_ID,
    execute_capability,
    import_csv,
    make_tools,
    missing_consumption_evidence,
    numeric_rows,
    site_config,
    source_summary,
)

SITE_ID = "bristol-home"
TIMEZONE = "Europe/London"
PV_ASSET = "home-roof-pv"
BATTERY_ASSET = "home-battery"
METER_ASSET = "home-meter"


async def main() -> None:
    config = site_config(
        site_id=SITE_ID,
        name="Bristol reference home",
        timezone=TIMEZONE,
        latitude=51.45,
        longitude=-2.59,
        assets=[
            {
                "id": METER_ASSET,
                "site_id": SITE_ID,
                "kind": "electricity-meter",
                "name": "Main meter",
            },
            {"id": PV_ASSET, "site_id": SITE_ID, "kind": "solar-array", "name": "2 kW rooftop PV"},
            {
                "id": BATTERY_ASSET,
                "site_id": SITE_ID,
                "kind": "home-battery",
                "name": "2 kWh battery",
            },
        ],
    )
    with TemporaryDirectory(prefix="energy-home-reference-") as temporary:
        async with make_tools(Path(temporary) / "state", config) as tools:
            session = tools.session(USER_ID, SITE_ID)
            meter_id, meter = await import_csv(
                session,
                "home_meter.csv",
                kind="metered",
                unit="kWh",
                timezone=TIMEZONE,
                asset_id=METER_ASSET,
                quantity_shape="interval",
                resolution="30min",
            )
            tariff_id, tariff = await import_csv(
                session,
                "home_tariff_forecast.csv",
                kind="forecast",
                unit="GBP/kWh",
                timezone=TIMEZONE,
                quantity_shape="interval",
                resolution="30min",
            )
            weather_id, weather = await import_csv(
                session,
                "home_weather_forecast.csv",
                kind="forecast",
                unit="W/m2, degC, m/s",
                timezone=TIMEZONE,
                quantity_shape="instantaneous",
                resolution="30min",
            )
            meter_rows = numeric_rows(meter, ("value",))
            tariff_rows = numeric_rows(tariff, ("value",))
            weather_rows = numeric_rows(
                weather,
                ("ghi_w_m2", "temp_air_c", "wind_speed_m_s", "duration_hours"),
            )
            meter_summary = await session.execute(
                "WORKBENCH_SUMMARIZE", {"artifact_id": meter_id, "column": "value"}
            )
            assert meter_summary["ok"], meter_summary
            assert abs(meter_summary["result"]["data"]["sum"] - 1.3) < 1e-9
            cost_workflow = await session.skill(
                "electricity-cost",
                {
                    "start": "2026-06-21T11:00:00+01:00",
                    "end": "2026-06-21T13:00:00+01:00",
                    "artifacts": {
                        "get_energy_consumption": meter_id,
                        "get_tariff": tariff_id,
                    },
                },
            )
            assert cost_workflow["ok"], cost_workflow
            cost_analysis = next(
                item["analysis"] for item in cost_workflow["evidence"] if "analysis" in item
            )
            cost_rows = cost_analysis["result"]["data"]
            assert abs(sum(row["cost"] for row in cost_rows) - 0.28) < 1e-9

            pv = await execute_capability(
                session,
                "estimate_solar_generation",
                {
                    "latitude": 51.45,
                    "longitude": -2.59,
                    "timezone": TIMEZONE,
                    "dc_capacity_kw": 2.0,
                    "inverter_capacity_kw": 2.0,
                    "surface_tilt_deg": 30.0,
                    "surface_azimuth_deg": 180.0,
                    "weather_source": "local-csv:home_weather_forecast.csv",
                    "weather_kind": weather.kind.value,
                    "weather_rows": weather_rows,
                },
                asset_id=PV_ASSET,
                input_artifact_ids=[weather_id],
            )
            assert pv["kind"] == "estimated"
            assert pv["data"]["total_energy_kwh"] > 0
            assert any(
                item.get("data_kind") == "forecast" and item.get("rows") == 4
                for item in pv["provenance"]
            )
            pv_intervals = pv["data"]["intervals"]
            assert len(pv_intervals) == len(meter_rows) == len(tariff_rows) == 4

            battery_intervals = []
            for meter_row, tariff_row, pv_row in zip(
                meter_rows, tariff_rows, pv_intervals, strict=True
            ):
                assert meter_row["timestamp"] == tariff_row["timestamp"] == pv_row["timestamp"]
                duration_hours = (
                    datetime.fromisoformat(meter_row["end"])
                    - datetime.fromisoformat(meter_row["timestamp"])
                ).total_seconds() / 3600.0
                assert duration_hours > 0
                assert abs(duration_hours - pv_row["duration_hours"]) < 1e-12
                battery_intervals.append(
                    {
                        "timestamp": meter_row["timestamp"],
                        "duration_hours": duration_hours,
                        "load_kw": meter_row["value"] / duration_hours,
                        "pv_kw": pv_row["ac_power_kw"],
                        "price_per_kwh": tariff_row["value"],
                    }
                )

            battery = await execute_capability(
                session,
                "plan_battery_charging",
                {
                    "intervals": battery_intervals,
                    "battery": {
                        "capacity_kwh": 2.0,
                        "initial_soc_kwh": 1.0,
                        "min_soc_kwh": 0.2,
                        "target_final_soc_kwh": 1.0,
                        "max_charge_kw": 1.0,
                        "max_discharge_kw": 1.0,
                        "charge_efficiency": 0.95,
                        "discharge_efficiency": 0.95,
                    },
                    "objective": "cost",
                    "allow_grid_charging": True,
                    "allow_grid_export": False,
                    "timezone": TIMEZONE,
                },
                asset_id=BATTERY_ASSET,
                input_artifact_ids=[meter_id, tariff_id, weather_id],
            )
            assert battery["kind"] == "simulated"
            summary = battery["data"]["summary"]
            assert abs(summary["final_soc_kwh"] - 1.0) < 1e-5
            assert summary["cost_savings"] >= -1e-7
            assert len(battery["data"]["schedule"]) == 4

            refusal = missing_consumption_evidence(session, asset_id=METER_ASSET)
            assert sum(row["value"] for row in meter_rows) == 1.3
            print(
                json.dumps(
                    {
                        "project": "home-meter-pv-battery",
                        "site": {"id": SITE_ID, "timezone": TIMEZONE},
                        "assets": [METER_ASSET, PV_ASSET, BATTERY_ASSET],
                        "sources": [
                            source_summary(meter_id, meter),
                            source_summary(tariff_id, tariff),
                            source_summary(weather_id, weather),
                        ],
                        "truths": {"metered_energy_kwh": 1.3, "meter_intervals": 4},
                        "workbench_summary": meter_summary["result"]["data"],
                        "electricity_cost_workflow": {
                            "skill_id": cost_workflow["skill_id"],
                            "kind": cost_analysis["result"]["kind"],
                            "total_gbp": sum(row["cost"] for row in cost_rows),
                            "evidence_count": len(cost_workflow["evidence"]),
                        },
                        "pv": {
                            "kind": pv["kind"],
                            "estimated_energy_kwh": pv["data"]["total_energy_kwh"],
                            "source_data_kind": "forecast",
                            "asset_id": pv["asset_id"],
                            "provenance": pv["provenance"],
                        },
                        "battery": {
                            "kind": battery["kind"],
                            "final_soc_kwh": summary["final_soc_kwh"],
                            "cost_savings_gbp": summary["cost_savings"],
                            "asset_id": battery["asset_id"],
                            "provenance": battery["provenance"],
                        },
                        "missing_evidence_refusal": refusal,
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
