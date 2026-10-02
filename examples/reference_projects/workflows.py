"""Offline SDK references for the six time-series workflow recipes."""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.models import EnergyError
from examples.reference_projects._shared import USER_ID, import_csv, site_config

SITE_ID = "workflow-reference-site"
TIMEZONE = "UTC"
METER_ASSET = "workflow-reference-meter"
TARIFF_ASSET = "workflow-reference-tariff"
PV_ASSET = "workflow-reference-pv"
SCENARIO_DAY = date(2026, 9, 29)
DAY_START = datetime(2026, 9, 29, tzinfo=UTC).isoformat()
DAY_END = datetime(2026, 9, 30, tzinfo=UTC).isoformat()
COMPARISON_START = datetime(2026, 9, 28, tzinfo=UTC).isoformat()
COMPARISON_END = DAY_END
CSV_TOOL = "CSV_READ_TIMESERIES"

PRIMARY_BILLING = {
    "standing_charge": {"amount_per_day": 0.30, "currency": "GBP", "taxable": False},
    "tax": {"rate": 0.05, "energy_taxable": True},
    "source": "synthetic primary tariff schedule",
}
ALTERNATIVE_BILLING = {
    "standing_charge": {"amount_per_day": 0.45, "currency": "GBP", "taxable": False},
    "tax": {"rate": 0.10, "energy_taxable": True},
    "source": "synthetic alternative tariff schedule",
}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("Reference CSV cannot be empty")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _hourly_rows(day: date, values: list[float]) -> list[dict[str, Any]]:
    rows = []
    for hour, value in enumerate(values):
        start = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=hour)
        rows.append(
            {
                "timestamp": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "value": value,
                "physical_meter": "false",
            }
        )
    return rows


def _write_sources(data_root: Path) -> None:
    previous_day = _hourly_rows(SCENARIO_DAY - timedelta(days=1), [0.5] * 24)
    consumption = [1.0] * 24
    consumption[9] = 5.0
    meter_rows = previous_day + _hourly_rows(SCENARIO_DAY, consumption)
    _write_csv(data_root / "synthetic_meter.csv", meter_rows)

    primary_prices = [0.1] * 12 + [0.3] * 12
    _write_csv(
        data_root / "synthetic_primary_tariff.csv",
        _hourly_rows(SCENARIO_DAY, primary_prices),
    )
    _write_csv(
        data_root / "synthetic_alternative_tariff.csv",
        _hourly_rows(SCENARIO_DAY, [0.2] * 24),
    )
    _write_csv(
        data_root / "synthetic_instantaneous_power.csv",
        [
            {
                "timestamp": datetime(2026, 9, 29, hour, tzinfo=UTC).isoformat(),
                "value": 1.0,
                "physical_meter": "false",
            }
            for hour in range(24)
        ],
    )
    _write_csv(
        data_root / "synthetic_forecast_energy.csv",
        _hourly_rows(SCENARIO_DAY, [0.5] * 24),
    )


def _binding(capability: str, asset_id: str, kind: str, unit: str) -> dict[str, Any]:
    return {
        "capability": capability,
        "tool": CSV_TOOL,
        "asset_id": asset_id,
        "kind": kind,
        "unit": unit,
        "quantity_shape": "interval",
        "resolution": "1h",
        "quality": "synthetic-reference-csv",
        "reviewed": True,
    }


def _config() -> dict[str, Any]:
    assets = [
        {
            "id": METER_ASSET,
            "site_id": SITE_ID,
            "kind": "electricity-meter",
            "name": "Synthetic interval meter",
        },
        {
            "id": TARIFF_ASSET,
            "site_id": SITE_ID,
            "kind": "tariff-schedule",
            "name": "Synthetic tariff schedule",
        },
        {
            "id": PV_ASSET,
            "site_id": SITE_ID,
            "kind": "solar-array",
            "name": "Reference solar array",
        },
    ]
    return {
        **site_config(
            site_id=SITE_ID,
            name="Workflow reference site",
            timezone=TIMEZONE,
            latitude=51.45,
            longitude=-2.59,
            assets=assets,
        ),
        "bindings": [
            _binding("get_energy_consumption", METER_ASSET, "metered", "kWh"),
            _binding("get_tariff", TARIFF_ASSET, "forecast", "GBP/kWh"),
        ],
    }


def _csv_arguments(file: str, *, kind: str, unit: str) -> dict[str, Any]:
    return {
        "file": file,
        "kind": kind,
        "unit": unit,
        "timezone": TIMEZONE,
        "quantity_shape": "interval",
        "resolution": "1h",
    }


def _parameters(*, window: tuple[str, str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "arguments": {
            "get_energy_consumption": _csv_arguments(
                "synthetic_meter.csv", kind="metered", unit="kWh"
            ),
            "get_tariff": _csv_arguments(
                "synthetic_primary_tariff.csv", kind="forecast", unit="GBP/kWh"
            ),
        },
        "tools": {"get_energy_consumption": CSV_TOOL, "get_tariff": CSV_TOOL},
    }
    if window:
        result["start"], result["end"] = window
    return result


def _capability_record(result: dict[str, Any], capability: str) -> dict[str, Any]:
    for item in result.get("evidence", []):
        if item.get("capability") == capability and item.get("ok"):
            return item
    raise AssertionError(f"No successful {capability} evidence in {result}")


def _artifact_id(result: dict[str, Any], capability: str) -> str:
    data = _capability_record(result, capability)["result"]["data"]
    assert isinstance(data, dict) and "artifact_id" in data, data
    return data["artifact_id"]


def _verify_source(
    session: Any,
    result: dict[str, Any],
    capability: str,
    *,
    asset_id: str,
    kind: str,
    unit: str,
) -> dict[str, Any]:
    envelope = _capability_record(result, capability)["result"]
    assert envelope["source"] == "local-csv", envelope
    assert envelope["site_id"] == SITE_ID, envelope
    assert envelope["asset_id"] == asset_id, envelope
    assert envelope["kind"] == kind, envelope
    assert envelope["unit"] == unit, envelope
    assert envelope["quantity_shape"] == "interval", envelope
    artifact_id = envelope["data"]["artifact_id"]
    artifact = session.agent.workbench.read(session.context, artifact_id)
    assert all(row.get("physical_meter") == "false" for row in artifact.data), artifact.data[:1]
    return {
        "capability": capability,
        "artifact_id": artifact_id,
        "source": artifact.source,
        "kind": artifact.kind.value,
        "unit": artifact.unit,
        "quantity_shape": artifact.quantity_shape,
        "resolution": artifact.resolution,
        "site_id": artifact.site_id,
        "asset_id": artifact.asset_id,
        "physical_meter": False,
        "provenance": artifact.provenance,
    }


def _analysis(result: dict[str, Any]) -> dict[str, Any]:
    for item in reversed(result.get("evidence", [])):
        analysis = item.get("analysis")
        if analysis and analysis.get("ok"):
            return analysis
    raise AssertionError(f"No successful workflow analysis in {result}")


def _result_data(session: Any, data: Any) -> Any:
    if isinstance(data, dict) and "artifact_id" in data:
        return session.agent.workbench.read(session.context, data["artifact_id"]).data
    return data


def _analysis_data(session: Any, result: dict[str, Any]) -> Any:
    return _result_data(session, _analysis(result)["result"]["data"])


def _references_artifact(session: Any, value: Any, artifact_id: str) -> bool:
    return _references_artifact_seen(session, value, artifact_id, set())


def _references_artifact_seen(session: Any, value: Any, artifact_id: str, seen: set[str]) -> bool:
    if isinstance(value, dict):
        reference = value.get("artifact_id")
        if reference == artifact_id:
            return True
        if isinstance(reference, str) and reference not in seen:
            seen.add(reference)
            try:
                stored = session.agent.workbench.read(session.context, reference)
            except EnergyError:
                stored = None
            if stored is not None and _references_artifact_seen(
                session, stored.model_dump(mode="json"), artifact_id, seen
            ):
                return True
        return any(
            _references_artifact_seen(session, nested, artifact_id, seen)
            for nested in value.values()
        )
    if isinstance(value, list):
        return any(
            _references_artifact_seen(session, nested, artifact_id, seen) for nested in value
        )
    return False


def _failure_code(result: dict[str, Any]) -> str | None:
    if result.get("error"):
        return result["error"]["code"]
    for item in result.get("evidence", []):
        if item.get("error"):
            return item["error"]["code"]
        if item.get("analysis", {}).get("error"):
            return item["analysis"]["error"]["code"]
    return None


async def run_reference() -> dict[str, Any]:
    """Run each of the six recipes, then exercise one contract failure per recipe."""
    with TemporaryDirectory(prefix="energy-workflow-reference-") as temporary:
        root = Path(temporary)
        data_root = root / "csv"
        data_root.mkdir()
        _write_sources(data_root)
        async with EnergyAgentTools(root / "state", _config(), data_root=data_root) as tools:
            tools.agent.calendar_clock = lambda: datetime(2026, 9, 30, 12, tzinfo=UTC)
            session = tools.session(USER_ID, SITE_ID)

            yesterday = await session.skill("yesterday-consumption", _parameters())
            assert yesterday["ok"], yesterday
            yesterday_data = _analysis_data(session, yesterday)
            assert yesterday_data["sum"] == 28.0, yesterday_data
            meter_source = _verify_source(
                session,
                yesterday,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            assert _references_artifact(
                session,
                _analysis(yesterday)["result"]["provenance"],
                meter_source["artifact_id"],
            )

            day_parameters = _parameters(window=(DAY_START, DAY_END))
            spike = await session.skill("building-spike", day_parameters)
            assert spike["ok"], spike
            spike_data = _analysis_data(session, spike)
            assert spike_data["summary"]["spike_count"] == 1, spike_data
            assert float(spike_data["spikes"][0]["load_kwh"]) == 5.0, spike_data
            spike_meter_source = _verify_source(
                session,
                spike,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            assert _references_artifact(
                session,
                _analysis(spike)["result"]["provenance"],
                spike_meter_source["artifact_id"],
            )

            cost_parameters = {
                **day_parameters,
                "billing": PRIMARY_BILLING,
            }
            cost = await session.skill("electricity-cost", cost_parameters)
            assert cost["ok"], cost
            bill = _analysis_data(session, cost)
            assert Decimal(bill["energy_cost"]) == Decimal("5.20"), bill
            assert Decimal(bill["standing_charge"]) == Decimal("0.30"), bill
            assert Decimal(bill["tax"]) == Decimal("0.26"), bill
            assert Decimal(bill["total"]) == Decimal("5.76"), bill
            tariff_source = _verify_source(
                session,
                cost,
                "get_tariff",
                asset_id=TARIFF_ASSET,
                kind="forecast",
                unit="GBP/kWh",
            )
            cost_meter_source = _verify_source(
                session,
                cost,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            cost_provenance = _analysis(cost)["result"]["provenance"]
            assert _references_artifact(session, cost_provenance, cost_meter_source["artifact_id"])
            assert _references_artifact(session, cost_provenance, tariff_source["artifact_id"])

            baseline_parameters = {**day_parameters, "window": 4}
            baseline = await session.skill("energy-baseline", baseline_parameters)
            assert baseline["ok"], baseline
            baseline_rows = _analysis_data(session, baseline)
            assert len(baseline_rows) == 24, len(baseline_rows)
            assert baseline_rows[4]["baseline"] == 1.0, baseline_rows[4]
            assert baseline_rows[9]["residual"] == 4.0, baseline_rows[9]
            baseline_meter_source = _verify_source(
                session,
                baseline,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            assert _references_artifact(
                session,
                _analysis(baseline)["result"]["provenance"],
                baseline_meter_source["artifact_id"],
            )

            comparison = await session.skill(
                "building-comparison",
                _parameters(window=(COMPARISON_START, COMPARISON_END)),
            )
            assert comparison["ok"], comparison
            comparison_rows = _analysis_data(session, comparison)
            current = next(row for row in comparison_rows if row["period"] == "2026-09-29")
            assert current["value"] == 28.0, current
            assert current["previous"] == 12.0, current
            assert current["difference"] == 16.0, current
            comparison_meter_source = _verify_source(
                session,
                comparison,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            assert _references_artifact(
                session,
                _analysis(comparison)["result"]["provenance"],
                comparison_meter_source["artifact_id"],
            )

            alternative_id, alternative_artifact = await import_csv(
                session,
                "synthetic_alternative_tariff.csv",
                kind="forecast",
                unit="GBP/kWh",
                timezone=TIMEZONE,
                asset_id=TARIFF_ASSET,
                quantity_shape="interval",
                resolution="1h",
            )
            assert all(row.get("physical_meter") == "false" for row in alternative_artifact.data)
            tariff_comparison_parameters = {
                **day_parameters,
                "billing": PRIMARY_BILLING,
                "alternative_tariff": alternative_id,
                "alternative_billing": ALTERNATIVE_BILLING,
            }
            tariff_comparison = await session.skill(
                "tariff-comparison", tariff_comparison_parameters
            )
            assert tariff_comparison["ok"], tariff_comparison
            tariff_comparison_meter_source = _verify_source(
                session,
                tariff_comparison,
                "get_energy_consumption",
                asset_id=METER_ASSET,
                kind="metered",
                unit="kWh",
            )
            tariff_comparison_tariff_source = _verify_source(
                session,
                tariff_comparison,
                "get_tariff",
                asset_id=TARIFF_ASSET,
                kind="forecast",
                unit="GBP/kWh",
            )
            primary_bill = _analysis_data(session, tariff_comparison)
            alternative_bill_record = next(
                item["alternative_tariff"]
                for item in tariff_comparison["evidence"]
                if "alternative_tariff" in item
            )
            alternative_bill = _result_data(session, alternative_bill_record["result"]["data"])
            assert Decimal(primary_bill["total"]) == Decimal("5.76"), primary_bill
            assert Decimal(alternative_bill["energy_cost"]) == Decimal("5.60"), alternative_bill
            assert Decimal(alternative_bill["standing_charge"]) == Decimal("0.45"), alternative_bill
            assert Decimal(alternative_bill["tax"]) == Decimal("0.56"), alternative_bill
            assert Decimal(alternative_bill["total"]) == Decimal("6.61"), alternative_bill
            tariff_provenance = _analysis(tariff_comparison)["result"]["provenance"]
            assert _references_artifact(
                session, tariff_provenance, tariff_comparison_meter_source["artifact_id"]
            )
            assert _references_artifact(
                session, tariff_provenance, tariff_comparison_tariff_source["artifact_id"]
            )
            assert _references_artifact(
                session,
                alternative_bill_record["result"]["provenance"],
                alternative_id,
            )

            invalid_power_id, invalid_power = await import_csv(
                session,
                "synthetic_instantaneous_power.csv",
                kind="metered",
                unit="kW",
                timezone=TIMEZONE,
                asset_id=METER_ASSET,
                quantity_shape="instantaneous",
                resolution="1h",
            )
            forecast_id, forecast = await import_csv(
                session,
                "synthetic_forecast_energy.csv",
                kind="forecast",
                unit="kWh",
                timezone=TIMEZONE,
                asset_id=PV_ASSET,
                quantity_shape="interval",
                resolution="1h",
            )
            assert all(row.get("physical_meter") == "false" for row in invalid_power.data)
            assert all(row.get("physical_meter") == "false" for row in forecast.data)

            missing_site = await tools.session(USER_ID).skill("yesterday-consumption")
            incompatible_spike = await session.skill(
                "building-spike",
                {"artifacts": {"get_energy_consumption": invalid_power_id}},
            )
            missing_bill_window = await session.skill(
                "electricity-cost",
                {
                    "artifacts": {
                        "get_energy_consumption": _artifact_id(cost, "get_energy_consumption"),
                        "get_tariff": _artifact_id(cost, "get_tariff"),
                    },
                    "billing": PRIMARY_BILLING,
                },
            )
            zero_baseline_window = await session.skill(
                "energy-baseline", {**day_parameters, "window": 0}
            )
            incompatible_comparison = await session.skill(
                "building-comparison",
                {
                    **day_parameters,
                    "comparison_artifact": forecast_id,
                },
            )
            missing_alternative = await session.skill(
                "tariff-comparison",
                {
                    **day_parameters,
                    "billing": PRIMARY_BILLING,
                    "alternative_billing": ALTERNATIVE_BILLING,
                },
            )
            failure_results = {
                "yesterday-consumption": missing_site,
                "building-spike": incompatible_spike,
                "electricity-cost": missing_bill_window,
                "energy-baseline": zero_baseline_window,
                "building-comparison": incompatible_comparison,
                "tariff-comparison": missing_alternative,
            }
            expected_failures = {
                "yesterday-consumption": "site_required",
                "building-spike": "incompatible_source",
                "electricity-cost": "billing_window_required",
                "energy-baseline": "invalid_arguments",
                "building-comparison": "unit_incompatible",
                "tariff-comparison": "alternative_required",
            }
            failures = {
                recipe: {"ok": result["ok"], "code": _failure_code(result)}
                for recipe, result in failure_results.items()
            }
            assert set(failures) == set(expected_failures), failures
            for recipe, code in expected_failures.items():
                assert not failure_results[recipe]["ok"], (recipe, failure_results[recipe])
                assert failures[recipe]["code"] == code, (recipe, failure_results[recipe])

            return {
                "project": "six-offline-workflow-recipes",
                "site": {"id": SITE_ID, "timezone": TIMEZONE},
                "scenario_clock": "2026-09-30T12:00:00Z",
                "scenario_day": "2026-09-29",
                "recipes": {
                    "yesterday-consumption": {"ok": True, "sum_kwh": yesterday_data["sum"]},
                    "building-spike": {
                        "ok": True,
                        "anomaly_count": spike_data["summary"]["spike_count"],
                        "spike_kwh": float(spike_data["spikes"][0]["load_kwh"]),
                    },
                    "electricity-cost": {"ok": True, "bill_gbp": bill},
                    "energy-baseline": {
                        "ok": True,
                        "rows": len(baseline_rows),
                        "baseline_at_hour_4_kwh": baseline_rows[4]["baseline"],
                        "residual_at_spike_kwh": baseline_rows[9]["residual"],
                    },
                    "building-comparison": {"ok": True, "current_day": current},
                    "tariff-comparison": {
                        "ok": True,
                        "primary_bill_gbp": primary_bill,
                        "alternative_bill_gbp": alternative_bill,
                    },
                },
                "sources": [
                    meter_source,
                    spike_meter_source,
                    cost_meter_source,
                    tariff_source,
                    baseline_meter_source,
                    comparison_meter_source,
                    tariff_comparison_meter_source,
                    tariff_comparison_tariff_source,
                ],
                "alternative_tariff_source": {
                    "artifact_id": alternative_id,
                    "source": alternative_artifact.source,
                    "kind": alternative_artifact.kind.value,
                    "unit": alternative_artifact.unit,
                    "quantity_shape": alternative_artifact.quantity_shape,
                    "resolution": alternative_artifact.resolution,
                    "site_id": alternative_artifact.site_id,
                    "asset_id": alternative_artifact.asset_id,
                    "physical_meter": False,
                    "provenance": alternative_artifact.provenance,
                },
                "failures": failures,
                "limitations": [
                    "Meter-shaped rows are synthetic caller-declared CSV data; physical_meter=false.",
                    "Billing uses the two explicit caller schedules and does not infer statutory charges.",
                ],
            }


async def main() -> None:
    print(json.dumps(await run_reference(), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
