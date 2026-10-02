"""Subprocess acceptance for the six offline SDK reference workflows."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "examples" / "reference_projects" / "model_workflows.py"
SITE_ID = "six-workflow-reference-site"


def _recipe(report: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    return next(item for item in report["recipes"] if item["id"] == recipe_id)


def _model_result(recipe: dict[str, Any]) -> dict[str, Any]:
    for item in reversed(recipe["success"]["evidence"]):
        analysis = item.get("analysis")
        if analysis and analysis.get("ok"):
            return analysis["result"]
    raise AssertionError(f"{recipe['id']} has no successful analysis result")


def _battery_truth(result: dict[str, Any], *, expected_charge_start: str) -> None:
    assert result["kind"] == "simulated"
    assert result["source"] == "energy-agent-tools:battery-optimizer"
    assert result["asset_id"] == "reference-home-battery"
    assert result["unit"].endswith("gCO2")
    assert result["data"]["summary"]["carbon_species"] == "CO2"
    schedule = result["data"]["schedule"]
    assert len(schedule) == 3
    charges = [row for row in schedule if row["charge_kw"] > 1e-6]
    assert len(charges) == 1
    assert charges[0]["timestamp"] == expected_charge_start
    soc = 0.0
    for row in schedule:
        assert 0.0 <= row["soc_kwh"] <= 2.0
        assert 0.0 <= row["charge_kw"] <= 1.0 + 1e-8
        assert 0.0 <= row["discharge_kw"] <= 1.0 + 1e-8
        soc += row["charge_kw"] * 0.9 * row["duration_hours"]
        soc -= row["discharge_kw"] / 0.9 * row["duration_hours"]
        assert math.isclose(soc, row["soc_kwh"], abs_tol=2e-5)
    assert math.isclose(soc, 0.9, abs_tol=2e-5)


def test_six_offline_model_workflows_have_source_evidence_and_real_failures() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            environment.get("PYTHONPATH", ""),
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
        )
        if part
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    report = json.loads(completed.stdout)
    expected_ids = {
        "cheapest-battery",
        "cleanest-battery",
        "solar-consumption",
        "solar-forecast",
        "grid-conditions",
        "power-flow",
    }
    assert {recipe["id"] for recipe in report["recipes"]} == expected_ids
    assert len(report["recipes"]) == 6
    assert report["physical_meter"] is False

    for recipe in report["recipes"]:
        assert recipe["success"]["ok"] is True
        assert recipe["failure"]["ok"] is False
        assert recipe["failure_code"]
        assert recipe["sources"], recipe["id"]
        for source in recipe["sources"]:
            assert source["artifact_id"]
            assert source["source"] == "local-csv"
            assert source["site_id"] == SITE_ID
            assert source["asset_id"]
            assert source["kind"]
            assert source["unit"]
            assert "quantity_shape" in source
            assert source["physical_meter"] is False
            assert source["synthetic_fixture"] is True
            assert all(row["physical_meter"].lower() == "false" for row in source["rows_data"])

    cheapest = _recipe(report, "cheapest-battery")
    cleanest = _recipe(report, "cleanest-battery")
    _battery_truth(_model_result(cheapest), expected_charge_start="2026-06-21T11:00:00+01:00")
    _battery_truth(_model_result(cleanest), expected_charge_start="2026-06-21T13:00:00+01:00")
    expected_failure_codes = {
        "cheapest-battery": "incomplete_coverage",
        "cleanest-battery": "battery_infeasible",
        "solar-consumption": "column_not_found",
        "solar-forecast": "unit_incompatible",
        "grid-conditions": "file_forbidden",
        "power-flow": "invalid_network",
    }
    assert {
        recipe["id"]: recipe["failure_code"] for recipe in report["recipes"]
    } == expected_failure_codes

    solar_consumption = _recipe(report, "solar-consumption")
    aligned = _model_result(solar_consumption)
    balance = aligned["data"]
    rows = balance["intervals"]
    assert len(rows) == 3
    assert [row["load_kwh"] for row in rows] == [0.5, 0.75, 0.4]
    assert [row["generation_kwh"] for row in rows] == [0.1, 0.35, 0.5]
    expected = {
        "load_kwh": 1.65,
        "generation_kwh": 0.95,
        "self_consumption_kwh": 0.85,
        "estimated_import_kwh": 0.8,
        "estimated_export_kwh": 0.1,
    }
    for field, value in expected.items():
        assert math.isclose(balance["summary"][field], value)
    assert any("not grid meter readings" in item for item in solar_consumption["limitations"])

    solar_forecast = _recipe(report, "solar-forecast")
    pv_result = _model_result(solar_forecast)
    assert pv_result["kind"] == "estimated"
    assert pv_result["source"] == "pvlib"
    intervals = pv_result["data"]["intervals"]
    total_energy = sum(row["energy_kwh"] for row in intervals)
    assert len(intervals) == 4
    assert math.isclose(total_energy, pv_result["data"]["total_energy_kwh"], abs_tol=1e-8)
    assert 0 < total_energy < 8.0
    assert all(0 <= row["ac_power_kw"] <= 2.0 for row in intervals)

    grid = _recipe(report, "grid-conditions")
    assert {source["capability"] for source in grid["sources"]} == {
        "get_grid_generation",
        "get_carbon_intensity",
    }
    generation = next(
        source for source in grid["sources"] if source["capability"] == "get_grid_generation"
    )
    carbon = next(
        source for source in grid["sources"] if source["capability"] == "get_carbon_intensity"
    )
    assert [float(row["value"]) for row in generation["rows_data"]] == [31000, 32500, 31800]
    assert [float(row["value"]) for row in carbon["rows_data"]] == [250, 100, 40]
    # The workflow labels its final fetched source as analysis evidence; it is
    # still that carbon source artifact and does not combine grid values.
    labeled_output = next(
        item["analysis"] for item in grid["success"]["evidence"] if "analysis" in item
    )
    assert labeled_output["result"]["data"]["artifact_id"] == carbon["artifact_id"]
    assert labeled_output["result"]["unit"] == "gCO2/kWh"
    assert grid["failure_code"] == "file_forbidden"
    assert any("produces no combined grid metric" in item for item in grid["limitations"])

    power_flow = _recipe(report, "power-flow")
    flow = power_flow["model_result"]
    assert flow["kind"] == "simulated"
    assert flow["source"] == "pandapower"
    assert flow["site_id"] == SITE_ID
    assert flow["asset_id"] == "reference-two-bus-network"
    assert flow["data"]["converged"] is True
    totals = flow["data"]["totals"]
    assert totals["line_loss_mw"] > 0
    assert abs(totals["balance_error_mw"]) < 1e-6
    load_bus = next(bus for bus in flow["data"]["buses"] if bus["id"] == "load")
    expected_drop_pu = (0.5 * 0.8 + 0.4 * 0.2) / 11**2
    assert math.isclose(load_bus["vm_pu"], 1 - expected_drop_pu, abs_tol=2e-4)
    expected_loss_mw = 0.5 * (0.8**2 + 0.2**2) / 11**2
    assert math.isclose(totals["line_loss_mw"], expected_loss_mw, abs_tol=5e-5)
    assert math.isclose(flow["data"]["loads"][0]["p_mw"], 0.8, abs_tol=1e-12)
    assert math.isclose(flow["data"]["loads"][0]["q_mvar"], 0.2, abs_tol=1e-12)
    feeder_source = power_flow["sources"][0]
    assert power_flow["model_input"]["source_artifact_id"] == feeder_source["artifact_id"]
    assert power_flow["model_input"]["site_id"] == feeder_source["site_id"]
    assert power_flow["model_input"]["asset_id"] == feeder_source["asset_id"]
    assert power_flow["model_input"]["load_values"] == {"p_mw": 0.8, "q_mvar": 0.2}
    assert any(
        item.get("artifact_id") == feeder_source["artifact_id"]
        and item.get("input_kind") == "metered"
        and item.get("source") == "local-csv"
        and item.get("unit") == "MW and Mvar"
        for item in flow["provenance"]
    )
