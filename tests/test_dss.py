from __future__ import annotations

import importlib.util

import pytest
from jsonschema import Draft202012Validator

from energy_agent_tools.connectors.dss import _schema, power_flow, register
from energy_agent_tools.models import DataKind, EnergyError
from energy_agent_tools.registry import Registry

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("opendssdirect") is None,
    reason="OpenDSSDirect.py optional dependency is not installed",
)


def _case(mode: str = "balanced") -> dict[str, object]:
    load: dict[str, object] = {
        "id": "house",
        "bus": "load",
        "phases": 3,
        "connection": "wye",
        "kv": 11.0,
    }
    if mode == "balanced":
        load.update({"kw": 1_000.0, "kvar": 200.0})
    else:
        load.update({"kw_by_phase": [500.0, 300.0, 100.0], "kvar_by_phase": [100.0, 60.0, 20.0]})
    return {
        "network": {
            "mode": mode,
            "frequency_hz": 50.0,
            "buses": [
                {"id": "grid", "kv_ll": 11.0, "phases": 3},
                {"id": "load", "kv_ll": 11.0, "phases": 3},
            ],
            "source": {"bus": "grid", "kv_ll": 11.0, "phases": 3, "pu": 1.0},
            "lines": [
                {
                    "id": "feeder",
                    "from_bus": "grid",
                    "to_bus": "load",
                    "phases": 3,
                    "length_km": 1.0,
                    "r1_ohm_per_km": 0.2,
                    "x1_ohm_per_km": 0.4,
                    "r0_ohm_per_km": 0.6,
                    "x0_ohm_per_km": 1.2,
                    "ampacity_a": 100.0,
                }
            ],
            "loads": [load],
        }
    }


def test_registers_reviewed_simulation_contract() -> None:
    registry = Registry()
    register(registry)

    assert registry.toolkits["opendss"].docs_url == "https://dss-extensions.org/OpenDSSDirect.py/"
    assert registry.tools["opendss.power_flow"].result_kind is DataKind.SIMULATED
    assert registry.tools["opendss.power_flow"].dependencies == ["opendssdirect"]
    Draft202012Validator(_schema()).validate(_case())


@pytest.mark.asyncio
async def test_balanced_case_solves_with_real_dss_engine() -> None:
    result = await power_flow(_case(), None)  # type: ignore[arg-type]

    assert result.source == "opendssdirect"
    assert result.kind is DataKind.SIMULATED
    assert result.data["converged"] is True
    assert result.data["frequency_hz"] == pytest.approx(50.0)
    assert result.provenance[0]["library"] == "opendssdirect.py"
    assert result.provenance[0]["version"]

    source = result.data["source"]
    assert source["kw"] == pytest.approx(1_001.7, abs=0.5)
    assert source["kvar"] == pytest.approx(202.7, abs=0.5)
    totals = result.data["totals"]
    assert totals["load_kw"] == pytest.approx(1_000.0)
    assert totals["loss_kw"] > 0
    assert totals["balance_error_kw"] == pytest.approx(0.0, abs=0.01)

    buses = {row["id"]: row for row in result.data["buses"]}
    assert buses["grid"]["v_mag_pu"] == pytest.approx(1.0, abs=0.002)
    assert buses["load"]["v_mag_pu"] < buses["grid"]["v_mag_pu"]
    assert len(buses["load"]["phases"]) == 3

    line = result.data["lines"][0]
    assert line["max_current_a"] == pytest.approx(53.65, abs=0.2)
    assert line["loading_percent"] < 100


@pytest.mark.asyncio
async def test_declared_sixty_hz_frequency_reaches_both_solver_interfaces() -> None:
    case = _case()
    case["network"]["frequency_hz"] = 60.0  # type: ignore[index]
    result = await power_flow(case, None)  # type: ignore[arg-type]

    assert result.data["frequency_hz"] == pytest.approx(60.0)
    assert result.data["source"]["kw"] == pytest.approx(1_001.7, abs=0.5)
    assert result.data["totals"]["balance_error_kw"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_unbalanced_case_preserves_per_phase_response() -> None:
    result = await power_flow(_case("unbalanced"), None)  # type: ignore[arg-type]

    assert result.data["mode"] == "unbalanced"
    line = result.data["lines"][0]
    currents = [phase["current_a"] for phase in line["phases"]]
    assert currents[0] > currents[1] > currents[2]
    bus = next(row for row in result.data["buses"] if row["id"] == "load")
    voltages = [phase["v_mag_pu"] for phase in bus["phases"]]
    assert max(voltages) - min(voltages) > 0.001
    assert result.data["totals"]["balance_error_kw"] == pytest.approx(0.0, abs=0.02)


@pytest.mark.asyncio
async def test_rejects_untrusted_dss_text_and_invalid_topology() -> None:
    case = _case()
    case["network"]["command"] = "redirect /tmp/attacker.dss"  # type: ignore[index]
    with pytest.raises(EnergyError) as unknown:
        await power_flow(case, None)  # type: ignore[arg-type]
    assert unknown.value.code == "invalid_input"

    bad = _case()
    bad["network"]["lines"][0]["from_bus"] = "missing"  # type: ignore[index]
    with pytest.raises(EnergyError) as topology:
        await power_flow(bad, None)  # type: ignore[arg-type]
    assert topology.value.code == "invalid_network"


@pytest.mark.asyncio
async def test_rejects_unbalanced_phase_array_with_wrong_length() -> None:
    case = _case("unbalanced")
    case["network"]["loads"][0]["kw_by_phase"] = [500.0, 300.0]  # type: ignore[index]
    with pytest.raises(EnergyError) as exc:
        await power_flow(case, None)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_input"
