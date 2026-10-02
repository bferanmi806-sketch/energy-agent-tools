from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from benchmarks.environments import BuiltEnvironment, ScenarioContext
from benchmarks.scenarios import scenario_cases
from benchmarks.telemetry_environments import (
    EXCLUDED_TELEMETRY_CASES,
    QUALIFIED_TELEMETRY_CASE_IDS,
    SCENARIO_CLOCKS,
    EnvironmentUnavailable,
    build_telemetry_environment,
)
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.models import DataKind

CASE_INDEX = {case.id: case for case in scenario_cases()}


def _artifact_id(response: dict) -> str:
    assert response["ok"] is True
    data = response["result"]["data"]
    assert isinstance(data, dict)
    return data["artifact_id"]


def test_qualified_set_has_only_supported_development_cases() -> None:
    assert QUALIFIED_TELEMETRY_CASE_IDS == {
        "dev_consumption_home_assistant",
        "dev_consumption_local_day_dublin",
        "dev_units_kw_kwh",
        "dev_counter_reset_quality",
        "dev_field_units_provenance",
    }
    for case_id in QUALIFIED_TELEMETRY_CASE_IDS:
        case = CASE_INDEX[case_id]
        assert case.split == "development"
        assert case.status == "pending_environment"
        assert case.fixture_case_id is None
        assert SCENARIO_CLOCKS[case_id].tzinfo is not None


def test_unqualified_cases_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentUnavailable, match="quantity_shape"):
        build_telemetry_environment(
            "dev_consumption_counter_reset", tmp_path / "root", tmp_path / "state"
        )
    with pytest.raises(EnvironmentUnavailable):
        build_telemetry_environment(
            "holdout_consumption_emoncms", tmp_path / "root", tmp_path / "state"
        )
    with pytest.raises(EnvironmentUnavailable):
        build_telemetry_environment("unknown", tmp_path / "root", tmp_path / "state")
    assert "dev_consumption_counter_reset" in EXCLUDED_TELEMETRY_CASES


@pytest.mark.asyncio
async def test_home_assistant_counter_is_reviewed_then_explicitly_differenced(
    tmp_path: Path,
) -> None:
    built = build_telemetry_environment(
        "dev_consumption_home_assistant", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        assert isinstance(built, BuiltEnvironment)
        assert isinstance(context, ScenarioContext)
        truth = json.loads((context.root / "provider-truth.json").read_text())
        assert (
            b"telemetry-home-assistant-fixture-token"
            not in (context.state_dir / "vault" / "auth.sqlite3").read_bytes()
        )
        raw_values = [Decimal(row["state"]) for row in truth["rows"]]
        assert len(raw_values) == 25
        assert sum(raw_values[index] - raw_values[index - 1] for index in range(1, 25)) == Decimal(
            "24"
        )

        session = built.agent.session(context.user_id, context.site_id)
        provider = await built.agent.resolver.execute(
            session,
            CapabilityRequest(
                capability="get_energy_consumption",
                asset_id="dublin-home-meter",
                arguments=context.arguments(),
            ),
            persist=True,
        )
        raw_id = _artifact_id(provider)
        raw = built.agent.workbench.read(session, raw_id)
        assert raw.quantity_shape == "counter"
        assert raw.site_id == context.site_id
        assert raw.asset_id == "dublin-home-meter"
        assert raw.provenance[-1]["account_id"] == "ha-dublin"

        differenced = await built.agent.execute(
            session,
            "WORKBENCH_ENERGY_OPERATION",
            {
                "operation": "counter",
                "artifact_ids": [raw_id],
                "parameters": {"timestamp": "timestamp", "column": "value", "frequency": "1h"},
            },
            expected_kind=DataKind.CALCULATED,
            expected_unit="kWh",
            persist=True,
        )
        differenced_id = _artifact_id(differenced)
        result = built.agent.workbench.read(session, differenced_id)
        assert sum(row["value"] or 0 for row in result.data) == pytest.approx(24.0)
        assert result.data[0]["status"] == "initial"
        assert all(row["status"] == "ok" for row in result.data[1:])
        assert raw.quantity_shape == "counter"
        selected = await built.agent.execute(
            session,
            "WORKBENCH_WINDOW",
            {"artifact_id": differenced_id, "timestamp": "timestamp", **context.arguments()},
            persist=True,
        )
        selected_result = built.agent.workbench.read(session, _artifact_id(selected))
        assert selected_result.quantity_shape == "interval"
        assert len(selected_result.data) == 24
        assert sum(row["value"] for row in selected_result.data) == pytest.approx(24.0)
        assert selected_result.data[-1]["end"] == context.window_end.isoformat()

        missing_entity = await built.agent.execute(
            session,
            "home_assistant.get_history",
            {**context.arguments(), "entity_id": "sensor.unknown"},
            account_id="ha-dublin",
            asset_id="dublin-home-meter",
        )
        assert missing_entity["ok"] is False
        assert missing_entity["error"]["code"] == "provider_not_found"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_dublin_local_day_is_not_silently_replaced_by_utc_day(tmp_path: Path) -> None:
    built = build_telemetry_environment(
        "dev_consumption_local_day_dublin", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        truth = json.loads((context.root / "provider-truth.json").read_text())
        assert (
            b"telemetry-emoncms-fixture-key"
            not in (context.state_dir / "vault" / "auth.sqlite3").read_bytes()
        )
        source_rows = truth["rows"]
        local_start = context.window_start
        local_end = context.window_end
        utc_start = datetime.combine(context.scenario_date, datetime.min.time(), tzinfo=UTC)
        utc_end = utc_start + timedelta(days=1)

        def independent_total(start: datetime, end: datetime) -> tuple[int, Decimal, list[float]]:
            selected = [row for row in source_rows if start.timestamp() <= row[0] < end.timestamp()]
            return (
                len(selected),
                sum((Decimal(str(row[1])) for row in selected), Decimal()),
                [row[0] for row in selected],
            )

        local_count, local_total, local_timestamps = independent_total(local_start, local_end)
        utc_count, utc_total, utc_timestamps = independent_total(utc_start, utc_end)
        assert local_count == utc_count == 24
        assert local_timestamps != utc_timestamps
        assert local_total != utc_total

        session = built.agent.session(context.user_id, context.site_id)
        local_result = await built.agent.resolver.execute(
            session,
            CapabilityRequest(
                capability="get_energy_consumption",
                asset_id="dublin-home-meter",
                arguments=context.arguments(),
            ),
            persist=True,
        )
        local_id = _artifact_id(local_result)
        utc_result = await built.agent.execute(
            session,
            "openenergymonitor.get_feed",
            {
                "start": utc_start.isoformat(),
                "end": utc_end.isoformat(),
                "feed_id": 7,
                "unit": "kWh",
                "interval": 3600,
            },
            account_id="emon-dublin",
            asset_id="dublin-home-meter",
            expected_kind=DataKind.METERED,
            expected_unit="kWh",
            expected_quantity_shape="interval",
            expected_resolution="3600s",
            persist=True,
        )
        utc_id = _artifact_id(utc_result)
        local_raw = built.agent.workbench.read(session, local_id)
        utc_raw = built.agent.workbench.read(session, utc_id)
        assert len(local_raw.data) == local_count
        assert len(utc_raw.data) == utc_count
        assert sum(row["value"] for row in local_raw.data) == pytest.approx(float(local_total))
        assert sum(row["value"] for row in utc_raw.data) == pytest.approx(float(utc_total))
        assert local_raw.provenance[-1]["account_id"] == "emon-dublin"
        assert local_raw.asset_id == utc_raw.asset_id == "dublin-home-meter"
        assert local_raw.site_id == utc_raw.site_id == context.site_id

        invalid_interval = await built.agent.execute(
            session,
            "openenergymonitor.get_feed",
            {**context.arguments(), "feed_id": 7, "unit": "kWh", "interval": 0},
            account_id="emon-dublin",
            asset_id="dublin-home-meter",
        )
        assert invalid_interval["ok"] is False
        assert invalid_interval["error"]["code"] == "invalid_arguments"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_power_conversion_uses_explicit_duration_and_refuses_missing_duration(
    tmp_path: Path,
) -> None:
    built = build_telemetry_environment("dev_units_kw_kwh", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        with (context.root / "power.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        assert Decimal(rows[0]["power_kw"]) == Decimal("5.0")
        start = datetime.fromisoformat(rows[0]["timestamp"])
        end = datetime.fromisoformat(rows[0]["end"])
        assert end - start == timedelta(minutes=30)
        assert Decimal(rows[0]["power_kw"]) * Decimal("0.5") == Decimal("2.5")

        session = built.agent.session(context.user_id, context.site_id)
        provider = await built.agent.execute(
            session,
            "CSV_READ_TIMESERIES",
            {
                "file": "power.csv",
                "kind": "metered",
                "unit": "kW",
                "timezone": context.timezone,
                "timestamp": "timestamp",
            },
            asset_id="manchester-power-reading",
            expected_kind=DataKind.METERED,
            expected_unit="kW",
            persist=True,
        )
        raw_id = _artifact_id(provider)
        raw = built.agent.workbench.read(session, raw_id)
        assert raw.site_id == context.site_id
        assert raw.asset_id == "manchester-power-reading"

        converted = await built.agent.execute(
            session,
            "WORKBENCH_ENERGY_OPERATION",
            {
                "operation": "integrate_power",
                "artifact_ids": [raw_id],
                "parameters": {
                    "timestamp": "timestamp",
                    "column": "power_kw",
                    "end": "end",
                    "method": "left",
                },
            },
            expected_kind=DataKind.CALCULATED,
            expected_unit="kWh",
            persist=True,
        )
        result = built.agent.workbench.read(session, _artifact_id(converted))
        assert result.data == [
            {"timestamp": rows[0]["timestamp"], "end": rows[0]["end"], "energy": 2.5}
        ]
        assert result.provenance[0]["inputs"][0]["unit"] == "kW"

        no_duration = await built.agent.execute(
            session,
            "WORKBENCH_ENERGY_OPERATION",
            {
                "operation": "integrate_power",
                "artifact_ids": [raw_id],
                "parameters": {
                    "timestamp": "timestamp",
                    "column": "power_kw",
                    "end": "missing_end",
                    "method": "left",
                },
            },
        )
        assert no_duration["ok"] is False
        assert no_duration["error"]["code"] == "insufficient_rows"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_counter_reset_quality_marks_only_monotonic_intervals(tmp_path: Path) -> None:
    built = build_telemetry_environment(
        "dev_counter_reset_quality", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        truth = json.loads((context.root / "provider-truth.json").read_text())
        assert (
            b"telemetry-emoncms-fixture-key"
            not in (context.state_dir / "vault" / "auth.sqlite3").read_bytes()
        )
        values = [Decimal(str(row[1])) for row in truth["rows"]]
        assert values == [
            Decimal("9998"),
            Decimal("12"),
            Decimal("13"),
            Decimal("15"),
            Decimal("16"),
        ]
        assert truth["valid_delta_kwh"] == 4.0

        session = built.agent.session(context.user_id, context.site_id)
        provider = await built.agent.resolver.execute(
            session,
            CapabilityRequest(
                capability="get_energy_consumption",
                asset_id="cape-town-counter",
                arguments=context.arguments(),
            ),
            persist=True,
        )
        raw_id = _artifact_id(provider)
        raw = built.agent.workbench.read(session, raw_id)
        assert raw.quantity_shape == "counter"
        assert raw.provenance[-1]["account_id"] == "emon-cape-town"

        transformed = await built.agent.execute(
            session,
            "WORKBENCH_ENERGY_OPERATION",
            {
                "operation": "counter",
                "artifact_ids": [raw_id],
                "parameters": {"timestamp": "timestamp", "column": "value", "frequency": "1h"},
            },
            expected_kind=DataKind.CALCULATED,
            expected_unit="kWh",
            persist=True,
        )
        result = built.agent.workbench.read(session, _artifact_id(transformed))
        assert result.data[1]["status"] == "counter_reset"
        assert result.data[1]["value"] is None
        assert sum(row["value"] or 0 for row in result.data) == pytest.approx(4.0)
        assert any("counter reset" in warning for warning in result.warnings)

        wrong_shape = await built.agent.execute(
            session,
            "openenergymonitor.get_feed",
            {**context.arguments(), "feed_id": 11, "unit": "kWh", "interval": 3600},
            account_id="emon-cape-town",
            asset_id="cape-town-counter",
            expected_kind=DataKind.METERED,
            expected_unit="kWh",
            expected_quantity_shape="interval",
        )
        assert wrong_shape["ok"] is False
        assert wrong_shape["error"]["code"] == "binding_semantics_changed"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_field_units_survive_provider_read_and_pivot_provenance(tmp_path: Path) -> None:
    built = build_telemetry_environment(
        "dev_field_units_provenance", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        truth = json.loads((context.root / "provider-truth.json").read_text())
        assert truth["field_units"] == {"power_kw": "kW", "energy_kwh": "kWh"}
        session = built.agent.session(context.user_id, context.site_id)
        provider = await built.agent.execute(
            session,
            "CSV_READ_TIMESERIES",
            {
                "file": "fields.csv",
                "kind": "metered",
                "unit": "mixed",
                "timezone": context.timezone,
                "timestamp": "timestamp",
            },
            asset_id="manchester-mixed-telemetry",
            expected_kind=DataKind.METERED,
            expected_unit="mixed",
            persist=True,
        )
        raw_id = _artifact_id(provider)
        raw = built.agent.workbench.read(session, raw_id)
        assert raw.site_id == context.site_id
        assert raw.asset_id == "manchester-mixed-telemetry"
        assert raw.field_units == {}

        pivoted = await built.agent.execute(
            session,
            "WORKBENCH_PIVOT",
            {
                "artifact_id": raw_id,
                "timestamp": "timestamp",
                "variable": "variable",
                "value": "value",
            },
            expected_kind=DataKind.CALCULATED,
            expected_unit="mixed",
            persist=True,
        )
        result = built.agent.workbench.read(session, _artifact_id(pivoted))
        column_units = next(
            item["column_units"] for item in result.provenance if "column_units" in item
        )
        assert column_units == {"energy_kwh": ["kWh"], "power_kw": ["kW"]}
        assert result.data[0]["power_kw"] == "5.0"
        assert result.data[0]["energy_kwh"] == "2.5"

        malformed = await built.agent.execute(
            session,
            "WORKBENCH_PIVOT",
            {
                "artifact_id": raw_id,
                "timestamp": "timestamp",
                "variable": "missing",
                "value": "value",
            },
        )
        assert malformed["ok"] is False
        assert malformed["error"]["code"] == "column_not_found"
    finally:
        await built.close()
