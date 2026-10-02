from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from benchmarks.environments import (
    QUALIFIED_ENVIRONMENT_CASE_IDS,
    EnvironmentUnavailable,
    build_environment,
)
from benchmarks.scenarios import scenario_cases
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.models import DataKind

CASE_INDEX = {case.id: case for case in scenario_cases()}


def test_qualified_set_is_explicit_and_does_not_promote_scenarios() -> None:
    assert QUALIFIED_ENVIRONMENT_CASE_IDS == {
        "dev_consumption_daily_csv",
        "dev_current_power_snapshot",
        "dev_consumption_interval_gap",
        "dev_two_account_selection",
    }
    for case_id in QUALIFIED_ENVIRONMENT_CASE_IDS:
        case = CASE_INDEX[case_id]
        assert case.split == "development"
        assert case.status == "pending_environment"
        assert case.fixture_case_id is None


def test_unsupported_and_heldout_cases_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentUnavailable):
        build_environment(
            "holdout_consumption_provider_swap", tmp_path / "root", tmp_path / "state"
        )
    with pytest.raises(EnvironmentUnavailable):
        build_environment("dev_tariff_pence_to_gbp", tmp_path / "root", tmp_path / "state")
    with pytest.raises(EnvironmentUnavailable):
        build_environment("not-a-scenario", tmp_path / "root", tmp_path / "state")


@pytest.mark.asyncio
async def test_daily_csv_is_complete_and_sums_42_5_kwh(tmp_path: Path) -> None:
    built = build_environment("dev_consumption_daily_csv", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        assert context.timezone == "Europe/London"
        assert context.scenario_date.isoformat() == "2026-09-29"
        assert context.window_end - context.window_start == timedelta(days=1)

        # Independent truth: parse the operator file directly, before asking
        # the production CSV connector to read it.
        with (context.root / "meter.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        total = sum((Decimal(row["energy_kwh"]) for row in rows), Decimal("0"))
        assert len(rows) == 48
        assert total == Decimal("42.5000")
        assert {"timestamp", "energy_kwh", "average_power_kw", "quality"} <= set(rows[0])

        session = built.agent.session(context.user_id, context.site_id)
        result = await built.agent.execute(
            session,
            "CSV_READ_TIMESERIES",
            {
                "file": "meter.csv",
                "kind": "metered",
                "unit": "kWh",
                "timezone": context.timezone,
                "timestamp": "timestamp",
                **context.arguments(),
            },
            asset_id="manchester-office-meter",
            expected_kind=DataKind.METERED,
            expected_unit="kWh",
        )
        assert result["ok"] is True
        output = result["result"]
        assert len(output["data"]) == 48
        assert sum(float(row["energy_kwh"]) for row in output["data"]) == pytest.approx(42.5)
        assert output["site_id"] == context.site_id
        assert output["asset_id"] == "manchester-office-meter"
        assert output["provenance"][0]["file"] == "meter.csv"
    finally:
        await built.close()
    assert built._http.is_closed


@pytest.mark.asyncio
async def test_current_power_uses_fresh_home_assistant_state_in_kw(tmp_path: Path) -> None:
    built = build_environment("dev_current_power_snapshot", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        session = built.agent.session(context.user_id, context.site_id)
        result = await built.agent.execute(
            session,
            "home_assistant.get_state",
            {"entity_id": "sensor.school_power"},
            account_id="ha-school",
            asset_id="new-york-school-meter",
            expected_kind=DataKind.METERED,
            expected_unit="kW",
        )
        assert result["ok"] is True
        output = result["result"]
        assert output["data"]["entity_id"] == "sensor.school_power"
        assert output["data"]["state"] == "12.75"
        assert output["unit"] == "kW"
        assert output["kind"] == DataKind.METERED.value
        observed_at = datetime.fromisoformat(output["data"]["last_updated"].replace("Z", "+00:00"))
        age_seconds = (context.scenario_clock - observed_at).total_seconds()
        assert 0 <= age_seconds <= 60
        assert output["site_id"] == context.site_id
        assert output["asset_id"] == "new-york-school-meter"
        assert output["provenance"][0]["endpoint"] == "/api/states/sensor.school_power"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_octopus_adapter_and_workbench_report_six_missing_intervals(tmp_path: Path) -> None:
    built = build_environment("dev_consumption_interval_gap", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        session = built.agent.session(context.user_id, context.site_id)
        provider = await built.agent.execute(
            session,
            "octopus_energy.get_consumption",
            context.arguments(),
            account_id="octopus-gap",
            asset_id="bristol-workshop-meter",
            persist=True,
            expected_kind=DataKind.METERED,
            expected_unit="kWh",
        )
        assert provider["ok"] is True
        artifact = provider["result"]["data"]
        assert artifact["rows"] == 42
        source = built.agent.workbench.read(session, artifact["artifact_id"])
        assert source.kind is DataKind.METERED
        assert source.unit == "kWh"
        assert source.provider == "octopus-energy"
        assert source.site_id == context.site_id

        missing = await built.agent.execute(
            session,
            "WORKBENCH_ENERGY_OPERATION",
            {
                "operation": "missing",
                "artifact_ids": [artifact["artifact_id"]],
                "parameters": {"timestamp": "from", "column": "value", "frequency": "30min"},
            },
            expected_kind=DataKind.CALCULATED,
            expected_unit="kWh",
        )
        assert missing["ok"] is True
        output = missing["result"]
        assert len(output["data"]) == 48
        assert sum(bool(row["missing"]) for row in output["data"]) == 6
        assert "Detected 6 missing interval(s)." in output["warnings"]
        assert output["provenance"][0]["operation"] == "missing"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_two_octopus_accounts_are_ambiguous_until_asset_selected(tmp_path: Path) -> None:
    built = build_environment("dev_two_account_selection", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        session = built.agent.session(context.user_id, context.site_id)
        connections = built.agent.connections(session)
        assert {connection["id"] for connection in connections} == {
            "octopus-annex",
            "octopus-home",
        }
        assert {connection["site_id"] for connection in connections} == {context.site_id}
        assert built.agent.assets["dublin-home-meter"].account_ids == ["octopus-home"]
        assert built.agent.assets["dublin-annex-meter"].account_ids == ["octopus-annex"]

        ambiguous = built.agent.resolver.resolve(
            session,
            CapabilityRequest(capability="get_energy_consumption"),
        )
        assert ambiguous["status"] == "ambiguous"
        assert ambiguous["selected"] is None
        assert {
            candidate["account_id"]
            for candidate in ambiguous["candidates"]
            if candidate["asset_id"] in {"dublin-home-meter", "dublin-annex-meter"}
        } == {"octopus-home", "octopus-annex"}

        selected = built.agent.resolver.resolve(
            session,
            CapabilityRequest(
                capability="get_energy_consumption",
                account_id="octopus-home",
                asset_id="dublin-home-meter",
                arguments=context.arguments(),
            ),
        )
        assert selected["status"] == "resolved"
        assert selected["selected"]["account_id"] == "octopus-home"
        assert selected["selected"]["asset_id"] == "dublin-home-meter"

        result = await built.agent.execute(
            session,
            "octopus_energy.get_consumption",
            context.arguments(),
            account_id="octopus-home",
            asset_id="dublin-home-meter",
            expected_kind=DataKind.METERED,
            expected_unit="kWh",
        )
        assert result["ok"] is True
        assert result["result"]["data"][0]["value"] == 1.0
        assert result["result"]["provenance"][-1]["account_id"] == "octopus-home"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_shared_dispatch_qualifies_all_family_environments(tmp_path: Path):
    from benchmarks.environments import (
        BuiltEnvironment,
        ScenarioContext,
        qualified_environment_clocks,
    )

    clocks = qualified_environment_clocks()
    assert len(clocks) == 16
    for case_id, clock in clocks.items():
        built = build_environment(case_id, tmp_path / "root", tmp_path / "state")
        try:
            assert isinstance(built, BuiltEnvironment)
            assert isinstance(built.context, ScenarioContext)
            assert built.context.case_id == case_id
            assert built.context.scenario_clock == clock
            session = built.agent.session(built.context.user_id, built.context.site_id)
            assert built.agent.sites[session.site_id].user_id == session.user_id
            assert built.agent.catalogue(session)
        finally:
            await built.close()
        assert built._http.is_closed
