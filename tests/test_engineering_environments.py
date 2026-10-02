from __future__ import annotations

import asyncio
import copy
import json
import stat
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from benchmarks.engineering_environments import (
    ENGINEERING_CASE_EXCLUSIONS,
    QUALIFIED_ENGINEERING_CASE_IDS,
    SCENARIO_CLOCKS,
    EnvironmentUnavailable,
    build_engineering_environment,
)
from benchmarks.scenarios import scenario_cases
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.jobs import JobManager
from energy_agent_tools.models import Action, DataKind, EnergyError

CASE_INDEX = {case.id: case for case in scenario_cases()}


def test_engineering_qualification_set_is_explicit() -> None:
    assert QUALIFIED_ENGINEERING_CASE_IDS == {
        "dev_power_flow_two_bus",
        "dev_pypsa_capacity_constraint",
        "dev_heat_loss",
        "dev_bounded_simulation",
        "dev_optimization_advisory",
        "dev_network_asset_scope",
    }
    assert ENGINEERING_CASE_EXCLUSIONS == {}
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
async def test_pypsa_capacity_assets_run_actual_one_hour_dispatch_comparison(
    tmp_path: Path,
) -> None:
    built = build_engineering_environment(
        "dev_pypsa_capacity_constraint", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        assert set(built.agent.assets) == {
            "dublin-home-capacity-1mw",
            "dublin-home-capacity-2mw",
        }
        tool = built.agent.registry.tools["pypsa.optimize_dispatch"]
        validator = Draft202012Validator(tool.input_schema)
        session = built.agent.session(context.user_id, context.site_id)
        results = {}
        for asset_id, capacity in (
            ("dublin-home-capacity-1mw", 1.0),
            ("dublin-home-capacity-2mw", 2.0),
        ):
            metadata = built.agent.assets[asset_id].metadata["engineering"]
            arguments = metadata["arguments"]
            validator.validate(arguments)
            assert metadata["tool"] == "pypsa.optimize_dispatch"
            assert metadata["control_mode"] == "simulation-only"
            assert (
                "one fixed snapshot represents a one-hour operating interval"
                in metadata["assumptions"]
            )
            assert arguments["network"]["lines"][0]["s_nom_mva"] == capacity

            resolution = built.agent.resolver.resolve(
                session,
                CapabilityRequest(capability="run_network_optimization", asset_id=asset_id),
            )
            assert resolution["status"] == "resolved"
            assert resolution["selected"]["tool"] == "pypsa.optimize_dispatch"
            assert resolution["selected"]["asset_id"] == asset_id
            response = await built.agent.resolver.execute(
                session,
                CapabilityRequest(capability="run_network_optimization", asset_id=asset_id),
            )
            assert response["ok"] is True, response
            result = response["result"]
            results[capacity] = result
            assert result["kind"] == DataKind.SIMULATED.value
            assert result["asset_id"] == asset_id
            assert result["site_id"] == context.site_id
            assert result["data"]["status"] == "optimal"
            assert result["data"]["snapshot_hours"] == 1.0
            assert result["data"]["load_balance_residual_mw"] == pytest.approx(0.0, abs=1e-7)
            assert any(item.get("library") == "pypsa" for item in result["provenance"])
            assert any(item.get("library") == "HiGHS" for item in result["provenance"])

        constrained = results[1.0]["data"]
        unconstrained = results[2.0]["data"]
        constrained_dispatch = {row["id"]: row["dispatch_mw"] for row in constrained["generators"]}
        unconstrained_dispatch = {
            row["id"]: row["dispatch_mw"] for row in unconstrained["generators"]
        }
        assert constrained_dispatch == {
            "remote-cheap": pytest.approx(1.0, abs=1e-7),
            "local-dear": pytest.approx(1.0, abs=1e-7),
        }
        assert unconstrained_dispatch == {
            "remote-cheap": pytest.approx(2.0, abs=1e-7),
            "local-dear": pytest.approx(0.0, abs=1e-7),
        }
        assert constrained["lines"][0]["capacity_mw"] == pytest.approx(1.0)
        assert constrained["objective_currency_per_hour"] == pytest.approx(60.0, abs=1e-7)
        assert unconstrained["lines"][0]["capacity_mw"] == pytest.approx(2.0)
        assert unconstrained["objective_currency_per_hour"] == pytest.approx(20.0, abs=1e-7)
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_native_dispatch_schema_is_strict_and_enforces_size_bounds(
    tmp_path: Path,
) -> None:
    built = build_engineering_environment(
        "dev_pypsa_capacity_constraint", tmp_path / "root", tmp_path / "state"
    )
    try:
        tool = built.agent.registry.tools["pypsa.optimize_dispatch"]
        schema = tool.input_schema
        network_schema = schema["properties"]["network"]
        assert schema["additionalProperties"] is False
        assert network_schema["additionalProperties"] is False
        assert network_schema["properties"]["buses"]["maxItems"] == 100
        assert network_schema["properties"]["lines"]["maxItems"] == 500
        assert network_schema["properties"]["loads"]["maxItems"] == 500
        assert network_schema["properties"]["generators"]["maxItems"] == 500

        valid = built.agent.assets["dublin-home-capacity-1mw"].metadata["engineering"]["arguments"]
        validator = Draft202012Validator(schema)
        validator.validate(valid)
        with pytest.raises(ValidationError):
            validator.validate({**valid, "extra": True})
        too_many_buses = copy.deepcopy(valid)
        too_many_buses["network"]["buses"] = [
            {"id": f"bus-{index}", "v_nom_kv": 11.0} for index in range(101)
        ]
        with pytest.raises(ValidationError):
            validator.validate(too_many_buses)

        too_many_elements = copy.deepcopy(valid)
        too_many_elements["network"]["loads"] = [
            {"id": f"load-{index}", "bus": "load", "p_mw": 0.0} for index in range(251)
        ]
        too_many_elements["network"]["generators"] = [
            {
                "id": f"generator-{index}",
                "bus": "load",
                "p_nom_mw": 1.0,
                "marginal_cost": 1.0,
            }
            for index in range(250)
        ]
        validator.validate(too_many_elements)
        response = await built.agent.execute(
            built.agent.session(built.context.user_id, built.context.site_id),
            "pypsa.optimize_dispatch",
            too_many_elements,
        )
        assert response["ok"] is False
        assert response["error"]["code"] == "input_too_large"
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_bounded_pypsa_job_enforces_runtime_bounds_scope_and_cleanup(
    tmp_path: Path,
) -> None:
    built = build_engineering_environment(
        "dev_bounded_simulation", tmp_path / "root", tmp_path / "state"
    )
    try:
        context = built.context
        asset_id = "cape-town-house-bounded-network"
        metadata = built.agent.assets[asset_id].metadata["engineering"]
        execution = metadata["execution"]
        assert metadata["tool"] == "pypsa.power_flow"
        assert execution == {
            "job_operation": "network_power_flow",
            "timeout_seconds": 30,
            "schema_max_buses": 100,
        }
        assert metadata["control_mode"] == "simulation-only"
        arguments = metadata["arguments"]
        power_flow_tool = built.agent.registry.tools["pypsa.power_flow"]
        validator = Draft202012Validator(power_flow_tool.input_schema)
        validator.validate(arguments)
        assert (
            power_flow_tool.input_schema["properties"]["network"]["properties"]["buses"]["maxItems"]
            == 100
        )
        assert power_flow_tool.input_schema["additionalProperties"] is False
        assert len(arguments["network"]["buses"]) <= execution["schema_max_buses"]

        session = built.agent.session(context.user_id, context.site_id)
        denied = built.agent.session(
            context.user_id,
            context.site_id,
            allowed_actions={Action.READ},
        )
        denied_submit = await built.agent.job(
            denied,
            "submit",
            simulation="network_power_flow",
            arguments=arguments,
        )
        assert denied_submit["error"]["code"] == "policy_denied"

        job_store = context.state_dir / "jobs"
        started = time.monotonic()
        submitted = await built.agent.job(
            session,
            "submit",
            simulation=execution["job_operation"],
            arguments=arguments,
        )
        assert submitted["ok"] is True, submitted
        record = submitted["job"]
        job_id = record["job_id"]
        job_dir = job_store / job_id
        input_path = job_dir / "input.json"
        assert record["operation"] == "network_power_flow"
        assert record["user_id"] == context.user_id
        assert record["session_id"] == session.id
        assert record["site_id"] == context.site_id
        assert stat.S_IMODE(job_store.stat().st_mode) == 0o700
        assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((job_dir / "state").stat().st_mode) == 0o700
        assert stat.S_IMODE(input_path.stat().st_mode) == 0o600
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        assert payload["operation"] == "network_power_flow"
        assert payload["arguments"] == arguments

        wrong_user = await built.agent.job(
            built.agent.session("another-user"), "resume", job_id=job_id
        )
        assert wrong_user["error"]["code"] == "job_access_denied"
        wrong_site = await built.agent.job(
            built.agent.session(context.user_id, None, id=session.id), "result", job_id=job_id
        )
        assert wrong_site["error"]["code"] == "site_forbidden"
        limited_toolkit = built.agent.session(
            context.user_id,
            context.site_id,
            id=session.id,
            toolkits={"engineering"},
        )
        toolkit_denied = await built.agent.job(limited_toolkit, "result", job_id=job_id)
        assert toolkit_denied["error"]["code"] == "tool_forbidden"

        await asyncio.wait_for(built.agent._job_task, timeout=30)
        elapsed = time.monotonic() - started
        assert elapsed < 30.0
        manager = built.agent._jobs
        assert manager is not None
        assert manager.timeout_seconds == 30.0
        result_response = await built.agent.job(session, "result", job_id=job_id)
        assert result_response["ok"] is True, result_response
        result = result_response["result"]
        assert result["kind"] == DataKind.SIMULATED.value
        assert result["data"]["converged"] is True
        assert any(item.get("library") == "pypsa" for item in result["provenance"])
        assert any(item.get("tool") == "pypsa.power_flow" for item in result["provenance"])
        assert not result.get("asset_id")
        assert not result.get("site_id")
        assert not any(item.get("asset_id") == asset_id for item in result["provenance"])
        assert (await built.agent.job(session, "status", job_id=job_id))["job"][
            "status"
        ] == "completed"

        assert manager.cleanup("another-user", session.id, older_than_seconds=0) == 0
        assert job_dir.exists()
        assert manager.cleanup(context.user_id, session.id, older_than_seconds=0) == 1
        assert not job_dir.exists()
        await built.close()
        reopened = JobManager(job_store)
        reopened.close()
        assert reopened._closed
    finally:
        await built.close()


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
