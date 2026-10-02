"""Building meter, weather outlook, and heat-pump sizing reference project."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.reference_projects._shared import (
    USER_ID,
    execute_capability,
    import_csv,
    make_tools,
    numeric_rows,
    site_config,
    source_summary,
)

SITE_ID = "leeds-office"
TIMEZONE = "Europe/London"
METER_ASSET = "office-main-meter"
HEAT_PUMP_ASSET = "office-heat-pump"


async def main() -> None:
    components = [
        {"name": "walls", "area_m2": 100.0, "u_value_w_m2k": 0.3},
        {"name": "roof", "area_m2": 80.0, "u_value_w_m2k": 0.2},
        {"name": "floor", "area_m2": 80.0, "u_value_w_m2k": 0.25},
        {"name": "windows", "area_m2": 20.0, "u_value_w_m2k": 1.2},
        {"name": "doors", "area_m2": 2.0, "u_value_w_m2k": 0.5},
    ]
    rated_output_kw = 2.0
    declared_cop = 2.8
    config = site_config(
        site_id=SITE_ID,
        name="Leeds reference office",
        timezone=TIMEZONE,
        latitude=53.80,
        longitude=-1.55,
        assets=[
            {
                "id": METER_ASSET,
                "site_id": SITE_ID,
                "kind": "electricity-meter",
                "name": "Office main meter",
            },
            {
                "id": HEAT_PUMP_ASSET,
                "site_id": SITE_ID,
                "kind": "heat-pump",
                "name": "Air-source heat pump",
                "metadata": {
                    "rated_output_kw": rated_output_kw,
                    "declared_cop_at_design": declared_cop,
                    "source": "reference-project caller assumption",
                },
            },
        ],
        bindings=[
            {
                "capability": "calculate_heat_loss",
                "tool": "engineering.calculate_heat_loss",
                "asset_id": HEAT_PUMP_ASSET,
                "kind": "calculated",
                "unit": "kW_th and kWh_th",
                "quality": "reviewed-reference-assumptions",
                "reviewed": True,
            }
        ],
    )
    with TemporaryDirectory(prefix="energy-building-reference-") as temporary:
        async with make_tools(Path(temporary) / "state", config) as tools:
            session = tools.session(USER_ID, SITE_ID)
            meter_id, meter = await import_csv(
                session,
                "building_meter.csv",
                kind="metered",
                unit="kWh",
                timezone=TIMEZONE,
                asset_id=METER_ASSET,
                quantity_shape="interval",
                resolution="1h",
            )
            weather_id, weather = await import_csv(
                session,
                "building_weather_forecast.csv",
                kind="forecast",
                unit="degC",
                timezone=TIMEZONE,
                quantity_shape="instantaneous",
                resolution="1h",
            )
            consumption_rows = numeric_rows(meter, ("kwh",))
            weather_rows = numeric_rows(weather, ("outdoor_temp_c",))
            consumption_summary = await session.execute(
                "WORKBENCH_SUMMARIZE", {"artifact_id": meter_id, "column": "kwh"}
            )
            assert consumption_summary["ok"], consumption_summary
            assert abs(consumption_summary["result"]["data"]["sum"] - 4.2) < 1e-9
            assert [row["timestamp"] for row in consumption_rows] == [
                row["timestamp"] for row in weather_rows
            ]
            outdoor_temp_c = weather_rows[-1]["outdoor_temp_c"]
            heat_loss = await execute_capability(
                session,
                "calculate_heat_loss",
                {
                    "indoor_temp_c": 20.0,
                    "outdoor_temp_c": outdoor_temp_c,
                    "components": components,
                    "duration_hours": 1.0,
                },
                asset_id=HEAT_PUMP_ASSET,
                input_artifact_ids=[weather_id],
                unit="kW_th and kWh_th",
            )

            independent_ua_w_per_k = sum(
                item["area_m2"] * item["u_value_w_m2k"] for item in components
            )
            independent_heat_loss_kw = independent_ua_w_per_k * (20.0 - 2.0) / 1000.0
            data = heat_loss["data"]
            assert heat_loss["kind"] == "calculated"
            assert weather.kind.value == "forecast"
            assert abs(sum(row["kwh"] for row in consumption_rows) - 4.2) < 1e-9
            assert abs(independent_heat_loss_kw - 1.638) < 1e-9
            assert abs(data["gross_heat_loss_kw"] - independent_heat_loss_kw) < 1e-9
            assert rated_output_kw >= data["design_capacity_kw"]
            assert any(item.get("artifact_id") == weather_id for item in heat_loss["provenance"])

            print(
                json.dumps(
                    {
                        "project": "building-consumption-weather-equipment",
                        "site": {"id": SITE_ID, "timezone": TIMEZONE},
                        "assets": [METER_ASSET, HEAT_PUMP_ASSET],
                        "sources": [
                            source_summary(meter_id, meter),
                            source_summary(weather_id, weather),
                        ],
                        "truths": {
                            "metered_energy_kwh": 4.2,
                            "workbench_sum_kwh": consumption_summary["result"]["data"]["sum"],
                            "design_outdoor_temp_c": outdoor_temp_c,
                            "independent_ua_w_per_k": independent_ua_w_per_k,
                            "independent_heat_loss_kw": independent_heat_loss_kw,
                        },
                        "heat_pump": {
                            "kind": heat_loss["kind"],
                            "rated_output_kw": rated_output_kw,
                            "declared_cop_at_design": declared_cop,
                            "design_capacity_kw": data["design_capacity_kw"],
                            "capacity_sufficient": rated_output_kw >= data["design_capacity_kw"],
                            "asset_id": heat_loss["asset_id"],
                            "provenance": heat_loss["provenance"],
                            "assumptions": heat_loss["assumptions"],
                        },
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
