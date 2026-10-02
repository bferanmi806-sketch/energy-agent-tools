"""Qualified local engineering environments for the development corpus.

The engineering cases in :mod:`benchmarks.scenarios` are deliberately kept as
frozen natural-language and scoring contracts.  This module supplies the
small set whose inputs can be exposed as reviewed site assets and executed by
the production :class:`~energy_agent_tools.runtime.EnergyAgent` runtime.

Two nearby scenarios remain intentionally unavailable.  The current PyPSA
adapter is a power-flow solver, not a dispatch optimizer, and the durable job
API has no PyPSA operation with an enforceable per-job timeout.  Raising
``EnvironmentUnavailable`` for those cases keeps the benchmark honest until
the corresponding contracts exist.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.connectors import engineering, networks
from energy_agent_tools.models import Asset, DataKind, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent

from .environments import BuiltEnvironment, EnvironmentUnavailable, ScenarioContext
from .scenarios import ScenarioCase, scenario_cases

_USER_ID = "engineering-qualification-user"
_SCENARIO_DATE = date(2026, 9, 29)
_SCENARIO_CLOCK = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

# The values are intentionally public to make the fixed-clock contract
# inspectable by the benchmark harness and by qualification tests.
SCENARIO_CLOCKS: dict[str, datetime] = {
    "dev_power_flow_two_bus": _SCENARIO_CLOCK,
    "dev_pypsa_capacity_constraint": _SCENARIO_CLOCK,
    "dev_heat_loss": _SCENARIO_CLOCK,
    "dev_bounded_simulation": _SCENARIO_CLOCK,
    "dev_optimization_advisory": _SCENARIO_CLOCK,
    "dev_network_asset_scope": _SCENARIO_CLOCK,
}

# These four cases have a complete source -> production execution -> evidence
# path.  The two excluded cases are documented below and fail closed in the
# builder instead of being represented by a weaker substitute.
QUALIFIED_ENGINEERING_CASE_IDS: frozenset[str] = frozenset(
    {
        "dev_power_flow_two_bus",
        "dev_heat_loss",
        "dev_optimization_advisory",
        "dev_network_asset_scope",
    }
)

ENGINEERING_CASE_EXCLUSIONS: dict[str, str] = {
    "dev_pypsa_capacity_constraint": (
        "excluded: the production PyPSA adapter solves steady-state AC power flow; "
        "it does not optimize dispatch or compare capacity-constrained dispatch "
        "as required by this prompt"
    ),
    "dev_bounded_simulation": (
        "excluded: the production job API exposes pandapower power_flow only and "
        "has no PyPSA network operation with an enforceable 30-second timeout"
    ),
}


def _case_index() -> dict[str, ScenarioCase]:
    return {case.id: case for case in scenario_cases()}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _context(case: ScenarioCase, root: Path, state_dir: Path) -> ScenarioContext:
    zone = ZoneInfo(case.timezone)
    local_start = datetime.combine(_SCENARIO_DATE, datetime.min.time(), tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return ScenarioContext(
        case_id=case.id,
        user_id=_USER_ID,
        site_id=case.site,
        provider=case.provider,
        timezone=case.timezone,
        scenario_date=_SCENARIO_DATE,
        scenario_clock=SCENARIO_CLOCKS[case.id],
        window_start=local_start.astimezone(UTC),
        window_end=local_end.astimezone(UTC),
        root=root,
        state_dir=state_dir,
    )


def _write_metadata(case: ScenarioCase, context: ScenarioContext) -> None:
    context.root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "case_id": case.id,
        "provider": case.provider,
        "site_id": context.site_id,
        "timezone": context.timezone,
        "scenario_date": context.scenario_date.isoformat(),
        "scenario_clock": _iso(context.scenario_clock),
        "window_start": _iso(context.window_start),
        "window_end": _iso(context.window_end),
        "qualification": "engineering-production-runtime",
    }
    (context.root / "environment.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _power_flow_args() -> dict[str, Any]:
    return {
        "network": {
            "buses": [
                {"id": "grid", "vn_kv": 11.0},
                {"id": "office", "vn_kv": 11.0},
            ],
            "lines": [
                {
                    "id": "office-feeder",
                    "from_bus": "grid",
                    "to_bus": "office",
                    "length_km": 2.0,
                    "r_ohm_per_km": 0.2,
                    "x_ohm_per_km": 0.4,
                    "c_nf_per_km": 0.0,
                    "max_i_ka": 1.0,
                    "max_loading_percent": 100.0,
                }
            ],
            "loads": [
                {"id": "office-load", "bus": "office", "p_mw": 0.2, "q_mvar": 0.04}
            ],
            "ext_grid": [{"id": "utility", "bus": "grid", "vm_pu": 1.0}],
        }
    }


def _pypsa_args(line_capacity_mva: float, *, line_id: str) -> dict[str, Any]:
    return {
        "network": {
            "buses": [
                {"id": "grid", "v_nom_kv": 11.0},
                {"id": "load", "v_nom_kv": 11.0},
            ],
            "lines": [
                {
                    "id": line_id,
                    "from_bus": "grid",
                    "to_bus": "load",
                    "r_ohm": 0.2,
                    "x_ohm": 0.4,
                    "s_nom_mva": line_capacity_mva,
                }
            ],
            "loads": [{"id": "site-load", "bus": "load", "p_mw": 0.5, "q_mvar": 0.1}],
            "slack_bus": "grid",
        }
    }


def _heat_loss_args() -> dict[str, Any]:
    # Independent truth: UA = 500 W/K and delta-T = 24 K, so gross loss is
    # exactly 12 kW.  Every input is visible in the asset metadata and passed
    # unchanged to the production calculator.
    return {
        "components": [
            {"name": "walls", "area_m2": 400.0, "u_value_w_m2k": 0.5},
            {"name": "roof", "area_m2": 200.0, "u_value_w_m2k": 0.5},
            {"name": "windows", "area_m2": 40.0, "u_value_w_m2k": 2.0},
            {"name": "floor", "area_m2": 400.0, "u_value_w_m2k": 0.3},
        ],
        "indoor_temp_c": 20.0,
        "outdoor_temp_c": -4.0,
        "duration_hours": 1.0,
    }


def _battery_args(context: ScenarioContext) -> dict[str, Any]:
    start = context.scenario_clock.replace(minute=0, second=0, microsecond=0)
    return {
        "intervals": [
            {
                "timestamp": _iso(start + timedelta(hours=index)),
                "duration_hours": 1.0,
                "load_kw": load,
                "pv_kw": pv,
                "price_per_kwh": price,
                "carbon_intensity_g_per_kwh": carbon,
            }
            for index, (load, pv, price, carbon) in enumerate(
                (
                    (0.8, 0.0, 0.10, 250.0),
                    (0.8, 0.0, 0.40, 220.0),
                    (0.8, 0.0, 0.45, 200.0),
                    (0.8, 0.0, 0.12, 80.0),
                )
            )
        ],
        "battery": {
            "capacity_kwh": 2.0,
            "initial_soc_kwh": 0.0,
            "max_charge_kw": 1.0,
            "max_discharge_kw": 1.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "target_final_soc_kwh": 0.0,
        },
        "objective": "cost_and_carbon",
        "carbon_price_gbp_per_tonne": 100.0,
        "carbon_weight": 1.0,
        "timezone": context.timezone,
    }


def _site(case: ScenarioCase, context: ScenarioContext) -> Site:
    coordinates = {
        "manchester-office": (53.4808, -2.2426),
        "dublin-home": (53.3498, -6.2603),
        "new-york-school": (40.7128, -74.0060),
    }
    latitude, longitude = coordinates.get(case.site, (0.0, 0.0))
    return Site(
        id=context.site_id,
        user_id=context.user_id,
        name=case.site.replace("-", " ").title(),
        timezone=context.timezone,
        latitude=latitude,
        longitude=longitude,
    )


def _engineering_asset(
    *,
    asset_id: str,
    site_id: str,
    name: str,
    model_tool: str,
    arguments: dict[str, Any],
    assumptions: list[str],
) -> Asset:
    return Asset(
        id=asset_id,
        site_id=site_id,
        kind="engineering-model",
        name=name,
        metadata={
            "engineering": {
                "tool": model_tool,
                "arguments": arguments,
                "assumptions": assumptions,
                "control_mode": "simulation-only",
            }
        },
    )


def _binding(
    *,
    capability: str,
    tool: str,
    asset_id: str,
    arguments: dict[str, Any],
    kind: DataKind,
    unit: str,
    preference: int = 100,
) -> CapabilityBinding:
    return CapabilityBinding(
        capability=capability,
        tool=tool,
        asset_id=asset_id,
        kind=kind,
        unit=unit,
        quality="qualified-production-runtime",
        preference=preference,
        defaults=arguments,
        reviewed=True,
    )


def _build_components(
    case: ScenarioCase,
    context: ScenarioContext,
) -> tuple[list[Asset], list[CapabilityBinding]]:
    if case.id == "dev_power_flow_two_bus":
        asset_id = "manchester-office-two-bus"
        arguments = _power_flow_args()
        asset = _engineering_asset(
            asset_id=asset_id,
            site_id=context.site_id,
            name="Manchester office two-bus feeder model",
            model_tool="engineering.run_power_flow",
            arguments=arguments,
            assumptions=[
                "balanced three-phase AC Newton-Raphson study",
                "no equipment is energized; the model is simulation-only",
            ],
        )
        binding = _binding(
            capability="run_power_flow",
            tool="engineering.run_power_flow",
            asset_id=asset_id,
            arguments=arguments,
            kind=DataKind.SIMULATED,
            unit="MW, Mvar, pu",
        )
        return [asset], [binding]

    if case.id == "dev_heat_loss":
        asset_id = "manchester-office-envelope"
        arguments = _heat_loss_args()
        asset = _engineering_asset(
            asset_id=asset_id,
            site_id=context.site_id,
            name="Manchester office envelope model",
            model_tool="engineering.calculate_heat_loss",
            arguments=arguments,
            assumptions=["UA equals 500 W/K", "indoor minus outdoor temperature is 24 K"],
        )
        binding = _binding(
            capability="calculate_heat_loss",
            tool="engineering.calculate_heat_loss",
            asset_id=asset_id,
            arguments=arguments,
            kind=DataKind.CALCULATED,
            unit="kW_th and kWh_th",
        )
        return [asset], [binding]

    if case.id == "dev_optimization_advisory":
        asset_id = "dublin-home-battery-model"
        arguments = _battery_args(context)
        asset = _engineering_asset(
            asset_id=asset_id,
            site_id=context.site_id,
            name="Dublin home battery advisory model",
            model_tool="engineering.schedule_battery_charging",
            arguments=arguments,
            assumptions=[
                "schedule is a model recommendation, not a control command",
                "tariff and carbon values are caller-supplied planning inputs",
            ],
        )
        binding = _binding(
            capability="plan_battery_charging",
            tool="engineering.schedule_battery_charging",
            asset_id=asset_id,
            arguments=arguments,
            kind=DataKind.SIMULATED,
            unit="kW, kWh, currency, gCO2e",
        )
        return [asset], [binding]

    if case.id == "dev_network_asset_scope":
        assets: list[Asset] = []
        bindings: list[CapabilityBinding] = []
        for asset_id, capacity, line_id in (
            ("new-york-school-network-east", 1.0, "east-feeder"),
            ("new-york-school-network-west", 2.0, "west-feeder"),
        ):
            arguments = _pypsa_args(capacity, line_id=line_id)
            assets.append(
                _engineering_asset(
                    asset_id=asset_id,
                    site_id=context.site_id,
                    name=f"New York school {line_id} PyPSA model",
                    model_tool="pypsa.power_flow",
                    arguments=arguments,
                    assumptions=[
                        "PyPSA non-linear AC power flow",
                        f"line capacity is {capacity:g} MVA in the selected model",
                        "no physical line is changed",
                    ],
                )
            )
            bindings.append(
                _binding(
                    capability="run_power_flow",
                    tool="pypsa.power_flow",
                    asset_id=asset_id,
                    arguments=arguments,
                    kind=DataKind.SIMULATED,
                    unit="MW, Mvar, pu, degree",
                )
            )
        return assets, bindings

    raise EnvironmentUnavailable(f"Scenario {case.id!r} is not an engineering environment.")


def _unreachable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"message": "No network connector is configured."}, request=request)


def build_engineering_environment(case_id: str, root: Path, state: Path) -> BuiltEnvironment:
    """Build one qualified engineering environment or fail closed.

    The returned object uses the shared benchmark ``BuiltEnvironment`` and
    ``ScenarioContext`` classes, so parent harnesses can handle engineering and
    provider environments through the same lifecycle API.
    """

    case = _case_index().get(case_id)
    if case is None:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} is unknown.")
    if case_id in ENGINEERING_CASE_EXCLUSIONS:
        raise EnvironmentUnavailable(ENGINEERING_CASE_EXCLUSIONS[case_id])
    if case_id not in QUALIFIED_ENGINEERING_CASE_IDS:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} has no qualified engineering environment.")
    if case.split != "development" or case.fixture_case_id is not None:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} is not a development environment.")

    case_root = (Path(root).resolve() / case_id).resolve()
    case_state = (Path(state).resolve() / case_id).resolve()
    case_root.mkdir(parents=True, exist_ok=True)
    case_state.mkdir(parents=True, exist_ok=True)
    context = _context(case, case_root, case_state)
    _write_metadata(case, context)
    assets, bindings = _build_components(case, context)

    registry = Registry()
    engineering.register(registry)
    networks.register(registry)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_unreachable),
        follow_redirects=False,
        timeout=5,
    )
    agent = EnergyAgent(
        registry,
        case_state,
        sites=[_site(case, context)],
        assets=assets,
        http=client,
        bindings=bindings,
        calendar_clock=lambda: context.scenario_clock,
    )
    return BuiltEnvironment(
        agent=agent,
        context=replace(context, account_ids=()),
        _http=client,
    )


__all__ = [
    "ENGINEERING_CASE_EXCLUSIONS",
    "QUALIFIED_ENGINEERING_CASE_IDS",
    "SCENARIO_CLOCKS",
    "build_engineering_environment",
]
