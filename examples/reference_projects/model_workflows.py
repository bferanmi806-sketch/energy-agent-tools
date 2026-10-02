"""Offline, runnable references for six Energy Agent Tools workflows.

The project uses reviewed CSV bindings and session-scoped artifacts so each
workflow exercises the SDK and its normal provenance path without network I/O.
All fixtures are synthetic, including rows declared as metered.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.models import DataKind
from examples.reference_projects._shared import USER_ID, source_summary

SITE_ID = "six-workflow-reference-site"
TIMEZONE = "Europe/London"
START = "2026-06-21T11:00:00+01:00"
END = "2026-06-21T14:00:00+01:00"

METER_ASSET = "reference-home-meter"
TARIFF_ASSET = "reference-tariff-source"
CARBON_ASSET = "reference-carbon-source"
WEATHER_ASSET = "reference-weather-source"
GENERATION_ASSET = "reference-solar-generation-source"
GRID_ASSET = "reference-grid-generation-source"
BATTERY_ASSET = "reference-home-battery"
PV_ASSET = "reference-rooftop-pv"
NETWORK_ASSET = "reference-two-bus-network"

SOURCE_TOOLS = {
    "get_energy_consumption": "CSV_READ_TIMESERIES",
    "get_tariff": "CSV_READ_TIMESERIES",
    "get_carbon_intensity": "CSV_READ_TIMESERIES",
    "get_generation": "CSV_READ_TIMESERIES",
    "get_grid_generation": "CSV_READ_TIMESERIES",
}

BATTERY = {
    "capacity_kwh": 2.0,
    "initial_soc_kwh": 0.0,
    "min_soc_kwh": 0.0,
    "target_final_soc_kwh": 0.9,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
}


def _write_csv(
    root: Path, filename: str, fieldnames: list[str], rows: list[dict[str, Any]]
) -> None:
    with (root / filename).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*fieldnames, "physical_meter"])
        writer.writeheader()
        writer.writerows([{**row, "physical_meter": False} for row in rows])


def _hourly_rows(values: list[float]) -> list[dict[str, Any]]:
    starts = [
        "2026-06-21T11:00:00+01:00",
        "2026-06-21T12:00:00+01:00",
        "2026-06-21T13:00:00+01:00",
    ]
    ends = [
        "2026-06-21T12:00:00+01:00",
        "2026-06-21T13:00:00+01:00",
        "2026-06-21T14:00:00+01:00",
    ]
    return [
        {"timestamp": start, "end": end, "value": value}
        for start, end, value in zip(starts, ends, values, strict=True)
    ]


def _weather_rows(*, temperature_unit: str = "°C") -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for timestamp, irradiance, temperature in (
        ("2026-06-21T10:00:00+01:00", 100, 19),
        ("2026-06-21T11:00:00+01:00", 450, 20),
        ("2026-06-21T12:00:00+01:00", 700, 21),
        ("2026-06-21T13:00:00+01:00", 550, 22),
    ):
        for variable, value, unit in (
            ("shortwave_radiation", irradiance, "W/m²"),
            ("temperature_2m", temperature, temperature_unit),
            ("wind_speed_10m", 7.2, "km/h"),
        ):
            rows.append(
                {"timestamp": timestamp, "variable": variable, "value": value, "unit": unit}
            )
    return rows


def _fixtures(data_root: Path) -> None:
    _write_csv(
        data_root,
        "meter.csv",
        ["timestamp", "end", "value"],
        _hourly_rows([0.5, 0.75, 0.4]),
    )
    _write_csv(
        data_root, "tariff.csv", ["timestamp", "end", "value"], _hourly_rows([0.05, 0.2, 0.3])
    )
    _write_csv(
        data_root,
        "carbon.csv",
        ["timestamp", "end", "value"],
        _hourly_rows([250, 100, 40]),
    )
    _write_csv(
        data_root,
        "generation.csv",
        ["timestamp", "end", "value"],
        _hourly_rows([0.1, 0.35, 0.5]),
    )
    _write_csv(
        data_root,
        "grid_generation.csv",
        ["timestamp", "value"],
        [
            {"timestamp": "2026-06-21T11:00:00+01:00", "value": 31000},
            {"timestamp": "2026-06-21T12:00:00+01:00", "value": 32500},
            {"timestamp": "2026-06-21T13:00:00+01:00", "value": 31800},
        ],
    )
    _write_csv(
        data_root, "weather.csv", ["timestamp", "variable", "value", "unit"], _weather_rows()
    )
    _write_csv(
        data_root,
        "weather_bad_temperature.csv",
        ["timestamp", "variable", "value", "unit"],
        _weather_rows(temperature_unit="K"),
    )
    _write_csv(
        data_root,
        "feeder_load.csv",
        ["timestamp", "p_mw", "q_mvar"],
        [{"timestamp": "2026-06-21T11:00:00+01:00", "p_mw": 0.8, "q_mvar": 0.2}],
    )


def _csv_binding(
    capability: str,
    asset_id: str,
    filename: str,
    *,
    kind: str,
    unit: str,
    quantity_shape: str | None,
    resolution: str | None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "file": filename,
        "kind": kind,
        "unit": unit,
        "timezone": TIMEZONE,
    }
    if quantity_shape is not None:
        arguments["quantity_shape"] = quantity_shape
    if resolution is not None:
        arguments["resolution"] = resolution
    return {
        "capability": capability,
        "tool": "CSV_READ_TIMESERIES",
        "asset_id": asset_id,
        "kind": kind,
        "unit": unit,
        "quantity_shape": quantity_shape,
        "resolution": resolution,
        "quality": "reviewed-synthetic-fixture",
        "reviewed": True,
        "fixed_arguments": arguments,
    }


def _configuration() -> dict[str, Any]:
    assets = [
        (METER_ASSET, "electricity-meter", "Synthetic interval energy meter"),
        (TARIFF_ASSET, "tariff-source", "Synthetic price forecast"),
        (CARBON_ASSET, "carbon-source", "Synthetic carbon forecast"),
        (WEATHER_ASSET, "weather-source", "Synthetic weather forecast"),
        (GENERATION_ASSET, "solar-generation-source", "Synthetic solar generation forecast"),
        (GRID_ASSET, "grid-generation-source", "Synthetic grid generation series"),
        (BATTERY_ASSET, "home-battery", "Two kWh battery model"),
        (PV_ASSET, "solar-array", "Two kW rooftop PV model"),
        (NETWORK_ASSET, "distribution-feeder-model", "11 kV two-bus feeder"),
    ]
    bindings = [
        _csv_binding(
            "get_energy_consumption",
            METER_ASSET,
            "meter.csv",
            kind="metered",
            unit="kWh",
            quantity_shape="interval",
            resolution="1h",
        ),
        _csv_binding(
            "get_tariff",
            TARIFF_ASSET,
            "tariff.csv",
            kind="forecast",
            unit="GBP/kWh",
            quantity_shape="interval",
            resolution="1h",
        ),
        _csv_binding(
            "get_carbon_intensity",
            CARBON_ASSET,
            "carbon.csv",
            kind="forecast",
            unit="gCO2/kWh",
            quantity_shape="interval",
            resolution="1h",
        ),
        _csv_binding(
            "get_generation",
            GENERATION_ASSET,
            "generation.csv",
            kind="forecast",
            unit="kWh",
            quantity_shape="interval",
            resolution="1h",
        ),
        _csv_binding(
            "get_grid_generation",
            GRID_ASSET,
            "grid_generation.csv",
            kind="metered",
            unit="MW",
            quantity_shape="instantaneous",
            resolution="1h",
        ),
        {
            "capability": "plan_battery_charging",
            "tool": "engineering.schedule_battery_charging",
            "asset_id": BATTERY_ASSET,
            "kind": "simulated",
            "unit": "kW, kWh, currency, gCO2",
            "quality": "verified",
            "reviewed": True,
        },
        {
            "capability": "estimate_solar_generation",
            "tool": "engineering.estimate_solar_generation",
            "asset_id": PV_ASSET,
            "kind": "estimated",
            "unit": "kW_ac and kWh",
            "quality": "verified",
            "reviewed": True,
        },
        {
            "capability": "run_power_flow",
            "tool": "engineering.run_power_flow",
            "asset_id": NETWORK_ASSET,
            "kind": "simulated",
            "unit": "MW, Mvar, pu",
            "quality": "verified",
            "reviewed": True,
        },
    ]
    return {
        "sites": [
            {
                "id": SITE_ID,
                "user_id": USER_ID,
                "name": "Six workflow synthetic reference site",
                "timezone": TIMEZONE,
                "latitude": 51.45,
                "longitude": -2.59,
            }
        ],
        "assets": [
            {
                "id": asset_id,
                "site_id": SITE_ID,
                "kind": kind,
                "name": name,
                "metadata": {"synthetic_fixture": True, "physical_meter": False},
            }
            for asset_id, kind, name in assets
        ],
        "bindings": bindings,
    }


def _source_record(session: Any, capability: str, evidence: dict[str, Any]) -> dict[str, Any]:
    result = evidence["result"]
    artifact_id = result["data"]["artifact_id"]
    artifact = session.agent.workbench.read(session.context, artifact_id)
    return {
        "capability": capability,
        **source_summary(artifact_id, artifact),
        "site_id": artifact.site_id,
        "provider": artifact.provider,
        "original_unit": artifact.original_unit,
        "field_units": artifact.field_units,
        "physical_meter": False,
        "synthetic_fixture": True,
        "rows_data": artifact.data,
    }


def _sources(session: Any, output: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _source_record(session, item["capability"], item)
        for item in output.get("evidence", [])
        if item.get("capability") in SOURCE_TOOLS and item.get("ok") and item.get("result")
    ]


def _analysis(output: dict[str, Any]) -> dict[str, Any] | None:
    evidence = output.get("evidence", [])
    for item in reversed(evidence):
        if "analysis" in item:
            return item["analysis"]
    return None


def _failure_code(output: dict[str, Any]) -> str | None:
    if output.get("ok"):
        return None
    if output.get("error"):
        return output["error"]["code"]
    for item in reversed(output.get("evidence", [])):
        analysis = item.get("analysis")
        if isinstance(analysis, dict) and analysis.get("error"):
            return analysis["error"]["code"]
    for item in output.get("evidence", []):
        if item.get("ok") is False and item.get("error"):
            return item["error"]["code"]
    return None


async def _import_meter_for_power_flow(session: Any) -> tuple[str, Any]:
    response = await session.execute(
        "CSV_READ_TIMESERIES",
        {
            "file": "feeder_load.csv",
            "kind": "metered",
            "unit": "MW and Mvar",
            "timezone": TIMEZONE,
            "quantity_shape": "instantaneous",
        },
        persist=True,
        asset_id=METER_ASSET,
    )
    assert response["ok"], response
    artifact_id = response["result"]["data"]["artifact_id"]
    artifact = session.agent.workbench.read(session.context, artifact_id)
    assert artifact.kind == DataKind.METERED
    assert artifact.unit == "MW and Mvar"
    assert artifact.quantity_shape == "instantaneous"
    assert artifact.site_id == SITE_ID and artifact.asset_id == METER_ASSET
    return artifact_id, artifact


async def _import_weather(session: Any, filename: str) -> tuple[str, Any]:
    response = await session.execute(
        "CSV_READ_TIMESERIES",
        {
            "file": filename,
            "kind": "forecast",
            "unit": "mixed",
            "timezone": TIMEZONE,
            "resolution": "1h",
        },
        persist=True,
        asset_id=WEATHER_ASSET,
    )
    assert response["ok"], response
    artifact_id = response["result"]["data"]["artifact_id"]
    artifact = session.agent.workbench.read(session.context, artifact_id)
    assert artifact.kind == DataKind.FORECAST
    assert artifact.unit == "mixed"
    assert artifact.site_id == SITE_ID and artifact.asset_id == WEATHER_ASSET
    assert artifact.source == "local-csv"
    return artifact_id, artifact


def _explicit_source_record(artifact_id: str, artifact: Any, *, capability: str) -> dict[str, Any]:
    return {
        "capability": capability,
        **source_summary(artifact_id, artifact),
        "site_id": artifact.site_id,
        "provider": artifact.provider,
        "original_unit": artifact.original_unit,
        "field_units": artifact.field_units,
        "physical_meter": False,
        "synthetic_fixture": True,
        "rows_data": artifact.data,
    }


async def run_report() -> dict[str, Any]:
    with TemporaryDirectory(prefix="energy-six-workflows-") as temporary:
        root = Path(temporary)
        data_root = root / "data"
        data_root.mkdir()
        _fixtures(data_root)
        async with EnergyAgentTools(
            root / "state", config=_configuration(), data_root=data_root
        ) as energy:
            session = energy.session(USER_ID, SITE_ID)
            common = {
                "start": START,
                "end": END,
                "tools": SOURCE_TOOLS,
            }

            cheapest = await session.skill("cheapest-battery", {**common, "battery": dict(BATTERY)})
            assert cheapest["ok"], cheapest
            cheapest_result = _analysis(cheapest)["result"]
            assert cheapest_result["kind"] == "simulated"
            cheapest_failure = await session.skill(
                "cheapest-battery",
                {
                    **common,
                    "end": "2026-06-21T15:00:00+01:00",
                    "battery": dict(BATTERY),
                },
            )
            assert not cheapest_failure["ok"], cheapest_failure

            cleanest = await session.skill("cleanest-battery", {**common, "battery": dict(BATTERY)})
            assert cleanest["ok"], cleanest
            cleanest_result = _analysis(cleanest)["result"]
            assert cleanest_result["kind"] == "simulated"
            infeasible_battery = {
                **BATTERY,
                "capacity_kwh": 1.0,
                "target_final_soc_kwh": 1.0,
                "max_charge_kw": 0.1,
            }
            cleanest_failure = await session.skill(
                "cleanest-battery", {**common, "battery": infeasible_battery}
            )
            assert not cleanest_failure["ok"], cleanest_failure

            solar_consumption = await session.skill(
                "solar-consumption",
                {
                    **common,
                    "tools": SOURCE_TOOLS,
                    "solar_balance": {"consumption_basis": "total_load", "storage_mode": "none"},
                },
            )
            assert solar_consumption["ok"], solar_consumption
            solar_consumption_failure = await session.skill(
                "solar-consumption", {**common, "column": "missing_energy_column"}
            )
            assert not solar_consumption_failure["ok"], solar_consumption_failure

            weather_id, weather = await _import_weather(session, "weather.csv")
            solar_forecast = await session.skill(
                "solar-forecast",
                {
                    "artifacts": {"get_weather": weather_id},
                    "solar": {
                        "dc_capacity_kw": 2.0,
                        "inverter_capacity_kw": 2.0,
                        "surface_tilt_deg": 30.0,
                        "surface_azimuth_deg": 180.0,
                    },
                },
            )
            assert solar_forecast["ok"], solar_forecast
            bad_weather_id, bad_weather = await _import_weather(
                session, "weather_bad_temperature.csv"
            )
            solar_forecast_failure = await session.skill(
                "solar-forecast",
                {
                    "artifacts": {"get_weather": bad_weather_id},
                    "solar": {
                        "dc_capacity_kw": 2.0,
                        "inverter_capacity_kw": 2.0,
                    },
                },
            )
            assert not solar_forecast_failure["ok"], solar_forecast_failure

            grid_conditions = await session.skill(
                "grid-conditions", {**common, "tools": SOURCE_TOOLS}
            )
            assert grid_conditions["ok"], grid_conditions
            (data_root / "grid_generation.csv").unlink()
            grid_failure = await session.skill("grid-conditions", {**common, "tools": SOURCE_TOOLS})
            assert not grid_failure["ok"], grid_failure

            meter_id, feeder_meter = await _import_meter_for_power_flow(session)
            meter_row = feeder_meter.data[0]
            network = {
                "network": {
                    "sn_mva": 5.0,
                    "f_hz": 50.0,
                    "buses": [
                        {"id": "grid", "name": "Grid supply", "vn_kv": 11.0},
                        {"id": "load", "name": "Measured feeder load", "vn_kv": 11.0},
                    ],
                    "lines": [
                        {
                            "id": "feeder-1",
                            "from_bus": "grid",
                            "to_bus": "load",
                            "length_km": 1.0,
                            "r_ohm_per_km": 0.5,
                            "x_ohm_per_km": 0.4,
                            "max_i_ka": 0.4,
                        }
                    ],
                    "loads": [
                        {
                            "id": "measured-load",
                            "bus": "load",
                            "p_mw": float(meter_row["p_mw"]),
                            "q_mvar": float(meter_row["q_mvar"]),
                        }
                    ],
                    "ext_grid": [{"id": "upstream", "bus": "grid"}],
                }
            }
            power_flow = await session.skill(
                "power-flow",
                {
                    "arguments": {"run_power_flow": network},
                    "tools": {"run_power_flow": "engineering.run_power_flow"},
                    "input_artifacts": [meter_id],
                },
            )
            assert power_flow["ok"], power_flow
            power_flow_envelope = next(
                item["result"]
                for item in power_flow["evidence"]
                if item.get("capability") == "run_power_flow"
            )
            power_flow_result_id = power_flow_envelope["data"]["artifact_id"]
            power_flow_result = session.agent.workbench.read(
                session.context, power_flow_result_id
            ).model_dump(mode="json")
            invalid_network = {
                "network": {
                    "buses": [{"id": "island", "vn_kv": 11.0}],
                    "lines": [],
                    "loads": [],
                }
            }
            power_flow_failure = await session.skill(
                "power-flow",
                {
                    "arguments": {"run_power_flow": invalid_network},
                    "tools": {"run_power_flow": "engineering.run_power_flow"},
                    "input_artifacts": [meter_id],
                },
            )
            assert not power_flow_failure["ok"], power_flow_failure

            recipes = [
                {
                    "id": "cheapest-battery",
                    "sources": _sources(session, cheapest),
                    "success": cheapest,
                    "failure": cheapest_failure,
                    "failure_code": _failure_code(cheapest_failure),
                    "limitations": [
                        "Advisory schedule only; it does not dispatch the battery.",
                        "Degradation, standing charges, and demand charges are outside this model.",
                    ],
                },
                {
                    "id": "cleanest-battery",
                    "sources": _sources(session, cleanest),
                    "success": cleanest,
                    "failure": cleanest_failure,
                    "failure_code": _failure_code(cleanest_failure),
                    "limitations": [
                        "Advisory schedule only; it does not dispatch the battery.",
                        "Carbon intensity is a forecast and does not represent marginal emissions.",
                    ],
                },
                {
                    "id": "solar-consumption",
                    "sources": _sources(session, solar_consumption),
                    "success": solar_consumption,
                    "failure": solar_consumption_failure,
                    "failure_code": _failure_code(solar_consumption_failure),
                    "limitations": [
                        "Import/export are interval netting estimates for declared total load with no storage, not grid meter readings.",
                        "Aligned values do not establish the cause of consumption or generation.",
                    ],
                },
                {
                    "id": "solar-forecast",
                    "sources": [
                        _explicit_source_record(weather_id, weather, capability="get_weather")
                    ],
                    "success": solar_forecast,
                    "failure": solar_forecast_failure,
                    "failure_code": _failure_code(solar_forecast_failure),
                    "limitations": [
                        "PV output is estimated from forecast weather, not measured generation.",
                        "Shading, clipping, soiling, snow, and site obstructions are not represented.",
                    ],
                },
                {
                    "id": "grid-conditions",
                    "sources": _sources(session, grid_conditions),
                    "success": grid_conditions,
                    "failure": grid_failure,
                    "failure_code": _failure_code(grid_failure),
                    "limitations": [
                        "This workflow exposes the two source outputs as evidence only; it produces no combined grid metric.",
                        "Grid averages do not represent marginal emissions or a particular site's conditions.",
                    ],
                },
                {
                    "id": "power-flow",
                    "sources": [
                        {
                            **source_summary(meter_id, feeder_meter),
                            "site_id": feeder_meter.site_id,
                            "physical_meter": False,
                            "synthetic_fixture": True,
                            "used_as": "measured-feeder-load-input",
                            "rows_data": feeder_meter.data,
                        }
                    ],
                    "success": power_flow,
                    "failure": power_flow_failure,
                    "failure_code": _failure_code(power_flow_failure),
                    "model_result_artifact_id": power_flow_result_id,
                    "model_result": power_flow_result,
                    "model_input": {
                        "source_artifact_id": meter_id,
                        "site_id": feeder_meter.site_id,
                        "asset_id": feeder_meter.asset_id,
                        "load_values": {
                            "p_mw": float(meter_row["p_mw"]),
                            "q_mvar": float(meter_row["q_mvar"]),
                        },
                    },
                    "limitations": [
                        "Power flow is a simulated balanced steady-state snapshot, not protection or transient analysis.",
                        "The feeder load snapshot is linked to the simulated result through its session-scoped CSV artifact.",
                    ],
                },
            ]
            assert len(recipes) == 6
            assert all(item["failure_code"] is not None for item in recipes)
            return {
                "project": "energy-agent-tools-six-model-workflows",
                "mode": "offline-synthetic-fixtures",
                "site": {"id": SITE_ID, "timezone": TIMEZONE},
                "physical_meter": False,
                "recipes": recipes,
                "independent_truth": {
                    "battery_price_cheapest_start": START,
                    "battery_carbon_cleanest_start": "2026-06-21T13:00:00+01:00",
                    "metered_consumption_kwh": 1.65,
                    "solar_generation_kwh": [0.1, 0.35, 0.5],
                    "feeder_load_mw": 0.8,
                    "feeder_reactive_load_mvar": 0.2,
                },
            }


async def main() -> None:
    print(json.dumps(await run_report(), indent=2))


if __name__ == "__main__":
    os.environ.setdefault("MPL_IGNORE_SYSTEM_FONTS", "1")
    asyncio.run(main())
