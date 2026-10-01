"""Bounded executable workflows resolve energy providers at execution time."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .capabilities import CapabilityRequest
from .models import DataKind, EnergyError, Json, Session
from .time import day_window

if TYPE_CHECKING:
    from .runtime import EnergyAgent

RECIPES: dict[str, Json] = {
    "yesterday-consumption": {"capabilities": ["get_energy_consumption"], "operation": "summary"},
    "building-spike": {"capabilities": ["get_energy_consumption"], "operation": "anomaly"},
    "electricity-cost": {
        "capabilities": ["get_energy_consumption", "get_tariff"],
        "operation": "cost",
    },
    "cheapest-battery": {
        "capabilities": ["get_tariff", "get_carbon_intensity"],
        "operation": "battery",
        "objective": "cost",
    },
    "cleanest-battery": {
        "capabilities": ["get_tariff", "get_carbon_intensity"],
        "operation": "battery",
        "objective": "carbon",
    },
    "solar-consumption": {
        "capabilities": ["get_energy_consumption", "get_generation"],
        "operation": "align",
    },
    "solar-forecast": {
        "capabilities": ["get_weather", "estimate_solar_generation"],
        "operation": "solar",
    },
    "grid-conditions": {
        "capabilities": ["get_grid_generation", "get_carbon_intensity"],
        "operation": "evidence",
    },
    "power-flow": {"capabilities": ["run_power_flow"], "operation": "evidence"},
    "building-comparison": {"capabilities": ["get_energy_consumption"], "operation": "compare"},
    "tariff-comparison": {
        "capabilities": ["get_energy_consumption", "get_tariff"],
        "operation": "cost",
        "alternative": True,
    },
    "energy-baseline": {"capabilities": ["get_energy_consumption"], "operation": "baseline"},
}


def _time_column(result: Json) -> str:
    rows = result.get("data")
    if not isinstance(rows, list) or not rows:
        raise EnergyError("insufficient_data", "Workflow requires nonempty interval rows.")
    for column in ("timestamp", "from", "time", "interval_start"):
        if column in rows[0]:
            return column
    raise EnergyError("timestamp_required", "Binding must expose an explicit timestamp column.")


async def run_skill(agent: EnergyAgent, session: Session, skill_id: str, parameters: Json) -> Json:
    evidence: list[Json] = []
    output: Json | None = None
    try:
        agent._scope(session)
        if skill_id not in RECIPES:
            raise EnergyError("skill_not_found", "Search skills for a supported workflow.")
        allowed = {
            "start",
            "end",
            "arguments",
            "asset_id",
            "account_ids",
            "tools",
            "artifacts",
            "column",
            "frequency",
            "window",
            "battery",
            "solar",
            "alternative_tariff",
            "comparison_artifact",
        }
        if set(parameters) - allowed:
            raise EnergyError("invalid_skill_parameters", "Unknown workflow parameters.")
        recipe = RECIPES[skill_id]
        operation = recipe["operation"]
        arguments = parameters.get("arguments", {})
        window: Json = {}
        if "start" in parameters or "end" in parameters:
            if not {"start", "end"} <= parameters.keys():
                raise EnergyError("time_window_required", "Supply both start and end.")
            window = {"start": parameters["start"], "end": parameters["end"]}
        elif skill_id == "yesterday-consumption":
            if not session.site_id:
                raise EnergyError("site_required", "Choose a site to determine yesterday.")
            site = agent.sites[session.site_id]
            date = agent.calendar_clock().astimezone(ZoneInfo(site.timezone)).date() - timedelta(
                days=1
            )
            start, end = day_window(date, site.timezone)
            window = {"start": start.isoformat(), "end": end.isoformat()}
        artifacts: dict[str, str] = dict(parameters.get("artifacts", {}))
        results: dict[str, Json] = {}
        if skill_id == "solar-forecast":
            direct_request = CapabilityRequest(
                capability="get_solar_forecast",
                arguments={**window, **arguments.get("get_solar_forecast", {})},
                kind=DataKind.FORECAST,
                unit="kWh",
                account_id=parameters.get("account_ids", {}).get("get_solar_forecast"),
                tool=parameters.get("tools", {}).get("get_solar_forecast"),
                asset_id=parameters.get("asset_id"),
            )
            direct = agent.resolver.resolve(session, direct_request)
            if direct["status"] == "ambiguous":
                return {
                    "ok": False,
                    "skill_id": skill_id,
                    "blocked": "get_solar_forecast",
                    "resolution": direct,
                    "evidence": [],
                }
            if direct["selected"] is not None:
                recipe = {"capabilities": ["get_solar_forecast"]}
                operation = "summary"
        for capability in recipe["capabilities"]:
            if capability in artifacts:
                result = agent.workbench.read(session, artifacts[capability])
                results[capability] = result.model_dump(mode="json")
                continue
            if operation == "solar" and capability == "estimate_solar_generation":
                continue
            args = dict(arguments.get(capability, {}))
            if capability in {
                "get_energy_consumption",
                "get_tariff",
                "get_weather",
                "get_generation",
                "get_grid_generation",
                "get_solar_forecast",
                "get_carbon_intensity",
            }:
                args = {**window, **args}
            request = CapabilityRequest(
                capability=capability,
                arguments=args,
                asset_id=parameters.get("asset_id")
                if capability in {"get_energy_consumption", "get_solar_forecast"}
                else None,
                account_id=parameters.get("account_ids", {}).get(capability),
                tool=parameters.get("tools", {}).get(capability),
                kind=DataKind.METERED
                if capability == "get_energy_consumption"
                else DataKind.FORECAST
                if capability == "get_solar_forecast"
                else None,
                unit="kWh" if capability == "get_solar_forecast" else None,
            )
            output = await agent.resolver.execute(session, request, persist=True)
            evidence.append({"capability": capability, **output})
            if not output.get("ok"):
                return {
                    "ok": False,
                    "skill_id": skill_id,
                    "blocked": capability,
                    "evidence": evidence,
                }
            ref = output["result"]["data"]["artifact_id"]
            artifacts[capability] = ref
            results[capability] = agent.workbench.read(session, ref).model_dump(mode="json")
        column = parameters.get("column", "value")
        source = artifacts.get("get_energy_consumption") or artifacts.get("get_solar_forecast")
        if operation in {"summary", "anomaly"}:
            output = await agent.execute(
                session,
                "WORKBENCH_SUMMARIZE" if operation == "summary" else "WORKBENCH_ANOMALY",
                {"artifact_id": source, "column": column},
            )
        elif operation in {"cost", "align", "compare", "baseline"}:
            keys = recipe["capabilities"]
            inputs = [artifacts[k] for k in keys]
            params: Json = {"timestamp": _time_column(results[keys[0]]), "column": column}
            if len(keys) > 1:
                params["second_timestamp"] = _time_column(results[keys[1]])
            if "frequency" in parameters:
                params["frequency"] = parameters["frequency"]
            if "window" in parameters:
                params["window"] = parameters["window"]
            if skill_id == "building-comparison" and parameters.get("comparison_artifact"):
                other_id = parameters["comparison_artifact"]
                other = agent.workbench.read(session, other_id)
                if other.kind != DataKind.METERED or other.unit != results[keys[0]]["unit"]:
                    raise EnergyError(
                        "unit_incompatible",
                        "Building comparisons require matching metered interval energy.",
                    )
                inputs.append(other_id)
                params["second_timestamp"] = _time_column(other.model_dump(mode="json"))
                operation = "align"
            output = await agent.execute(
                session,
                "WORKBENCH_ENERGY_OPERATION",
                {"operation": operation, "artifact_ids": inputs, "parameters": params},
                input_artifacts=inputs,
            )
            if recipe.get("alternative"):
                alternative = parameters.get("alternative_tariff")
                if not isinstance(alternative, str):
                    raise EnergyError(
                        "alternative_required",
                        "Provide an alternative tariff artifact for the same intervals.",
                    )
                alt_inputs = [inputs[0], alternative]
                alt = agent.workbench.read(session, alternative)
                params["second_timestamp"] = _time_column(alt.model_dump(mode="json"))
                alternative_output = await agent.execute(
                    session,
                    "WORKBENCH_ENERGY_OPERATION",
                    {"operation": "cost", "artifact_ids": alt_inputs, "parameters": params},
                    input_artifacts=alt_inputs,
                )
                evidence.append({"alternative_tariff": alternative_output})
        elif operation == "battery":
            import pandas as pd

            tariff, carbon = results["get_tariff"], results["get_carbon_intensity"]
            if tariff["unit"] not in {"p/kWh", "GBP/kWh"} or carbon["unit"] not in {
                "gCO2/kWh",
                "gCO2e/kWh",
            }:
                raise EnergyError(
                    "unit_incompatible", "Charging requires reviewed price and carbon units."
                )
            prices = pd.DataFrame(tariff["data"])
            emissions = pd.DataFrame(carbon["data"])
            pcol, ccol = _time_column(tariff), _time_column(carbon)
            prices["instant"] = pd.to_datetime(prices[pcol], utc=True)
            emissions["instant"] = pd.to_datetime(emissions[ccol], utc=True)
            if prices["instant"].duplicated().any() or emissions["instant"].duplicated().any():
                raise EnergyError(
                    "duplicate_timestamp", "Charging inputs must have unique intervals."
                )
            merged = prices.merge(
                emissions, on="instant", suffixes=("_price", "_carbon"), validate="one_to_one"
            )
            if len(merged) != len(prices):
                raise EnergyError("incomplete_coverage", "Carbon must cover every tariff interval.")
            intervals = []
            for row in merged.to_dict("records"):
                interval_end = row.get("to_price", row.get("to"))
                if interval_end is None:
                    raise EnergyError(
                        "interval_end_required", "Tariff binding must expose interval end."
                    )
                duration = (pd.Timestamp(interval_end) - row["instant"]).total_seconds() / 3600
                cvalue = row.get("value_carbon", row.get("forecast", row.get("actual")))
                intervals.append(
                    {
                        "timestamp": row["instant"].isoformat(),
                        "duration_hours": duration,
                        "price_per_kwh": float(row["value_price"])
                        / (100 if tariff["unit"] == "p/kWh" else 1),
                        "carbon_intensity_g_per_kwh": cvalue,
                        "load_kw": 0,
                        "pv_kw": 0,
                    }
                )
            request = CapabilityRequest(
                capability="plan_battery_charging",
                arguments={
                    "intervals": intervals,
                    "battery": parameters.get("battery", {}),
                    "objective": recipe["objective"],
                },
            )
            selected = agent.resolver.resolve(session, request)["selected"]
            if not selected:
                raise EnergyError(
                    "battery_inputs_required",
                    "Supply battery capacity, state and charge/discharge limits.",
                )
            output = await agent.execute(
                session,
                selected["tool"],
                selected["arguments"],
                input_artifacts=list(artifacts.values()),
            )
        elif operation == "solar":
            weather = results["get_weather"]
            if weather["kind"] != "forecast":
                raise EnergyError("forecast_required", "Solar forecast requires forecast weather.")
            pivot = await agent.execute(
                session,
                "WORKBENCH_PIVOT",
                {
                    "artifact_id": artifacts["get_weather"],
                    "timestamp": "timestamp",
                    "variable": "variable",
                    "value": "value",
                },
                persist=True,
            )
            if not pivot["ok"]:
                return {"ok": False, "skill_id": skill_id, "evidence": [*evidence, pivot]}
            pivot_id = pivot["result"]["data"]["artifact_id"]
            wide = agent.workbench.read(session, pivot_id)
            units: dict[str, str | None] = {}
            for row in weather["data"]:
                variable = row.get("variable")
                unit = row.get("unit")
                if variable in units and units[variable] != unit:
                    raise EnergyError(
                        "unit_incompatible", "Weather variable units must be consistent."
                    )
                units[variable] = unit
            if units.get("shortwave_radiation") not in {"W/m²", "W/m2", "W/m^2"}:
                raise EnergyError("unit_incompatible", "Solar irradiance must be supplied in W/m2.")
            wind_unit = units.get("wind_speed_10m")
            if wind_unit not in {None, "m/s", "km/h"}:
                raise EnergyError("unit_incompatible", "Unsupported weather wind-speed unit.")
            if units.get("temperature_2m") not in {None, "°C", "C", "degC"}:
                raise EnergyError(
                    "unit_incompatible", "Solar weather temperature must use degrees Celsius."
                )
            rows = [
                {
                    "timestamp": r["timestamp"],
                    "ghi_w_m2": r.get("shortwave_radiation"),
                    "temp_air_c": r.get("temperature_2m", 20),
                    "wind_speed_m_s": r.get("wind_speed_10m", 0)
                    / (3.6 if wind_unit == "km/h" else 1),
                }
                for r in wide.data
            ]
            solar = dict(parameters.get("solar", {}))
            solar.update(
                weather_rows=rows, weather_kind="forecast", weather_source=weather["source"]
            )
            if session.site_id:
                site = agent.sites[session.site_id]
                for key in ("latitude", "longitude", "timezone"):
                    solar.setdefault(key, getattr(site, key))
            request = CapabilityRequest(capability="estimate_solar_generation", arguments=solar)
            selected = agent.resolver.resolve(session, request)["selected"]
            if not selected:
                raise EnergyError(
                    "solar_inputs_required", "Supply PV capacity and forecast irradiance in W/m2."
                )
            output = await agent.execute(
                session,
                selected["tool"],
                selected["arguments"],
                account_id=selected["account_id"],
                expected_kind=selected["kind"],
                expected_unit=selected["unit"],
                expected_resolution=selected["resolution"],
                expected_arguments=selected["fixed_arguments"],
                asset_id=selected["asset_id"],
                input_artifacts=[artifacts["get_weather"], pivot_id],
            )
        if output:
            evidence.append({"analysis": output})
        return {
            "ok": all(item.get("analysis", {}).get("ok", True) for item in evidence),
            "skill_id": skill_id,
            "evidence": evidence,
            "pitfalls": [
                "Correlation does not establish a cause.",
                "Plans and model outputs cannot dispatch equipment.",
                "Source kinds and units remain in evidence; forecasts are not measurements.",
            ],
        }
    except (EnergyError, ValueError, TypeError, KeyError) as exc:
        return {
            "ok": False,
            "skill_id": skill_id,
            "error": {
                "code": exc.code if isinstance(exc, EnergyError) else "invalid_skill_parameters",
                "message": exc.message
                if isinstance(exc, EnergyError)
                else "Workflow inputs do not satisfy the reviewed contract.",
            },
            "evidence": evidence,
        }
