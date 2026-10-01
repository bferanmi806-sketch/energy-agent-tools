from __future__ import annotations

import importlib.util

import pytest
from jsonschema import Draft202012Validator

from energy_agent_tools.connectors.networks import (
    pandapipes_pipeflow,
    pypsa_power_flow,
    register,
)
from energy_agent_tools.models import DataKind, EnergyError
from energy_agent_tools.registry import Registry


def _pypsa_case() -> dict[str, object]:
    return {
        "network": {
            "buses": [
                {"id": "grid", "v_nom_kv": 11.0},
                {"id": "load", "v_nom_kv": 11.0},
            ],
            "lines": [
                {
                    "id": "feeder",
                    "from_bus": "grid",
                    "to_bus": "load",
                    "r_ohm": 0.2,
                    "x_ohm": 0.4,
                    "s_nom_mva": 100.0,
                }
            ],
            "loads": [{"id": "house", "bus": "load", "p_mw": 1.0, "q_mvar": 0.2}],
            "slack_bus": "grid",
        }
    }


def _pandapipes_case(fluid: str = "water") -> dict[str, object]:
    return {
        "network": {
            "fluid": fluid,
            "junctions": [
                {"id": "source", "pn_bar": 5.0},
                {"id": "load", "pn_bar": 5.0},
            ],
            "pipes": [
                {
                    "id": "branch",
                    "from_junction": "source",
                    "to_junction": "load",
                    "length_km": 0.1,
                    "diameter_m": 0.1,
                }
            ],
            "sources": [{"id": "inlet", "junction": "source", "mdot_kg_per_s": 0.1}],
            "sinks": [{"id": "demand", "junction": "load", "mdot_kg_per_s": 0.1}],
            "reference_junction": "source",
            "reference_pressure_bar": 5.0,
        }
    }


def test_registers_explicit_optional_solver_contracts() -> None:
    registry = Registry()
    register(registry)

    assert {"pypsa", "pandapipes"}.issubset(registry.toolkits)
    assert registry.tools["pypsa.power_flow"].result_kind is DataKind.SIMULATED
    assert registry.tools["pypsa.power_flow"].dependencies == ["pypsa"]
    assert registry.tools["pandapipes.pipeflow"].result_kind is DataKind.SIMULATED
    assert registry.tools["pandapipes.pipeflow"].dependencies == ["pandapipes"]
    Draft202012Validator(registry.tools["pypsa.power_flow"].input_schema).validate(_pypsa_case())
    Draft202012Validator(registry.tools["pandapipes.pipeflow"].input_schema).validate(
        _pandapipes_case()
    )


@pytest.mark.asyncio
async def test_pypsa_solves_real_two_bus_ac_network() -> None:
    if importlib.util.find_spec("pypsa") is None:
        pytest.skip("pypsa optional dependency is not installed")

    result = await pypsa_power_flow(_pypsa_case(), None)  # type: ignore[arg-type]

    assert result.source == "pypsa"
    assert result.kind is DataKind.SIMULATED
    assert result.data["converged"] is True
    assert abs(result.data["totals"]["balance_error_mw"]) < 1e-6
    grid = next(row for row in result.data["buses"] if row["id"] == "grid")
    load = next(row for row in result.data["buses"] if row["id"] == "load")
    assert grid["v_mag_pu"] == pytest.approx(1.0, abs=1e-8)
    assert load["v_mag_pu"] < grid["v_mag_pu"]
    line = result.data["lines"][0]
    assert line["r_pu"] == pytest.approx(0.2 / (11.0**2))
    assert line["x_pu"] == pytest.approx(0.4 / (11.0**2))
    assert result.provenance[0]["library"] == "pypsa"
    assert result.provenance[0]["version"]


@pytest.mark.asyncio
async def test_pypsa_matches_pandapower_for_same_physical_two_bus_case() -> None:
    if importlib.util.find_spec("pypsa") is None:
        pytest.skip("pypsa optional dependency is not installed")
    if importlib.util.find_spec("pandapower") is None:
        pytest.skip("pandapower optional dependency is not installed")

    from energy_agent_tools.connectors.engineering import run_power_flow

    pandapower_case = {
        "network": {
            "buses": [
                {"id": "grid", "vn_kv": 11.0},
                {"id": "load", "vn_kv": 11.0},
            ],
            "lines": [
                {
                    "id": "feeder",
                    "from_bus": "grid",
                    "to_bus": "load",
                    "length_km": 1.0,
                    "r_ohm_per_km": 0.2,
                    "x_ohm_per_km": 0.4,
                    "c_nf_per_km": 0.0,
                    "max_i_ka": 1.0,
                }
            ],
            "loads": [{"id": "house", "bus": "load", "p_mw": 1.0, "q_mvar": 0.2}],
            "ext_grid": [{"id": "utility", "bus": "grid", "vm_pu": 1.0}],
        }
    }

    pypsa_result = await pypsa_power_flow(_pypsa_case(), None)  # type: ignore[arg-type]
    pandapower_result = await run_power_flow(pandapower_case, None)  # type: ignore[arg-type]
    py_load = next(row for row in pypsa_result.data["buses"] if row["id"] == "load")
    pp_load = next(row for row in pandapower_result.data["buses"] if row["id"] == "load")

    assert py_load["v_mag_pu"] == pytest.approx(pp_load["vm_pu"], rel=1e-5, abs=1e-8)
    assert py_load["v_ang_degree"] == pytest.approx(pp_load["va_degree"], abs=1e-4)
    assert pypsa_result.data["lines"][0]["loss_mw"] == pytest.approx(
        pandapower_result.data["lines"][0]["loss_mw"], rel=1e-5, abs=1e-9
    )


@pytest.mark.asyncio
async def test_pypsa_rejects_unknown_bus_before_solver() -> None:
    if importlib.util.find_spec("pypsa") is None:
        pytest.skip("pypsa optional dependency is not installed")
    case = _pypsa_case()
    case["network"]["loads"] = [{"id": "bad", "bus": "missing", "p_mw": 1.0}]

    with pytest.raises(EnergyError) as exc:
        await pypsa_power_flow(case, None)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_network"


@pytest.mark.asyncio
async def test_pandapipes_solves_real_water_pipeflow_and_mass_balance() -> None:
    if importlib.util.find_spec("pandapipes") is None:
        pytest.skip("pandapipes optional dependency is not installed")

    result = await pandapipes_pipeflow(_pandapipes_case(), None)  # type: ignore[arg-type]

    assert result.source == "pandapipes"
    assert result.kind is DataKind.SIMULATED
    assert result.data["converged"] is True
    assert result.data["fluid"] == "water"
    assert result.data["junctions"][1]["p_bar"] < result.data["junctions"][0]["p_bar"]
    assert result.data["pipes"][0]["mdot_from_kg_per_s"] == pytest.approx(0.1)
    assert result.data["pipes"][0]["mdot_to_kg_per_s"] == pytest.approx(-0.1)
    assert abs(result.data["totals"]["mass_balance_error_kg_per_s"]) < 1e-9
    assert result.provenance[0]["library"] == "pandapipes"


@pytest.mark.asyncio
async def test_pandapipes_supports_gas_preset_with_explicit_units() -> None:
    if importlib.util.find_spec("pandapipes") is None:
        pytest.skip("pandapipes optional dependency is not installed")

    result = await pandapipes_pipeflow(_pandapipes_case("gas"), None)  # type: ignore[arg-type]

    assert result.data["fluid"] == "lgas"
    assert result.data["converged"] is True
    assert result.unit == "bar, K, kg/s, m/s"


@pytest.mark.asyncio
async def test_pandapipes_exposes_external_grid_sign_in_mass_balance() -> None:
    if importlib.util.find_spec("pandapipes") is None:
        pytest.skip("pandapipes optional dependency is not installed")
    case = _pandapipes_case()
    case["network"]["sources"] = []

    result = await pandapipes_pipeflow(case, None)  # type: ignore[arg-type]

    assert result.data["totals"]["external_grid_mdot_kg_per_s"] == pytest.approx(-0.1)
    assert abs(result.data["totals"]["mass_balance_error_kg_per_s"]) < 1e-9


@pytest.mark.asyncio
async def test_pandapipes_rejects_unknown_junction_and_bad_diameter() -> None:
    if importlib.util.find_spec("pandapipes") is None:
        pytest.skip("pandapipes optional dependency is not installed")
    case = _pandapipes_case()
    case["network"]["pipes"][0]["to_junction"] = "missing"
    with pytest.raises(EnergyError) as unknown:
        await pandapipes_pipeflow(case, None)  # type: ignore[arg-type]
    assert unknown.value.code == "invalid_network"

    case = _pandapipes_case()
    case["network"]["pipes"][0]["diameter_m"] = 0
    with pytest.raises(EnergyError) as diameter:
        await pandapipes_pipeflow(case, None)  # type: ignore[arg-type]
    assert diameter.value.code == "invalid_input"
