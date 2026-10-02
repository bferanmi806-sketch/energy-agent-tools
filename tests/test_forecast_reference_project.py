from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def test_forecast_reference_runs_offline_and_reports_provenance() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = repository / "examples/reference_projects/forecast_workflow.py"
    environment = os.environ.copy()
    source_paths = [
        path for path in (environment.get("PYTHONPATH"), str(repository / "src")) if path
    ]
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)

    process = subprocess.run(
        [sys.executable, str(script)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)

    history = report["source_roles"]["get_energy_consumption"]
    tariff = report["source_roles"]["get_tariff"]
    forecast = report["forecast"]
    bill = report["bill"]
    assert report["execution"].startswith("offline;")
    assert history["kind"] == "metered"
    assert history["unit"] == "kWh" and history["quantity_shape"] == "interval"
    assert history["rows"] == history["written_rows"] == 92 * 48
    assert history["retrieval_chunks"] == 4
    assert history["physical_meter"] is False
    assert history["interval_start_column"] == "timestamp"
    assert history["interval_end_column"] == "end"
    assert tariff["kind"] == "forecast" and tariff["unit"] == "p/kWh"
    assert tariff["quantity_shape"] == "interval"
    assert tariff["validity"] == {
        "start": "2026-09-01T00:00:00Z",
        "end": "2026-11-01T00:00:00Z",
    }
    assert forecast["kind"] == "forecast" and forecast["unit"] == "kWh"
    assert forecast["quantity_shape"] == "interval"
    assert forecast["interval_count"] == 8 * 48
    assert Decimal(str(forecast["summary"]["total_kwh"])) == Decimal("192")
    assert forecast["model"]
    assert bill["kind"] == "calculated" and bill["unit"] == "GBP"
    assert bill["calculation_basis"] == "forecast_consumption"
    assert bill["estimate"]["complete_bill"] is True
    assert Decimal(str(bill["estimate"]["energy_cost"])) == Decimal("38.4")
    assert Decimal(str(bill["estimate"]["standing_charge"])) == Decimal("2.4")
    assert Decimal(str(bill["estimate"]["tax"])) == Decimal("1.92")
    assert Decimal(str(bill["estimate"]["total"])) == Decimal("42.72")

    history_id = history["artifact_id"]
    tariff_id = tariff["artifact_id"]
    forecast_id = forecast["artifact_id"]
    bill_lineage = json.dumps(bill["provenance"])
    forecast_lineage = json.dumps(forecast["provenance"])
    assert history_id in forecast_lineage
    assert forecast_id in bill_lineage and tariff_id in bill_lineage
    assert report["workflow_evidence"]
    assert report["limits"]

    failure = report["missing_tariff_coverage_example"]
    assert failure["ok"] is False
    assert failure["error"]["code"] == "missing_rate_coverage"
    assert failure["tariff_validity_end"] < failure["forecast_horizon_end"]
