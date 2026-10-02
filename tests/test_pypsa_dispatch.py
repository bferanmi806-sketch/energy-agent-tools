from __future__ import annotations

import builtins
import importlib.util

import pytest
from jsonschema import Draft202012Validator

from energy_agent_tools.connectors import networks, pypsa_dispatch
from energy_agent_tools.models import Action, DataKind
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def _registry() -> Registry:
    registry = Registry()
    networks.register(registry)
    pypsa_dispatch.register(registry)
    return registry


def _agent(tmp_path) -> EnergyAgent:
    return EnergyAgent(_registry(), tmp_path)


def _case(
    *, line_capacity_mva: float = 1.0, load_mw: float = 2.0, remote_capacity_mw: float = 10.0
) -> dict[str, object]:
    return {
        "network": {
            "buses": [
                {"id": "remote", "v_nom_kv": 11.0},
                {"id": "load", "v_nom_kv": 11.0},
            ],
            "lines": [
                {
                    "id": "feeder",
                    "from_bus": "remote",
                    "to_bus": "load",
                    "x_ohm": 0.4,
                    "s_nom_mva": line_capacity_mva,
                }
            ],
            "loads": [{"id": "demand", "bus": "load", "p_mw": load_mw}],
            "generators": [
                {
                    "id": "remote-cheap",
                    "bus": "remote",
                    "p_nom_mw": remote_capacity_mw,
                    "marginal_cost": 10.0,
                },
                {
                    "id": "local-dear",
                    "bus": "load",
                    "p_nom_mw": 10.0,
                    "marginal_cost": 50.0,
                },
            ],
        }
    }


def _result(response: dict[str, object]) -> dict[str, object]:
    assert response["ok"], response
    return response["result"]  # type: ignore[return-value]


def test_registers_dispatch_under_the_existing_pypsa_toolkit() -> None:
    registry = _registry()
    tool = registry.tools["pypsa.optimize_dispatch"]

    assert "pypsa" in registry.toolkits
    assert tool.toolkit == "pypsa"
    assert tool.actions == {Action.SIMULATE}
    assert tool.result_kind is DataKind.SIMULATED
    assert tool.dependencies == ["pypsa", "highspy"]
    assert {"optimize_dispatch", "run_network_optimization"} <= set(tool.capabilities)
    Draft202012Validator(tool.input_schema).validate(_case())


@pytest.mark.asyncio
@pytest.mark.skipif(
    importlib.util.find_spec("pypsa") is None or importlib.util.find_spec("highspy") is None,
    reason="PyPSA dispatch extra is not installed",
)
async def test_real_dispatch_respects_a_congested_line_and_uses_pypsa(tmp_path) -> None:
    agent = _agent(tmp_path)
    session = agent.session("operator")
    try:
        constrained = _result(
            await agent.execute(session, "pypsa.optimize_dispatch", _case(line_capacity_mva=1.0))
        )
        unconstrained = _result(
            await agent.execute(session, "pypsa.optimize_dispatch", _case(line_capacity_mva=2.0))
        )
    finally:
        await agent.close()

    constrained_data = constrained["data"]
    unconstrained_data = unconstrained["data"]
    assert constrained["kind"] == "simulated"
    assert constrained["source"] == "pypsa"
    assert constrained_data["status"] == "optimal"
    assert "lossless" in constrained_data["model"]
    constrained_dispatch = {row["id"]: row["dispatch_mw"] for row in constrained_data["generators"]}
    unconstrained_dispatch = {
        row["id"]: row["dispatch_mw"] for row in unconstrained_data["generators"]
    }
    assert constrained_dispatch["remote-cheap"] == pytest.approx(1.0, abs=1e-7)
    assert constrained_dispatch["local-dear"] == pytest.approx(1.0, abs=1e-7)
    assert constrained_data["lines"][0]["flow_mw"] == pytest.approx(1.0, abs=1e-7)
    assert constrained_data["lines"][0]["capacity_mw"] == pytest.approx(1.0)
    assert constrained_data["lines"][0]["utilization_percent"] == pytest.approx(100.0)
    assert constrained_data["objective_currency_per_hour"] == pytest.approx(60.0, abs=1e-7)
    assert constrained_data["load_balance_residual_mw"] == pytest.approx(0.0, abs=1e-7)
    assert unconstrained_dispatch["remote-cheap"] == pytest.approx(2.0, abs=1e-7)
    assert unconstrained_dispatch["local-dear"] == pytest.approx(0.0, abs=1e-7)
    assert unconstrained_data["objective_currency_per_hour"] == pytest.approx(20.0, abs=1e-7)
    assert any("AC voltage magnitudes" in item for item in constrained["assumptions"])
    libraries = {item["library"] for item in constrained["provenance"] if "library" in item}
    assert libraries >= {"pypsa", "HiGHS"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    importlib.util.find_spec("pypsa") is None or importlib.util.find_spec("highspy") is None,
    reason="PyPSA dispatch extra is not installed",
)
async def test_infeasible_network_returns_an_error_without_dispatch_data(tmp_path) -> None:
    agent = _agent(tmp_path)
    case = _case(load_mw=11.0, remote_capacity_mw=1.0)
    case["network"]["generators"][1]["p_nom_mw"] = 1.0  # type: ignore[index]
    try:
        response = await agent.execute(agent.session("operator"), "pypsa.optimize_dispatch", case)
    finally:
        await agent.close()

    assert response["ok"] is False
    assert response["error"]["code"] == "infeasible_network"
    assert "result" not in response


@pytest.mark.asyncio
@pytest.mark.skipif(
    importlib.util.find_spec("pypsa") is None or importlib.util.find_spec("highspy") is None,
    reason="PyPSA dispatch extra is not installed",
)
async def test_nonoptimal_solver_status_is_reported_as_nonconvergence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(pypsa_dispatch, "_solve", lambda _network: ("warning", "time_limit"))
    agent = _agent(tmp_path)
    try:
        response = await agent.execute(
            agent.session("operator"), "pypsa.optimize_dispatch", _case()
        )
    finally:
        await agent.close()

    assert response["ok"] is False
    assert response["error"]["code"] == "optimization_not_converged"
    assert "result" not in response


@pytest.mark.asyncio
async def test_base_install_discovers_tool_and_reports_missing_solver_dependency(
    tmp_path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def without_pypsa(name, *args, **kwargs):
        if name == "pypsa":
            raise ModuleNotFoundError("optional PyPSA dependency missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pypsa)
    agent = _agent(tmp_path)
    try:
        response = await agent.execute(
            agent.session("operator"), "pypsa.optimize_dispatch", _case()
        )
    finally:
        await agent.close()

    assert "pypsa.optimize_dispatch" in agent.registry.tools
    assert response["ok"] is False
    assert response["error"]["code"] == "dependency_unavailable"


@pytest.mark.asyncio
async def test_handler_rejects_boolean_nan_bad_topology_and_engineering_bounds(tmp_path) -> None:
    agent = _agent(tmp_path)
    cases = []

    boolean = _case()
    boolean["network"]["buses"][0]["v_nom_kv"] = True  # type: ignore[index]
    cases.append(boolean)

    not_finite = _case()
    not_finite["network"]["lines"][0]["x_ohm"] = float("nan")  # type: ignore[index]
    cases.append(not_finite)

    duplicate = _case()
    duplicate["network"]["loads"][0]["id"] = "feeder"  # type: ignore[index]
    cases.append(duplicate)

    unknown_bus = _case()
    unknown_bus["network"]["generators"][0]["bus"] = "absent"  # type: ignore[index]
    cases.append(unknown_bus)

    too_large = _case()
    too_large["network"]["generators"][0]["p_nom_mw"] = 10_001.0  # type: ignore[index]
    cases.append(too_large)

    try:
        responses = [
            await agent.execute(agent.session("operator"), "pypsa.optimize_dispatch", case)
            for case in cases
        ]
    finally:
        await agent.close()

    assert all(response["ok"] is False for response in responses)
    assert all("result" not in response for response in responses)
