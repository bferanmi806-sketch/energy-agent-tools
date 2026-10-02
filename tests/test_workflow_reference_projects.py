from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from energy_agent_tools.workflows import RECIPES

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RECIPES = {
    "yesterday-consumption",
    "building-spike",
    "electricity-cost",
    "energy-baseline",
    "building-comparison",
    "tariff-comparison",
}


def test_offline_workflow_reference_executes_and_checks_six_recipes() -> None:
    assert EXPECTED_RECIPES <= set(RECIPES)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), environment.get("PYTHONPATH", ""), str(ROOT / "src"))
    )
    process = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "reference_projects" / "workflows.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(process.stdout)

    assert set(report["recipes"]) == EXPECTED_RECIPES
    assert all(result["ok"] for result in report["recipes"].values())
    assert report["scenario_clock"] == "2026-09-30T12:00:00Z"
    assert report["scenario_day"] == "2026-09-29"
    assert report["site"] == {"id": "workflow-reference-site", "timezone": "UTC"}

    assert report["recipes"]["yesterday-consumption"]["sum_kwh"] == 28.0
    assert report["recipes"]["building-spike"] == {
        "ok": True,
        "anomaly_count": 1,
        "spike_kwh": 5.0,
    }
    base_bill = report["recipes"]["electricity-cost"]["bill_gbp"]
    assert Decimal(base_bill["energy_cost"]) == Decimal("5.20")
    assert Decimal(base_bill["standing_charge"]) == Decimal("0.30")
    assert Decimal(base_bill["tax"]) == Decimal("0.26")
    assert Decimal(base_bill["total"]) == Decimal("5.76")

    baseline = report["recipes"]["energy-baseline"]
    assert baseline["rows"] == 24
    assert baseline["baseline_at_hour_4_kwh"] == 1.0
    assert baseline["residual_at_spike_kwh"] == 4.0
    comparison = report["recipes"]["building-comparison"]["current_day"]
    assert comparison["value"] == 28.0
    assert comparison["previous"] == 12.0
    assert comparison["difference"] == 16.0
    assert comparison["percent_change"] == 16 / 12 * 100

    tariff_comparison = report["recipes"]["tariff-comparison"]
    primary_bill = tariff_comparison["primary_bill_gbp"]
    alternative_bill = tariff_comparison["alternative_bill_gbp"]
    assert Decimal(primary_bill["total"]) == Decimal("5.76")
    assert Decimal(alternative_bill["energy_cost"]) == Decimal("5.60")
    assert Decimal(alternative_bill["standing_charge"]) == Decimal("0.45")
    assert Decimal(alternative_bill["tax"]) == Decimal("0.56")
    assert Decimal(alternative_bill["total"]) == Decimal("6.61")
    assert primary_bill["standing_charge"] != alternative_bill["standing_charge"]
    assert primary_bill["tax"] != alternative_bill["tax"]

    for source in [*report["sources"], report["alternative_tariff_source"]]:
        assert source["source"] == "local-csv"
        assert source["physical_meter"] is False
        assert source["site_id"] == report["site"]["id"]
        assert source["asset_id"]
        assert source["quantity_shape"] == "interval"
        assert source["resolution"] == "1h"
        assert source["provenance"]

    failures = report["failures"]
    assert set(failures) == EXPECTED_RECIPES
    assert failures == {
        "yesterday-consumption": {"ok": False, "code": "site_required"},
        "building-spike": {"ok": False, "code": "incompatible_source"},
        "electricity-cost": {"ok": False, "code": "billing_window_required"},
        "energy-baseline": {"ok": False, "code": "invalid_arguments"},
        "building-comparison": {"ok": False, "code": "unit_incompatible"},
        "tariff-comparison": {"ok": False, "code": "alternative_required"},
    }
