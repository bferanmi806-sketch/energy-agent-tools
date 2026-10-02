"""Bounded PyPSA linear economic dispatch from an explicit in-memory network.

This adapter accepts only small JSON network descriptions. It builds one fixed
one-hour snapshot and solves a lossless linear optimal power flow with PyPSA's
HiGHS backend. It does not load files, execute model code, or accept solver
options from callers.
"""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Mapping
from typing import Any

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    schema,
)
from ..registry import Registry

_TOOLKIT_ID = "pypsa"
_TOOL_NAME = "pypsa.optimize_dispatch"
_MAX_BUSES = 100
_MAX_LINES = 500
_MAX_ELEMENTS = 500
_MAX_CAPACITY_MW = 10_000.0
_MAX_VOLTAGE_KV = 1_000.0
_MAX_REACTANCE_OHM = 10_000.0
_MAX_MARGINAL_COST = 1_000_000.0
_SNAPSHOT = "dispatch"
_SOLVER_TIME_LIMIT_SECONDS = 30.0
_SOLVER_THREADS = 1

PYPSA_DOCS = "https://docs.pypsa.org/latest/api/networks/optimize/"


def _error(code: str, message: str) -> EnergyError:
    return EnergyError(code, message)


def _strict_mapping(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("invalid_input", f"{name} must be an object")
    result = dict(value)
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise _error("invalid_input", f"{name} contains unsupported fields: {', '.join(unknown)}")
    return result


def _list(value: Any, name: str, *, max_items: int, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list):
        raise _error("invalid_input", f"{name} must be a list")
    if nonempty and not value:
        raise _error("invalid_input", f"{name} must not be empty")
    if len(value) > max_items:
        raise _error("input_too_large", f"{name} cannot contain more than {max_items} items")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise _error("invalid_input", f"{name} must be a non-empty string of at most 80 characters")
    return value.strip()


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid_input", f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise _error("invalid_input", f"{name} must be a finite number")
    if strict_positive and result <= 0:
        raise _error("invalid_input", f"{name} must be greater than zero")
    if minimum is not None and result < minimum:
        raise _error("invalid_input", f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise _error("invalid_input", f"{name} must be at most {maximum}")
    return result


def _schema() -> Json:
    bus = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "v_nom_kv": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_VOLTAGE_KV},
        },
        "required": ["id", "v_nom_kv"],
        "additionalProperties": False,
    }
    line = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "from_bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "to_bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "x_ohm": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_REACTANCE_OHM,
            },
            "s_nom_mva": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_CAPACITY_MW,
            },
        },
        "required": ["id", "from_bus", "to_bus", "x_ohm", "s_nom_mva"],
        "additionalProperties": False,
    }
    load = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "p_mw": {"type": "number", "minimum": 0, "maximum": _MAX_CAPACITY_MW},
        },
        "required": ["id", "bus", "p_mw"],
        "additionalProperties": False,
    }
    generator = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "p_nom_mw": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_CAPACITY_MW,
            },
            "marginal_cost": {
                "type": "number",
                "minimum": 0,
                "maximum": _MAX_MARGINAL_COST,
            },
        },
        "required": ["id", "bus", "p_nom_mw", "marginal_cost"],
        "additionalProperties": False,
    }
    network = {
        "type": "object",
        "properties": {
            "buses": {"type": "array", "items": bus, "minItems": 1, "maxItems": _MAX_BUSES},
            "lines": {"type": "array", "items": line, "maxItems": _MAX_LINES},
            "loads": {"type": "array", "items": load, "minItems": 1, "maxItems": _MAX_ELEMENTS},
            "generators": {
                "type": "array",
                "items": generator,
                "minItems": 1,
                "maxItems": _MAX_ELEMENTS,
            },
        },
        "required": ["buses", "lines", "loads", "generators"],
        "additionalProperties": False,
    }
    return schema({"network": network}, required=["network"])


def _parse(args: Json) -> tuple[list[Json], list[Json], list[Json], list[Json]]:
    raw = _strict_mapping(args, "args", {"network"})
    network = _strict_mapping(
        raw.get("network"), "network", {"buses", "lines", "loads", "generators"}
    )
    buses = _list(network.get("buses"), "network.buses", max_items=_MAX_BUSES)
    lines = _list(network.get("lines"), "network.lines", max_items=_MAX_LINES, nonempty=False)
    loads = _list(network.get("loads"), "network.loads", max_items=_MAX_ELEMENTS)
    generators = _list(network.get("generators"), "network.generators", max_items=_MAX_ELEMENTS)
    if len(loads) + len(generators) > _MAX_ELEMENTS:
        raise _error(
            "input_too_large", "network loads and generators cannot exceed 500 elements total"
        )

    all_ids: set[str] = set()

    def unique_id(item: Mapping[str, Any], name: str) -> str:
        item_id = _text(item.get("id"), f"{name}.id")
        if item_id in all_ids:
            raise _error("invalid_network", f"duplicate id {item_id!r}")
        all_ids.add(item_id)
        return item_id

    bus_ids: set[str] = set()
    for index, raw_bus in enumerate(buses):
        bus = _strict_mapping(raw_bus, f"network.buses[{index}]", {"id", "v_nom_kv"})
        bus_id = unique_id(bus, f"network.buses[{index}]")
        bus_ids.add(bus_id)
        _number(
            bus.get("v_nom_kv"),
            f"network.buses[{index}].v_nom_kv",
            strict_positive=True,
            maximum=_MAX_VOLTAGE_KV,
        )

    for collection_name, items, allowed in (
        ("lines", lines, {"id", "from_bus", "to_bus", "x_ohm", "s_nom_mva"}),
        ("loads", loads, {"id", "bus", "p_mw"}),
        ("generators", generators, {"id", "bus", "p_nom_mw", "marginal_cost"}),
    ):
        for index, raw_item in enumerate(items):
            name = f"network.{collection_name}[{index}]"
            item = _strict_mapping(raw_item, name, allowed)
            unique_id(item, name)
            if collection_name == "lines":
                from_bus = _text(item.get("from_bus"), f"{name}.from_bus")
                to_bus = _text(item.get("to_bus"), f"{name}.to_bus")
                if from_bus not in bus_ids or to_bus not in bus_ids:
                    raise _error("invalid_network", f"{name} refers to an unknown bus")
                if from_bus == to_bus:
                    raise _error("invalid_network", f"{name} cannot connect a bus to itself")
                _number(
                    item.get("x_ohm"),
                    f"{name}.x_ohm",
                    strict_positive=True,
                    maximum=_MAX_REACTANCE_OHM,
                )
                _number(
                    item.get("s_nom_mva"),
                    f"{name}.s_nom_mva",
                    strict_positive=True,
                    maximum=_MAX_CAPACITY_MW,
                )
            else:
                bus_id = _text(item.get("bus"), f"{name}.bus")
                if bus_id not in bus_ids:
                    raise _error("invalid_network", f"{name}.bus refers to unknown bus {bus_id!r}")
                if collection_name == "loads":
                    _number(
                        item.get("p_mw"),
                        f"{name}.p_mw",
                        minimum=0,
                        maximum=_MAX_CAPACITY_MW,
                    )
                else:
                    _number(
                        item.get("p_nom_mw"),
                        f"{name}.p_nom_mw",
                        strict_positive=True,
                        maximum=_MAX_CAPACITY_MW,
                    )
                    _number(
                        item.get("marginal_cost"),
                        f"{name}.marginal_cost",
                        minimum=0,
                        maximum=_MAX_MARGINAL_COST,
                    )
    return buses, lines, loads, generators


def _version(package: str, module: Any) -> str:
    module_version = getattr(module, "__version__", None)
    if module_version:
        return str(module_version)
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _solve(network: Any) -> tuple[str, str]:
    """Run with the fixed HiGHS backend and bounded solver settings."""

    status, condition = network.optimize(
        snapshots=[_SNAPSHOT],
        solver_name="highs",
        solver_options={"time_limit": _SOLVER_TIME_LIMIT_SECONDS, "threads": _SOLVER_THREADS},
        include_objective_constant=False,
        transmission_losses=0,
        linearized_unit_commitment=False,
    )
    return str(status).lower(), str(condition).lower()


def _value(frame: Any, snapshot: str, item_id: str, description: str) -> float:
    try:
        value = float(frame.loc[snapshot, item_id])
    except Exception as exc:
        raise _error("invalid_solver_result", f"PyPSA did not return {description}") from exc
    if not math.isfinite(value):
        raise _error("invalid_solver_result", f"PyPSA returned a non-finite {description}")
    return value


async def optimize_dispatch(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Minimize one-hour generation cost subject to linear network constraints."""

    del ctx
    buses, lines, loads, generators = _parse(args)
    try:
        import highspy
        import pypsa
    except Exception as exc:  # pragma: no cover - exercised on base installs
        missing = "highspy" if "highspy" in str(exc).lower() else "pypsa"
        raise _error(
            "dependency_unavailable",
            f"{missing} is required for this network optimization; install the network-solvers extra",
        ) from exc

    network = pypsa.Network()
    network.set_snapshots([_SNAPSHOT])
    network.snapshot_weightings.loc[_SNAPSHOT, "objective"] = 1.0
    for raw_bus in buses:
        network.add("Bus", str(raw_bus["id"]).strip(), v_nom=float(raw_bus["v_nom_kv"]))
    for raw_line in lines:
        network.add(
            "Line",
            str(raw_line["id"]).strip(),
            bus0=str(raw_line["from_bus"]).strip(),
            bus1=str(raw_line["to_bus"]).strip(),
            r=0.0,
            x=float(raw_line["x_ohm"]),
            s_nom=float(raw_line["s_nom_mva"]),
        )
    for raw_load in loads:
        network.add(
            "Load",
            str(raw_load["id"]).strip(),
            bus=str(raw_load["bus"]).strip(),
            p_set=float(raw_load["p_mw"]),
        )
    for raw_generator in generators:
        network.add(
            "Generator",
            str(raw_generator["id"]).strip(),
            bus=str(raw_generator["bus"]).strip(),
            p_nom=float(raw_generator["p_nom_mw"]),
            p_min_pu=0.0,
            p_max_pu=1.0,
            marginal_cost=float(raw_generator["marginal_cost"]),
        )

    try:
        status, condition = _solve(network)
    except Exception as exc:
        raise _error(
            "optimization_failed", "PyPSA failed to optimize the supplied network"
        ) from exc
    if "infeasible" in condition:
        raise _error(
            "infeasible_network", "PyPSA found no dispatch satisfying the network constraints"
        )
    if status != "ok" or condition != "optimal":
        raise _error("optimization_not_converged", "PyPSA did not find an optimal dispatch")

    generator_rows: list[Json] = []
    total_generation_mw = 0.0
    total_cost_per_hour = 0.0
    for raw_generator in generators:
        generator_id = str(raw_generator["id"]).strip()
        dispatch_mw = _value(
            network.generators_t.p,
            _SNAPSHOT,
            generator_id,
            f"dispatch for generator {generator_id!r}",
        )
        total_generation_mw += dispatch_mw
        total_cost_per_hour += dispatch_mw * float(raw_generator["marginal_cost"])
        generator_rows.append(
            {
                "id": generator_id,
                "bus": str(raw_generator["bus"]).strip(),
                "dispatch_mw": dispatch_mw,
                "capacity_mw": float(raw_generator["p_nom_mw"]),
                "marginal_cost_per_mwh": float(raw_generator["marginal_cost"]),
            }
        )

    line_rows: list[Json] = []
    for raw_line in lines:
        line_id = str(raw_line["id"]).strip()
        flow_mw = _value(network.lines_t.p0, _SNAPSHOT, line_id, f"flow for line {line_id!r}")
        capacity_mw = float(raw_line["s_nom_mva"])
        line_rows.append(
            {
                "id": line_id,
                "from_bus": str(raw_line["from_bus"]).strip(),
                "to_bus": str(raw_line["to_bus"]).strip(),
                "flow_mw": flow_mw,
                "capacity_mw": capacity_mw,
                "utilization_percent": abs(flow_mw) / capacity_mw * 100.0,
            }
        )

    total_load_mw = sum(float(raw_load["p_mw"]) for raw_load in loads)
    residual_mw = total_generation_mw - total_load_mw
    assumptions = [
        "one fixed snapshot represents a one-hour operating interval",
        "dispatch is bounded between zero and each generator's declared capacity",
        "line flows use PyPSA's linear lossless DC optimal power flow with the declared line reactance and capacity",
        "marginal_cost uses the caller's currency per MWh; the objective is reported in that currency per hour",
        "the model does not solve AC voltage magnitudes, reactive power, or control behavior",
    ]
    return EnergyResult(
        data={
            "status": "optimal",
            "model": "linear lossless economic dispatch (DC optimal power flow)",
            "snapshot_hours": 1.0,
            "generators": generator_rows,
            "lines": line_rows,
            "load_balance_residual_mw": residual_mw,
            "objective_currency_per_hour": total_cost_per_hour,
        },
        kind=DataKind.SIMULATED,
        unit="MW, caller currency/hour",
        source="pypsa",
        resolution="1 hour",
        assumptions=assumptions,
        warnings=["This linear lossless dispatch is not an AC voltage or operating-safety study."],
        quality="optimal",
        provenance=[
            {
                "kind": "software",
                "library": "pypsa",
                "version": _version("pypsa", pypsa),
                "model": "linear lossless economic dispatch",
            },
            {
                "kind": "software",
                "library": "HiGHS",
                "version": _version("highspy", highspy),
                "model": "linear optimization solver",
            },
        ],
    )


def register(registry: Registry) -> None:
    """Register dispatch under the PyPSA toolkit created by networks.register."""

    registry.add(
        Tool(
            name=_TOOL_NAME,
            toolkit=_TOOLKIT_ID,
            description="Optimize a bounded one-hour lossless linear power network by generator marginal cost.",
            input_schema=_schema(),
            capabilities=[
                "optimize_dispatch",
                "run_network_optimization",
                "economic dispatch",
                "linear OPF",
                "PyPSA",
            ],
            actions={Action.SIMULATE},
            result_kind=DataKind.SIMULATED,
            result_unit="MW, caller currency/hour",
            dependencies=["pypsa", "highspy"],
        ),
        optimize_dispatch,
    )


__all__ = ["optimize_dispatch", "register"]
