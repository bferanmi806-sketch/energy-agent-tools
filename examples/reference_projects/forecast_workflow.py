"""Offline three-month history to eight-day forecast-bill reference project."""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from energy_agent_tools import EnergyAgentTools

USER_ID = "forecast-reference-user"
SITE_ID = "synthetic-home"
HISTORY_START = datetime(2026, 7, 1, tzinfo=UTC)
FORECAST_START = datetime(2026, 10, 1, tzinfo=UTC)
FORECAST_END = datetime(2026, 10, 9, tzinfo=UTC)
BILLING = {
    "standing_charge": {"amount_per_day": 0.30, "currency": "GBP", "taxable": False},
    "tax": {"rate": 0.05, "energy_taxable": True},
    "source": "explicit synthetic reviewed tariff components",
}


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _write_history(path: Path) -> int:
    timestamp = HISTORY_START
    rows = 0
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "end", "value", "physical_meter"])
        writer.writeheader()
        while timestamp < FORECAST_START:
            end = timestamp + timedelta(minutes=30)
            writer.writerow(
                {
                    "timestamp": _utc(timestamp),
                    "end": _utc(end),
                    "value": "0.5",
                    "physical_meter": "false",
                }
            )
            timestamp = end
            rows += 1
    return rows


def _write_tariff(path: Path, valid_until: datetime) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "end", "value", "physical_meter"])
        writer.writeheader()
        writer.writerow(
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "end": _utc(valid_until),
                "value": "20",
                "physical_meter": "false",
            }
        )


def _binding(capability: str, filename: str, kind: str, unit: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "file": filename,
        "kind": kind,
        "unit": unit,
        "timezone": "UTC",
        "quantity_shape": "interval",
    }
    binding: dict[str, Any] = {
        "capability": capability,
        "tool": "CSV_READ_TIMESERIES",
        "reviewed": True,
        "kind": kind,
        "unit": unit,
        "quantity_shape": "interval",
        "fixed_arguments": arguments,
    }
    if capability == "get_tariff":
        arguments["window_mode"] = "overlap"
    if capability == "get_energy_consumption":
        arguments["resolution"] = "30min"
        binding["resolution"] = "30min"
    return binding


def _source_evidence(artifact_id: str, artifact: Any, *, row_count: int) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": artifact.kind.value,
        "unit": artifact.unit,
        "quantity_shape": artifact.quantity_shape,
        "resolution": artifact.resolution,
        "source": artifact.source,
        "quality": artifact.quality,
        "site_id": artifact.site_id,
        "time_start": artifact.time_start.isoformat() if artifact.time_start else None,
        "time_end": artifact.time_end.isoformat() if artifact.time_end else None,
        "rows": row_count,
        "provenance": artifact.provenance,
        "warnings": artifact.warnings,
    }


async def _run() -> dict[str, Any]:
    config = {
        "sites": [
            {
                "id": SITE_ID,
                "user_id": USER_ID,
                "name": "Synthetic forecast reference home",
                "timezone": "UTC",
            }
        ],
        "bindings": [
            _binding("get_energy_consumption", "history.csv", "metered", "kWh"),
            _binding("get_tariff", "tariff.csv", "forecast", "p/kWh"),
        ],
    }
    with TemporaryDirectory(prefix="energy-forecast-reference-") as temporary:
        root = Path(temporary)
        data_root = root / "data"
        data_root.mkdir()
        history_rows = _write_history(data_root / "history.csv")
        tariff_end = datetime(2026, 11, 1, tzinfo=UTC)
        tariff_path = data_root / "tariff.csv"
        _write_tariff(tariff_path, tariff_end)

        async with EnergyAgentTools(root / "state", config, data_root=data_root) as energy:
            session = energy.session(USER_ID, SITE_ID)
            parameters = {
                "start": FORECAST_START.isoformat(),
                "end": FORECAST_END.isoformat(),
                "billing": BILLING,
                "context_mode": "explicit",
                "history_end": FORECAST_START.isoformat(),
            }
            response = await session.skill("forecast-bill", parameters)
            if not response["ok"]:
                raise RuntimeError(f"forecast-bill failed: {response['error']}")

            history_resolution = next(
                item
                for item in response["evidence"]
                if item.get("capability") == "get_energy_consumption" and "history_chunks" in item
            )
            tariff_resolution = next(
                item for item in response["evidence"] if item.get("capability") == "get_tariff"
            )
            history_id = history_resolution["artifact_id"]
            tariff_id = tariff_resolution["result"]["data"]["artifact_id"]
            forecast_id = response["forecast_artifact"]
            analysis = next(item["analysis"] for item in response["evidence"] if "analysis" in item)
            bill_id = analysis["result"]["data"]["artifact_id"]

            history = energy.agent.workbench.read(session.context, history_id)
            tariff = energy.agent.workbench.read(session.context, tariff_id)
            forecast = energy.agent.workbench.read(session.context, forecast_id)
            bill = energy.agent.workbench.read(session.context, bill_id)
            history_physical_meter = history.data[0].get("physical_meter", "").lower() == "true"
            forecast_data = forecast.model_dump(mode="json")["data"]
            bill_data = bill.model_dump(mode="json")["data"]
            intervals = bill_data["intervals"]

            # Exercise the real tariff-coverage guard with a reviewed CSV tariff
            # that ends one day before the requested forecast horizon.
            shortened_end = datetime(2026, 10, 8, tzinfo=UTC)
            _write_tariff(tariff_path, shortened_end)
            missing_coverage = await session.skill("forecast-bill", parameters)
            if missing_coverage["ok"]:
                raise RuntimeError(
                    "short tariff unexpectedly covered the complete forecast horizon"
                )

            return {
                "project": "history-to-forecast-bill",
                "execution": "offline; reviewed local CSV bindings; synthetic fixture values",
                "window": response["window"],
                "request": {
                    "skill_id": "forecast-bill",
                    "start": FORECAST_START.isoformat(),
                    "end": FORECAST_END.isoformat(),
                    "history_months": 3,
                    "billing": BILLING,
                },
                "source_roles": {
                    "get_energy_consumption": {
                        **_source_evidence(history_id, history, row_count=len(history.data)),
                        "synthetic": True,
                        "written_rows": history_rows,
                        "retrieval_chunks": history_resolution["history_chunks"],
                        "physical_meter": history_physical_meter,
                        "interval_start_column": "timestamp",
                        "interval_end_column": "end",
                    },
                    "get_tariff": {
                        **_source_evidence(tariff_id, tariff, row_count=len(tariff.data)),
                        "validity": {
                            "start": "2026-09-01T00:00:00Z",
                            "end": _utc(tariff_end),
                        },
                        "rate": 20,
                        "rate_unit": "p/kWh",
                    },
                },
                "forecast": {
                    "artifact_id": forecast_id,
                    "kind": forecast.kind.value,
                    "unit": forecast.unit,
                    "quantity_shape": forecast.quantity_shape,
                    "resolution": forecast.resolution,
                    "interval_count": len(forecast_data["intervals"]),
                    "summary": forecast_data["summary"],
                    "model": forecast_data["model"],
                    "provenance": forecast.provenance,
                    "assumptions": forecast.assumptions,
                    "warnings": forecast.warnings,
                },
                "bill": {
                    "artifact_id": bill_id,
                    "kind": bill.kind.value,
                    "unit": bill.unit,
                    "quantity_shape": bill.quantity_shape,
                    "calculation_basis": bill_data["calculation_basis"],
                    "estimate": bill_data["estimate"],
                    "uncertainty": bill_data["uncertainty"],
                    "interval_count": len(intervals),
                    "interval_example": intervals[0],
                    "provenance": bill.provenance,
                    "assumptions": bill.assumptions,
                    "warnings": bill.warnings,
                },
                "workflow_evidence": response["evidence"],
                "limits": [
                    "All meter rows are synthetic and caller-declared; physical_meter is false.",
                    "Future consumption and cost are forecasts, not measured use or a provider-issued bill.",
                    "The supplied tariff is treated as fixed for its declared validity; no future rates are inferred.",
                    bill_data["uncertainty"]["interpretation"],
                    *bill.assumptions,
                ],
                "missing_tariff_coverage_example": {
                    "tariff_validity_end": _utc(shortened_end),
                    "forecast_horizon_end": _utc(FORECAST_END),
                    "ok": missing_coverage["ok"],
                    "error": missing_coverage["error"],
                    "evidence": missing_coverage["evidence"],
                },
            }


def main() -> None:
    report = asyncio.run(_run())
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
