"""Bounded in-memory OpenDSS power-flow connector.

This adapter uses the cross-platform DSS-Extensions implementation exposed by
``opendssdirect.py``.  A caller supplies a small, explicit topology as JSON.  The
adapter validates that topology, generates only the OpenDSS objects needed for the
case, and runs a snapshot power flow in a fresh engine context.  It intentionally
does not accept DSS text, model paths, include files, or solver options.

The contract supports three-phase balanced cases and three-phase unbalanced cases
with per-phase load values.  Results retain per-phase voltages and line currents so
an agent can distinguish an unbalanced result from a balanced summary.
"""

from __future__ import annotations

import importlib.util
import math
import re
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
    Toolkit,
    schema,
)
from ..registry import Registry

_TOOLKIT_ID = "opendss"
_TOOL_NAME = "opendss.power_flow"
_MAX_BUSES = 100
_MAX_LINES = 500
_MAX_LOADS = 500
_MAX_PHASES = 3
_MAX_VOLTAGE_KV = 1_000.0
_MAX_FREQUENCY_HZ = 1_000.0
_MAX_LENGTH_KM = 1_000.0
_MAX_POWER_KW = 10_000_000.0
_MAX_IMPEDANCE = 10_000.0
_MAX_ENGINE_COMMANDS = 2_000
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

OPENDSS_DOCS = "https://dss-extensions.org/OpenDSSDirect.py/"


def _error(code: str, message: str, *, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("invalid_input", f"{name} must be an object")
    return dict(value)


def _strict_mapping(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    result = _mapping(value, name)
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


def _text(value: Any, name: str, *, max_length: int = 80) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise _error(
            "invalid_input", f"{name} must be a non-empty string of at most {max_length} characters"
        )
    if not _IDENTIFIER.fullmatch(value):
        raise _error("invalid_input", f"{name} must contain only letters, digits, and underscores")
    return value


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


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error("invalid_input", f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise _error("invalid_input", f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_number(
    item: Mapping[str, Any],
    name: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if name not in item or item[name] is None:
        return default
    return _number(item[name], name, minimum=minimum, maximum=maximum)


def _dependency_error(package: str, exc: Exception) -> EnergyError:
    del exc
    return _error(
        "dependency_unavailable",
        f"{package} is required for this OpenDSS tool; install the network-solvers extra",
    )


def _schema() -> Json:
    bus = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "kv_ll": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_VOLTAGE_KV},
            "phases": {"type": "integer", "minimum": 1, "maximum": _MAX_PHASES},
        },
        "required": ["id", "kv_ll"],
        "additionalProperties": False,
    }
    source = {
        "type": "object",
        "properties": {
            "bus": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "kv_ll": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_VOLTAGE_KV},
            "phases": {"type": "integer", "minimum": 1, "maximum": _MAX_PHASES},
            "pu": {"type": "number", "exclusiveMinimum": 0, "maximum": 2},
            "angle_deg": {"type": "number", "minimum": -360, "maximum": 360},
        },
        "required": ["bus"],
        "additionalProperties": False,
    }
    line = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "from_bus": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "to_bus": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "phases": {"type": "integer", "minimum": 1, "maximum": _MAX_PHASES},
            "length_km": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_LENGTH_KM},
            "r1_ohm_per_km": {"type": "number", "minimum": 0, "maximum": _MAX_IMPEDANCE},
            "x1_ohm_per_km": {"type": "number", "minimum": 0, "maximum": _MAX_IMPEDANCE},
            "r0_ohm_per_km": {"type": "number", "minimum": 0, "maximum": _MAX_IMPEDANCE},
            "x0_ohm_per_km": {"type": "number", "minimum": 0, "maximum": _MAX_IMPEDANCE},
            "ampacity_a": {"type": "number", "exclusiveMinimum": 0, "maximum": 1e7},
        },
        "required": ["id", "from_bus", "to_bus", "length_km", "r1_ohm_per_km", "x1_ohm_per_km"],
        "additionalProperties": False,
    }
    load = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "bus": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"},
            "phases": {"type": "integer", "minimum": 1, "maximum": _MAX_PHASES},
            "connection": {"type": "string", "enum": ["wye", "delta"]},
            "kv": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_VOLTAGE_KV},
            "kw": {"type": "number", "minimum": -_MAX_POWER_KW, "maximum": _MAX_POWER_KW},
            "kvar": {"type": "number", "minimum": -_MAX_POWER_KW, "maximum": _MAX_POWER_KW},
            "kw_by_phase": {
                "type": "array",
                "items": {"type": "number", "minimum": -_MAX_POWER_KW, "maximum": _MAX_POWER_KW},
                "minItems": 1,
                "maxItems": _MAX_PHASES,
            },
            "kvar_by_phase": {
                "type": "array",
                "items": {"type": "number", "minimum": -_MAX_POWER_KW, "maximum": _MAX_POWER_KW},
                "minItems": 1,
                "maxItems": _MAX_PHASES,
            },
        },
        "required": ["id", "bus"],
        "additionalProperties": False,
    }
    network = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["balanced", "unbalanced"]},
            "frequency_hz": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_FREQUENCY_HZ},
            "buses": {"type": "array", "items": bus, "minItems": 1, "maxItems": _MAX_BUSES},
            "source": source,
            "lines": {"type": "array", "items": line, "maxItems": _MAX_LINES},
            "loads": {"type": "array", "items": load, "minItems": 1, "maxItems": _MAX_LOADS},
        },
        "required": ["buses", "source", "loads"],
        "additionalProperties": False,
    }
    return schema({"network": network}, required=["network"])


def _load_phase_values(
    load: Mapping[str, Any], name: str, phases: int, field: str, scalar: str
) -> list[float]:
    by_phase = load.get(field)
    if by_phase is not None:
        if not isinstance(by_phase, list) or len(by_phase) != phases:
            raise _error("invalid_input", f"{name}.{field} must contain exactly {phases} values")
        return [
            _number(value, f"{name}.{field}[{idx}]", minimum=-_MAX_POWER_KW, maximum=_MAX_POWER_KW)
            for idx, value in enumerate(by_phase)
        ]
    value = _number(
        load.get(scalar, 0.0), f"{name}.{scalar}", minimum=-_MAX_POWER_KW, maximum=_MAX_POWER_KW
    )
    return [value / phases] * phases


def _bus_nodes(bus: str, phases: int) -> str:
    return f"{bus}." + ".".join(str(phase) for phase in range(1, phases + 1))


def _dss_command(engine: Any, command: str) -> None:
    """Run one generated command and convert engine failures to a stable error."""

    try:
        engine.Text.Command(command)
    except Exception as exc:
        raise _error(
            "solver_failed", f"OpenDSS rejected a generated network command: {exc}"
        ) from exc
    try:
        number = int(engine.Error.Number())
        description = str(engine.Error.Description())
    except Exception:
        number = 0
        description = ""
    if number:
        raise _error("solver_failed", f"OpenDSS command failed ({number}): {description}")


def _bus_result(engine: Any, bus_id: str) -> Json:
    try:
        engine.Circuit.SetActiveBus(bus_id)
        nodes = [int(value) for value in engine.Bus.Nodes()]
        values = [float(value) for value in engine.Bus.puVmagAngle()]
    except Exception as exc:
        raise _error("solver_failed", f"OpenDSS could not read bus {bus_id!r}") from exc
    if len(values) != 2 * len(nodes):
        raise _error("solver_failed", f"OpenDSS returned malformed voltage data for bus {bus_id!r}")
    phases = [
        {"phase": node, "v_mag_pu": values[2 * index], "v_ang_deg": values[2 * index + 1]}
        for index, node in enumerate(nodes)
    ]
    return {
        "id": bus_id,
        "nodes": nodes,
        "phases": phases,
        "v_mag_pu": sum(item["v_mag_pu"] for item in phases) / len(phases) if phases else 0.0,
        "v_ang_deg": phases[0]["v_ang_deg"] if phases else 0.0,
    }


def _line_result(engine: Any, line: Mapping[str, Any], phases: int) -> Json:
    line_id = str(line["id"])
    try:
        engine.Circuit.SetActiveElement(f"Line.{line_id}")
        currents = [float(value) for value in engine.CktElement.Currents()]
        powers = [float(value) for value in engine.CktElement.Powers()]
        losses = [float(value) for value in engine.CktElement.Losses()]
    except Exception as exc:
        raise _error("solver_failed", f"OpenDSS could not read line {line_id!r}") from exc
    expected = 2 * phases
    if len(currents) < expected or len(powers) < expected:
        raise _error("solver_failed", f"OpenDSS returned malformed line data for {line_id!r}")
    current_phases = [
        {
            "phase": phase,
            "current_a": math.hypot(currents[2 * index], currents[2 * index + 1]),
            "power_kw": powers[2 * index],
            "power_kvar": powers[2 * index + 1],
        }
        for index, phase in enumerate(range(1, phases + 1))
    ]
    max_current = max((item["current_a"] for item in current_phases), default=0.0)
    ampacity = _optional_number(line, "ampacity_a")
    return {
        "id": line_id,
        "from_bus": str(line["from_bus"]),
        "to_bus": str(line["to_bus"]),
        "phases": current_phases,
        "max_current_a": max_current,
        "ampacity_a": ampacity,
        "loading_percent": (100.0 * max_current / ampacity) if ampacity else None,
        "loss_kw": losses[0] / 1000.0 if losses else None,
        "loss_kvar": losses[1] / 1000.0 if len(losses) > 1 else None,
    }


async def power_flow(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Solve a bounded, explicit balanced or unbalanced snapshot network."""

    del ctx
    try:
        import opendssdirect as odd
    except Exception as exc:  # pragma: no cover - exercised when optional extra is absent
        raise _dependency_error("opendssdirect.py", exc) from exc

    args = _strict_mapping(args, "args", {"network"})
    network = _strict_mapping(
        args.get("network"),
        "network",
        {"mode", "frequency_hz", "buses", "source", "lines", "loads"},
    )
    mode = network.get("mode", "balanced")
    if mode not in {"balanced", "unbalanced"}:
        raise _error("invalid_input", "network.mode must be balanced or unbalanced")
    frequency = _number(
        network.get("frequency_hz", 50.0),
        "network.frequency_hz",
        strict_positive=True,
        maximum=_MAX_FREQUENCY_HZ,
    )
    bus_specs = _list(network.get("buses"), "network.buses", max_items=_MAX_BUSES)
    source_spec = _strict_mapping(
        network.get("source"),
        "network.source",
        {"bus", "kv_ll", "phases", "pu", "angle_deg"},
    )
    line_specs = network.get("lines", [])
    if not isinstance(line_specs, list):
        raise _error("invalid_input", "network.lines must be a list")
    if len(line_specs) > _MAX_LINES:
        raise _error(
            "input_too_large", f"network.lines cannot contain more than {_MAX_LINES} items"
        )
    load_specs = _list(network.get("loads"), "network.loads", max_items=_MAX_LOADS)

    buses: dict[str, Json] = {}
    for index, raw in enumerate(bus_specs):
        bus = _strict_mapping(raw, f"network.buses[{index}]", {"id", "kv_ll", "phases"})
        bus_id = _text(bus.get("id"), f"network.buses[{index}].id")
        if bus_id in buses:
            raise _error("invalid_network", f"duplicate bus id {bus_id!r}")
        buses[bus_id] = {
            "id": bus_id,
            "kv_ll": _number(
                bus.get("kv_ll"),
                f"network.buses[{index}].kv_ll",
                strict_positive=True,
                maximum=_MAX_VOLTAGE_KV,
            ),
            "phases": _integer(
                bus.get("phases", _MAX_PHASES),
                f"network.buses[{index}].phases",
                minimum=1,
                maximum=_MAX_PHASES,
            ),
        }
    source_bus = _text(source_spec.get("bus"), "network.source.bus")
    if source_bus not in buses:
        raise _error("invalid_network", f"network.source.bus refers to unknown bus {source_bus!r}")
    source_phases = _integer(
        source_spec.get("phases", buses[source_bus]["phases"]),
        "network.source.phases",
        minimum=1,
        maximum=_MAX_PHASES,
    )
    if source_phases != buses[source_bus]["phases"]:
        raise _error("invalid_network", "network.source.phases must match the source bus phases")
    source_kv = _number(
        source_spec.get("kv_ll", buses[source_bus]["kv_ll"]),
        "network.source.kv_ll",
        strict_positive=True,
        maximum=_MAX_VOLTAGE_KV,
    )
    source_pu = _number(
        source_spec.get("pu", 1.0), "network.source.pu", strict_positive=True, maximum=2
    )
    source_angle = _number(
        source_spec.get("angle_deg", 0.0), "network.source.angle_deg", minimum=-360, maximum=360
    )

    seen_ids: set[str] = set()
    normalized_lines: list[Json] = []
    for index, raw in enumerate(line_specs):
        line = _strict_mapping(
            raw,
            f"network.lines[{index}]",
            {
                "id",
                "from_bus",
                "to_bus",
                "phases",
                "length_km",
                "r1_ohm_per_km",
                "x1_ohm_per_km",
                "r0_ohm_per_km",
                "x0_ohm_per_km",
                "ampacity_a",
            },
        )
        line_id = _text(line.get("id"), f"network.lines[{index}].id")
        if line_id in seen_ids:
            raise _error("invalid_network", f"duplicate line id {line_id!r}")
        seen_ids.add(line_id)
        from_bus = _text(line.get("from_bus"), f"network.lines[{index}].from_bus")
        to_bus = _text(line.get("to_bus"), f"network.lines[{index}].to_bus")
        if from_bus not in buses or to_bus not in buses or from_bus == to_bus:
            raise _error(
                "invalid_network", f"line {line_id!r} must connect two different declared buses"
            )
        phases = _integer(
            line.get("phases", min(buses[from_bus]["phases"], buses[to_bus]["phases"])),
            f"network.lines[{index}].phases",
            minimum=1,
            maximum=_MAX_PHASES,
        )
        if phases > buses[from_bus]["phases"] or phases > buses[to_bus]["phases"]:
            raise _error(
                "invalid_network", f"line {line_id!r} has more phases than an endpoint bus"
            )
        r1 = _number(
            line.get("r1_ohm_per_km"),
            f"network.lines[{index}].r1_ohm_per_km",
            minimum=0,
            maximum=_MAX_IMPEDANCE,
        )
        x1 = _number(
            line.get("x1_ohm_per_km"),
            f"network.lines[{index}].x1_ohm_per_km",
            minimum=0,
            maximum=_MAX_IMPEDANCE,
        )
        normalized_lines.append(
            {
                "id": line_id,
                "from_bus": from_bus,
                "to_bus": to_bus,
                "phases": phases,
                "length_km": _number(
                    line.get("length_km"),
                    f"network.lines[{index}].length_km",
                    strict_positive=True,
                    maximum=_MAX_LENGTH_KM,
                ),
                "r1_ohm_per_km": r1,
                "x1_ohm_per_km": x1,
                "r0_ohm_per_km": _number(
                    line.get("r0_ohm_per_km", 3 * r1),
                    f"network.lines[{index}].r0_ohm_per_km",
                    minimum=0,
                    maximum=_MAX_IMPEDANCE,
                ),
                "x0_ohm_per_km": _number(
                    line.get("x0_ohm_per_km", 3 * x1),
                    f"network.lines[{index}].x0_ohm_per_km",
                    minimum=0,
                    maximum=_MAX_IMPEDANCE,
                ),
                "ampacity_a": _optional_number(line, "ampacity_a", minimum=0.000001),
            }
        )

    normalized_loads: list[Json] = []
    for index, raw in enumerate(load_specs):
        load = _strict_mapping(
            raw,
            f"network.loads[{index}]",
            {
                "id",
                "bus",
                "phases",
                "connection",
                "kv",
                "kw",
                "kvar",
                "kw_by_phase",
                "kvar_by_phase",
            },
        )
        load_id = _text(load.get("id"), f"network.loads[{index}].id")
        if load_id in seen_ids:
            raise _error("invalid_network", f"duplicate element id {load_id!r}")
        seen_ids.add(load_id)
        bus_id = _text(load.get("bus"), f"network.loads[{index}].bus")
        if bus_id not in buses:
            raise _error("invalid_network", f"load {load_id!r} refers to unknown bus {bus_id!r}")
        phases = _integer(
            load.get("phases", buses[bus_id]["phases"]),
            f"network.loads[{index}].phases",
            minimum=1,
            maximum=_MAX_PHASES,
        )
        if phases > buses[bus_id]["phases"]:
            raise _error("invalid_network", f"load {load_id!r} has more phases than its bus")
        connection = load.get("connection", "wye")
        if connection not in {"wye", "delta"}:
            raise _error("invalid_input", f"network.loads[{index}].connection must be wye or delta")
        if mode == "balanced" and phases != 3:
            raise _error("invalid_network", "balanced mode requires three-phase loads")
        if mode == "balanced" and ("kw_by_phase" in load or "kvar_by_phase" in load):
            raise _error(
                "invalid_input", "balanced mode uses scalar kw and kvar, not per-phase arrays"
            )
        kw_by_phase = _load_phase_values(
            load, f"network.loads[{index}]", phases, "kw_by_phase", "kw"
        )
        kvar_by_phase = _load_phase_values(
            load, f"network.loads[{index}]", phases, "kvar_by_phase", "kvar"
        )
        kv_default = (
            buses[bus_id]["kv_ll"]
            if connection == "delta" or phases > 1
            else buses[bus_id]["kv_ll"] / math.sqrt(3)
        )
        kv = _number(
            load.get("kv", kv_default),
            f"network.loads[{index}].kv",
            strict_positive=True,
            maximum=_MAX_VOLTAGE_KV,
        )
        normalized_loads.append(
            {
                "id": load_id,
                "bus": bus_id,
                "phases": phases,
                "connection": connection,
                "kv": kv,
                "kw_by_phase": kw_by_phase,
                "kvar_by_phase": kvar_by_phase,
                "kw": sum(kw_by_phase),
                "kvar": sum(kvar_by_phase),
            }
        )

    if source_phases == 1 and any(line["phases"] > 1 for line in normalized_lines):
        raise _error("invalid_network", "a single-phase source cannot feed a multi-phase line")

    try:
        engine = odd.dss.NewContext()
        # The generated contract never needs a working directory or DOS command.
        engine.Basic.AllowChangeDir(False)
        engine.Basic.AllowDOScmd(False)
        engine.Error.UseExceptions(True)
    except Exception as exc:
        raise _error("solver_failed", "could not create an isolated OpenDSS context") from exc

    commands: list[str] = [
        "clear",
        f"new circuit.energy_agent_circuit basekv={source_kv:.12g} pu={source_pu:.12g} phases={source_phases} bus1={source_bus} angle={source_angle:.12g}",
        f"edit vsource.source frequency={frequency:.12g}",
    ]
    for line in normalized_lines:
        phases = int(line["phases"])
        r1 = float(line["r1_ohm_per_km"])
        x1 = float(line["x1_ohm_per_km"])
        r0 = float(line["r0_ohm_per_km"])
        x0 = float(line["x0_ohm_per_km"])
        commands.append(
            f"new line.{line['id']} phases={phases} bus1={_bus_nodes(str(line['from_bus']), phases)} "
            f"bus2={_bus_nodes(str(line['to_bus']), phases)} length={float(line['length_km']):.12g} units=km "
            f"r1={r1:.12g} x1={x1:.12g} r0={r0:.12g} x0={x0:.12g}"
        )
        if line["ampacity_a"] is not None:
            commands[-1] += f" normamps={float(line['ampacity_a']):.12g}"
    for load in normalized_loads:
        phases = int(load["phases"])
        if mode == "balanced":
            commands.append(
                f"new load.{load['id']} phases={phases} bus1={_bus_nodes(str(load['bus']), phases)} "
                f"conn={load['connection']} kv={float(load['kv']):.12g} kw={float(load['kw']):.12g} kvar={float(load['kvar']):.12g} model=1"
            )
        else:
            for phase, (kw, kvar) in enumerate(
                zip(load["kw_by_phase"], load["kvar_by_phase"], strict=True), start=1
            ):
                generated_id = f"{load['id']}_p{phase}"
                # The public input uses line-to-line kV for a multi-phase load.
                # OpenDSS expects line-to-neutral kV for a one-phase wye element.
                element_kv = float(load["kv"])
                if load["connection"] == "wye" and phases > 1:
                    element_kv /= math.sqrt(3)
                commands.append(
                    f"new load.{generated_id} phases=1 bus1={load['bus']}.{phase} conn={load['connection']} "
                    f"kv={element_kv:.12g} kw={float(kw):.12g} kvar={float(kvar):.12g} model=1"
                )
    voltage_bases = sorted({float(bus["kv_ll"]) for bus in buses.values()})
    commands.append(
        "set voltagebases=[" + ",".join(f"{value:.12g}" for value in voltage_bases) + "]"
    )
    commands.append("calcvoltagebases")
    # calcvoltagebases restores the engine's default solution frequency.  Set it
    # after that command and after the Vsource edit so 50 Hz and 60 Hz cases both
    # solve with the caller's declared frequency.
    commands.append(f"set frequency={frequency:.12g}")
    commands.append("set mode=snapshot")
    commands.append("solve")
    if len(commands) > _MAX_ENGINE_COMMANDS:
        raise _error("input_too_large", "generated OpenDSS command count exceeds the safety limit")
    for command in commands:
        _dss_command(engine, command)
    try:
        converged = bool(engine.Solution.Converged())
    except Exception as exc:
        raise _error("solver_failed", "OpenDSS did not return a convergence state") from exc
    if not converged:
        raise _error("power_flow_not_converged", "OpenDSS did not converge for this network")

    bus_results = [_bus_result(engine, bus_id) for bus_id in buses]
    line_results = [_line_result(engine, line, int(line["phases"])) for line in normalized_lines]
    try:
        total_power = [float(value) for value in engine.Circuit.TotalPower()]
        losses = [float(value) for value in engine.Circuit.Losses()]
        source_engine_version = str(engine.Basic.Version())
    except Exception as exc:
        raise _error("solver_failed", "OpenDSS could not return circuit totals") from exc
    source_kw = -total_power[0] if total_power else 0.0
    source_kvar = -total_power[1] if len(total_power) > 1 else 0.0
    loss_kw = losses[0] / 1000.0 if losses else 0.0
    loss_kvar = losses[1] / 1000.0 if len(losses) > 1 else 0.0
    load_kw = sum(float(load["kw"]) for load in normalized_loads)
    load_kvar = sum(float(load["kvar"]) for load in normalized_loads)
    load_results = [
        {
            "id": str(load["id"]),
            "bus": str(load["bus"]),
            "phases": int(load["phases"]),
            "connection": str(load["connection"]),
            "kw": float(load["kw"]),
            "kvar": float(load["kvar"]),
            "kw_by_phase": [float(value) for value in load["kw_by_phase"]],
            "kvar_by_phase": [float(value) for value in load["kvar_by_phase"]],
        }
        for load in normalized_loads
    ]
    return EnergyResult(
        data={
            "converged": True,
            "mode": mode,
            "frequency_hz": frequency,
            "source": {
                "bus": source_bus,
                "kv_ll": source_kv,
                "phases": source_phases,
                "pu": source_pu,
                "angle_deg": source_angle,
                "kw": source_kw,
                "kvar": source_kvar,
            },
            "buses": bus_results,
            "lines": line_results,
            "loads": load_results,
            "totals": {
                "source_kw": source_kw,
                "source_kvar": source_kvar,
                "load_kw": load_kw,
                "load_kvar": load_kvar,
                "loss_kw": loss_kw,
                "loss_kvar": loss_kvar,
                "balance_error_kw": source_kw - load_kw - loss_kw,
                "balance_error_kvar": source_kvar - load_kvar - loss_kvar,
            },
        },
        kind=DataKind.SIMULATED,
        unit="kW, kvar, A, pu, degree",
        source="opendssdirect",
        timezone="UTC",
        resolution="steady-state snapshot",
        assumptions=[
            "DSS-Extensions OpenDSS snapshot power flow on a caller-supplied bounded topology",
            "line impedances use positive- and zero-sequence ohms per kilometre; missing zero-sequence values default to three times the positive-sequence values",
            "loads are constant-power model 1 elements; no controls, protection, faults, dynamics, or time series are simulated",
        ],
        warnings=[
            "This is a bounded engineering simulation and is not a certification of network safety or an operating limit.",
            "DSS-Extensions is an alternative OpenDSS implementation and is not supported by EPRI.",
        ],
        quality="converged-power-flow",
        provenance=[
            {"kind": "software", "library": "opendssdirect.py", "version": str(odd.__version__)},
            {"kind": "engine", "library": "DSS-Extensions", "version": source_engine_version},
        ],
    )


def _available() -> bool:
    return importlib.util.find_spec("opendssdirect") is not None


def register(registry: Registry) -> None:
    """Register the OpenDSSDirect adapter without importing the optional engine."""

    registry.add_toolkit(
        Toolkit(
            id=_TOOLKIT_ID,
            name="OpenDSS via DSS-Extensions",
            description="Bounded in-memory balanced and unbalanced distribution-network power flow.",
            runtime="python",
            status="experimental" if _available() else "unavailable",
            docs_url=OPENDSS_DOCS,
            categories=[
                "power flow",
                "distribution grid",
                "unbalanced network",
                "engineering simulation",
            ],
        )
    )
    registry.add(
        Tool(
            name=_TOOL_NAME,
            toolkit=_TOOLKIT_ID,
            description="Solve a bounded explicit balanced or per-phase unbalanced network with OpenDSSDirect; no DSS files or arbitrary commands are accepted.",
            input_schema=_schema(),
            capabilities=[
                "power flow",
                "distribution network",
                "unbalanced power flow",
                "phase voltage",
                "line current",
                "DSS-Extensions",
            ],
            actions={Action.SIMULATE},
            result_kind=DataKind.SIMULATED,
            result_unit="kW, kvar, A, pu, degree",
            dependencies=["opendssdirect"],
        ),
        power_flow,
    )


__all__ = ["OPENDSS_DOCS", "power_flow", "register"]
