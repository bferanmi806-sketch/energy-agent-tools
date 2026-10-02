from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.engineering_environments import (
    ENGINEERING_CASE_EXCLUSIONS,
    QUALIFIED_ENGINEERING_CASE_IDS,
    SCENARIO_CLOCKS,
    EnvironmentUnavailable,
    build_engineering_environment,
)
from benchmarks.scenarios import scenario_cases
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.models import DataKind, EnergyError

CASE_INDEX = {case.id: case for case in scenario_cases()}


def test_engineering_qualification_set_is_explicit() -> None:
    assert QUALIFIED_ENGINEERING_CASE_IDS == {
        "dev_power_flow_two_bus",
        "dev_heat_loss",
        "dev_optimization_advisory",
        "dev_network_asset_scope",
    }
    assert set(ENGINEERING_CASE_EXCLUSIONS) == {
        "dev_pypsa_capacity_constraint",
        "dev_bounded_simulation",
    }
    assert set(SCENARIO_CLOCKS) == QUALIFIED_ENGINEERING_CASE_IDS | set(ENGINEERING_CASE_EXCLUSIONS)
    for case_id in QUALIFIED_ENGINEERING_CASE_IDS:
        case = CASE_INDEX[case_id]
        assert case.split == "development"
        assert case.status == "pending_environment"
        assert case.fixture_case_id is None


def test_excluded_and_unknown_cases_fail_closed(tmp_path: Path) -> None:
    for case_id, reason in ENGINEERING_CASE_EXCLUSIONS.items():
        with pytest.raises(EnvironmentUnavailable, match="excluded") as error:
            build_engineering_environment(case_id, tmp_path / "root", tmp_path / "state")
        assert reason in str(error.value)
    with pytest.raises(EnvironmentUnavailable, match="unknown"):
        build_engineering_environment("not-a-scenario", tmp_path / "root", tmp_path / "state")


@pytest.mark.asyncio
async def test_power_flow_uses_asset_model_and_real_pandapower_runtime(tmp_path: Path) -> None:
    built = build_engineering_environment(
        "dev_power_flow_two_bus", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        asset_id = "manchester-office-two-bus"
        asset = built.agent.assets[asset_id]
        model = asset.metadata["engineering"]
        assert model["tool"] == "engineering.run_power_flow"
        assert model["control_mode"] == "simulation-only"
        assert model["arguments"]["network"]["buses"]

        session = built.agent.session(context.user_id, context.site_id)
        result = await built.agent.resolver.execute(
            session,
            CapabilityRequest(capability="run_power_flow", asset_id=asset_id),
        )
        assert result["ok"] is True
        output = result["result"]
        assert output["kind"] == DataKind.SIMULATED.value
        assert output["asset_id"] == asset_id
        assert output["site_id"] == context.site_id
        assert output["data"]["converged"] is True
        assert output["data"]["totals"]["balance_error_mw"] == pytest.approx(0.0, abs=1e-6)
        assert output["data"]["totals"]["line_loss_mw"] > 0
        load_bus = next(row for row in output["data"]["buses"] if row["id"] == "office")
        assert 0 < load_bus["vm_pu"] < 1.0
        assert any(item.get("library") == "pandapower" for item in output["provenance"])
        assert any(
            item.get("tool") == "engineering.run_power_flow" for item in output["provenance"]
        )
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_heat_loss_has_independent_12_kw_truth_and_input_provenance(tmp_path: Path) -> None:
    built = build_engineering_environment("dev_heat_loss", tmp_path / "root", tmp_path / "state")
    try:
        context = built.context
        asset_id = "manchester-office-envelope"
        arguments = built.agent.assets[asset_id].metadata["engineering"]["arguments"]
        ua = sum(
            component["area_m2"] * component["u_value_w_m2k"]
            for component in arguments["components"]
        )
        delta_t = arguments["indoor_temp_c"] - arguments["outdoor_temp_c"]
        assert ua == pytest.approx(500.0)
        assert ua * delta_t / 1000 == pytest.approx(12.0)

        session = built.agent.session(context.user_id, context.site_id)
        result = await built.agent.resolver.execute(
            session,
            CapabilityRequest(capability="calculate_heat_loss", asset_id=asset_id),
        )
        assert result["ok"] is True
        output = result["result"]
        assert output["kind"] == DataKind.CALCULATED.value
        assert output["unit"] == "kW_th and kWh_th"
        assert output["data"]["gross_heat_loss_kw"] == pytest.approx(12.0, abs=0.05)
        assert output["data"]["transmission_ua_w_per_k"] == pytest.approx(500.0)
        assert output["asset_id"] == asset_id
        assert output["site_id"] == context.site_id
        assert any(
            item.get("tool") == "engineering.calculate_heat_loss" for item in output["provenance"]
        )
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_battery_advisory_is_feasible_and_keeps_constraints_visible(tmp_path: Path) -> None:
    built = build_engineering_environment(
        "dev_optimization_advisory", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        asset_id = "dublin-home-battery-model"
        metadata = built.agent.assets[asset_id].metadata["engineering"]
        assert metadata["assumptions"]
        assert "recommendation" in metadata["assumptions"][0]
        arguments = metadata["arguments"]
        session = built.agent.session(context.user_id, context.site_id)
        result = await built.agent.resolver.execute(
            session,
            CapabilityRequest(capability="plan_battery_charging", asset_id=asset_id),
        )
        assert result["ok"] is True
        output = result["result"]
        assert output["kind"] == DataKind.SIMULATED.value
        assert output["asset_id"] == asset_id
        assert output["site_id"] == context.site_id
        assert output["data"]["schedule"]
        battery = arguments["battery"]
        previous_soc = battery["initial_soc_kwh"]
        for interval in output["data"]["schedule"]:
            assert 0 <= interval["soc_kwh"] <= battery["capacity_kwh"]
            assert interval["charge_kw"] >= 0
            assert interval["discharge_kw"] >= 0
            assert not (interval["charge_kw"] > 1e-7 and interval["discharge_kw"] > 1e-7)
            assert interval["soc_kwh"] >= 0
            previous_soc = interval["soc_kwh"]
        assert previous_soc == pytest.approx(battery["target_final_soc_kwh"], abs=1e-6)
        assert output["data"]["summary"]["cost_savings"] > 0
        assert any(
            item.get("tool") == "engineering.schedule_battery_charging"
            for item in output["provenance"]
        )
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_network_asset_selection_runs_pypsa_and_preserves_lineage(tmp_path: Path) -> None:
    built = build_engineering_environment(
        "dev_network_asset_scope", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        session = built.agent.session(context.user_id, context.site_id)
        assert set(built.agent.assets) == {
            "new-york-school-network-east",
            "new-york-school-network-west",
        }
        for asset_id, line_id in (
            ("new-york-school-network-east", "east-feeder"),
            ("new-york-school-network-west", "west-feeder"),
        ):
            assert (
                "no physical line is changed"
                in built.agent.assets[asset_id].metadata["engineering"]["assumptions"]
            )
            resolution = built.agent.resolver.resolve(
                session,
                CapabilityRequest(capability="run_power_flow", asset_id=asset_id),
            )
            assert resolution["status"] == "resolved"
            assert resolution["selected"]["tool"] == "pypsa.power_flow"
            assert resolution["selected"]["asset_id"] == asset_id
            result = await built.agent.resolver.execute(
                session,
                CapabilityRequest(capability="run_power_flow", asset_id=asset_id),
            )
            assert result["ok"] is True
            output = result["result"]
            assert output["kind"] == DataKind.SIMULATED.value
            assert output["asset_id"] == asset_id
            assert output["site_id"] == context.site_id
            assert output["data"]["converged"] is True
            assert output["data"]["lines"][0]["id"] == line_id
            assert output["data"]["totals"]["balance_error_mw"] == pytest.approx(0.0, abs=1e-6)
            assert any(item.get("library") == "pypsa" for item in output["provenance"])
            assert any(item.get("tool") == "pypsa.power_flow" for item in output["provenance"])

        with pytest.raises(EnergyError) as forbidden:
            built.agent.resolver.resolve(
                session,
                CapabilityRequest(capability="run_power_flow", asset_id="missing-network"),
            )
        assert forbidden.value.code == "asset_forbidden"
    finally:
        await built.close()
