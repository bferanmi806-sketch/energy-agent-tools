"""Bounded local power and fluid-network solver connectors.

The adapters in this module intentionally expose small, explicit exchange
contracts.  They build solver networks from JSON values supplied by the caller;
they never load a solver file, execute model code, or accept arbitrary solver
options.  PyPSA and pandapipes remain optional dependencies and are imported only
inside their handlers so a base installation can still discover the tools and
report a useful ``dependency_unavailable`` error at execution time.

The contracts are deliberately narrower than either upstream library.  That makes
their results portable across installations and gives the runtime a place to
validate units, topology, and resource bounds before invoking a numerical solver.
See ``docs/network-solvers.md`` for the upstream contract references.
"""

from __future__ import annotations

import importlib.util
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
    Toolkit,
    schema,
)
from ..registry import Registry

_PYPSA_TOOLKIT = "pypsa"
_PANDAPIPES_TOOLKIT = "pandapipes"
_MAX_BUSES = 100
_MAX_LINES = 500
_MAX_JUNCTIONS = 100
_MAX_PIPES = 500
_MAX_ELEMENTS = 500
_MAX_MVA = 10_000.0
_MAX_VOLTAGE_KV = 1_000.0
_MAX_PRESSURE_BAR = 200.0
_MAX_LENGTH_KM = 100.0

PYPSA_DOCS = "https://docs.pypsa.org/latest/user-guide/power-flow/"
PANDAPIPES_DOCS = "https://pandapipes.readthedocs.io/en/latest/pipeflow/"


def _error(code: str, message: str, *, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("invalid_input", f"{name} must be an object")
    return dict(value)


def _list(value: Any, name: str, *, max_items: int, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list):
        raise _error("invalid_input", f"{name} must be a list")
    if nonempty and not value:
        raise _error("invalid_input", f"{name} must not be empty")
    if len(value) > max_items:
        raise _error("input_too_large", f"{name} cannot contain more than {max_items} items")
    return value


def _text(value: Any, name: str, *, max_length: int = 80) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise _error(
            "invalid_input", f"{name} must be a non-empty string of at most {max_length} characters"
        )
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


def _optional_number(
    item: Mapping[str, Any],
    name: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_positive: bool = False,
) -> float | None:
    if name not in item or item[name] is None:
        return default
    return _number(
        item[name],
        name,
        minimum=minimum,
        maximum=maximum,
        strict_positive=strict_positive,
    )


def _id(item: Mapping[str, Any], name: str) -> str:
    return _text(item.get(name), name, max_length=80)


def _dependency_error(package: str, exc: Exception) -> EnergyError:
    del exc
    return _error(
        "dependency_unavailable",
        f"{package} is required for this network solver; install the network-solvers extra",
    )


def _version(module: Any) -> str:
    return str(getattr(module, "__version__", "unknown"))


def _solver_provenance(library: str, version: str, model: str) -> list[Json]:
    return [{"kind": "software", "library": library, "version": version, "model": model}]


# ---------------------------------------------------------------------------
# PyPSA


def _pypsa_schema() -> Json:
    bus = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "v_nom_kv": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_VOLTAGE_KV},
            "v_mag_pu_set": {"type": "number", "exclusiveMinimum": 0, "maximum": 2},
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
            "r_ohm": {"type": "number", "minimum": 0},
            "x_ohm": {"type": "number", "minimum": 0},
            "length_km": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_LENGTH_KM},
            "r_ohm_per_km": {"type": "number", "minimum": 0},
            "x_ohm_per_km": {"type": "number", "minimum": 0},
            "s_nom_mva": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_MVA},
        },
        "required": ["id", "from_bus", "to_bus"],
        "additionalProperties": False,
    }
    load = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "p_mw": {"type": "number"},
            "q_mvar": {"type": "number"},
        },
        "required": ["id", "bus", "p_mw"],
        "additionalProperties": False,
    }
    generator = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "p_mw": {"type": "number"},
            "q_mvar": {"type": "number"},
            "control": {"type": "string", "enum": ["PQ", "PV"]},
            "v_mag_pu": {"type": "number", "exclusiveMinimum": 0, "maximum": 2},
        },
        "required": ["id", "bus", "p_mw"],
        "additionalProperties": False,
    }
    network = {
        "type": "object",
        "properties": {
            "base_mva": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_MVA,
                "default": 100,
            },
            "buses": {"type": "array", "items": bus, "minItems": 1, "maxItems": _MAX_BUSES},
            "lines": {"type": "array", "items": line, "maxItems": _MAX_LINES},
            "loads": {"type": "array", "items": load, "maxItems": _MAX_ELEMENTS},
            "generators": {"type": "array", "items": generator, "maxItems": _MAX_ELEMENTS},
            "slack_bus": {"type": "string", "minLength": 1, "maxLength": 80},
            "slack_voltage_pu": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 2,
                "default": 1,
            },
        },
        "required": ["buses", "slack_bus"],
        "additionalProperties": False,
    }
    return schema({"network": network}, required=["network"])


def _impedance(line: Mapping[str, Any]) -> tuple[float, float]:
    """Return total R/X in ohms from either total or per-km fields."""

    has_total = "r_ohm" in line or "x_ohm" in line
    has_per_km = "r_ohm_per_km" in line or "x_ohm_per_km" in line
    if has_total and has_per_km:
        raise _error("invalid_input", "line impedance must use total ohms or ohms per km, not both")
    if not has_total and not has_per_km:
        raise _error("invalid_input", "each line requires r_ohm/x_ohm or r_ohm_per_km/x_ohm_per_km")
    r_name, x_name = ("r_ohm", "x_ohm") if has_total else ("r_ohm_per_km", "x_ohm_per_km")
    if r_name not in line or x_name not in line:
        raise _error("invalid_input", f"line requires both {r_name} and {x_name}")
    r = _number(line[r_name], f"line.{r_name}", minimum=0)
    x = _number(line[x_name], f"line.{x_name}", minimum=0)
    if has_per_km:
        length = _number(
            line.get("length_km"), "line.length_km", strict_positive=True, maximum=_MAX_LENGTH_KM
        )
        r *= length
        x *= length
    return r, x


def _series_value(frame: Any, snapshot: Any, name: str) -> float:
    try:
        value = frame.loc[snapshot, name]
    except Exception:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


async def pypsa_power_flow(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Run a bounded PyPSA AC power flow from explicit buses and impedances."""

    del ctx
    try:
        import pypsa
    except Exception as exc:  # pragma: no cover - exercised on base installs
        raise _dependency_error("pypsa", exc) from exc

    raw = _mapping(args, "args")
    network = _mapping(raw.get("network", raw), "network")
    buses = _list(network.get("buses"), "network.buses", max_items=_MAX_BUSES)
    lines = _list(network.get("lines", []), "network.lines", max_items=_MAX_LINES, nonempty=False)
    loads = _list(
        network.get("loads", []), "network.loads", max_items=_MAX_ELEMENTS, nonempty=False
    )
    generators = _list(
        network.get("generators", []),
        "network.generators",
        max_items=_MAX_ELEMENTS,
        nonempty=False,
    )
    base_mva = _number(
        network.get("base_mva", 100.0), "network.base_mva", strict_positive=True, maximum=_MAX_MVA
    )
    slack_bus = _text(network.get("slack_bus"), "network.slack_bus")
    slack_voltage = _number(
        network.get("slack_voltage_pu", 1.0),
        "network.slack_voltage_pu",
        strict_positive=True,
        maximum=2,
    )

    bus_ids: set[str] = set()
    bus_voltage: dict[str, float] = {}
    for idx, raw_bus in enumerate(buses):
        bus = _mapping(raw_bus, f"network.buses[{idx}]")
        bus_id = _id(bus, "id")
        if bus_id in bus_ids:
            raise _error("invalid_network", f"duplicate bus id {bus_id!r}")
        bus_ids.add(bus_id)
        bus_voltage[bus_id] = _number(
            bus.get("v_nom_kv"),
            f"network.buses[{idx}].v_nom_kv",
            strict_positive=True,
            maximum=_MAX_VOLTAGE_KV,
        )
    if slack_bus not in bus_ids:
        raise _error("invalid_network", "network.slack_bus must reference a declared bus")

    def check_bus(value: Any, name: str) -> str:
        bus_id = _text(value, name)
        if bus_id not in bus_ids:
            raise _error("invalid_network", f"{name} refers to unknown bus {bus_id!r}")
        return bus_id

    net = pypsa.Network()
    net.set_snapshots(["now"])
    for idx, raw_bus in enumerate(buses):
        bus = _mapping(raw_bus, f"network.buses[{idx}]")
        bus_id = str(bus["id"]).strip()
        net.add(
            "Bus",
            bus_id,
            v_nom=bus_voltage[bus_id],
            v_mag_pu_set=_optional_number(
                bus,
                "v_mag_pu_set",
                default=1.0,
                strict_positive=True,
                maximum=2,
            ),
        )

    line_ids: set[str] = set()
    for idx, raw_line in enumerate(lines):
        line = _mapping(raw_line, f"network.lines[{idx}]")
        line_id = _id(line, "id")
        if line_id in line_ids:
            raise _error("invalid_network", f"duplicate line id {line_id!r}")
        line_ids.add(line_id)
        bus0 = check_bus(line.get("from_bus"), f"network.lines[{idx}].from_bus")
        bus1 = check_bus(line.get("to_bus"), f"network.lines[{idx}].to_bus")
        if bus0 == bus1:
            raise _error("invalid_network", f"network.lines[{idx}] cannot connect a bus to itself")
        r_ohm, x_ohm = _impedance(line)
        s_nom = _number(
            line.get("s_nom_mva", base_mva),
            f"network.lines[{idx}].s_nom_mva",
            strict_positive=True,
            maximum=_MAX_MVA,
        )
        # PyPSA Line.r/x are physical ohms.  PyPSA derives r_pu/x_pu from the
        # bus nominal voltage during calculate_dependent_values(); only
        # transformers use per-unit r/x as input.  The public contract remains
        # explicit ohms.
        net.add(
            "Line",
            line_id,
            bus0=bus0,
            bus1=bus1,
            r=r_ohm,
            x=x_ohm,
            s_nom=s_nom,
        )

    load_ids: set[str] = set()
    for idx, raw_load in enumerate(loads):
        load = _mapping(raw_load, f"network.loads[{idx}]")
        load_id = _id(load, "id")
        if load_id in load_ids:
            raise _error("invalid_network", f"duplicate load id {load_id!r}")
        load_ids.add(load_id)
        load_bus = check_bus(load.get("bus"), f"network.loads[{idx}].bus")
        p_mw = _number(load.get("p_mw"), f"network.loads[{idx}].p_mw")
        q_mvar = _optional_number(load, "q_mvar", default=0.0)
        net.add("Load", load_id, bus=load_bus, p_set=p_mw, q_set=q_mvar)

    generator_ids: set[str] = {"__energy_agent_tools_slack__"}
    net.add(
        "Generator",
        "__energy_agent_tools_slack__",
        bus=slack_bus,
        control="Slack",
    )
    # PyPSA stores the AC voltage setpoint on the bus, including for the slack
    # generator.  It is not a generator attribute in current PyPSA releases.
    net.buses.at[slack_bus, "v_mag_pu_set"] = slack_voltage
    pv_voltage_by_bus: dict[str, float] = {}
    for idx, raw_generator in enumerate(generators):
        generator = _mapping(raw_generator, f"network.generators[{idx}]")
        generator_id = _id(generator, "id")
        if generator_id in generator_ids:
            raise _error("invalid_network", f"duplicate generator id {generator_id!r}")
        generator_ids.add(generator_id)
        generator_bus = check_bus(generator.get("bus"), f"network.generators[{idx}].bus")
        p_mw = _number(generator.get("p_mw"), f"network.generators[{idx}].p_mw")
        control = generator.get("control", "PQ")
        if control not in {"PQ", "PV"}:
            raise _error("invalid_input", f"network.generators[{idx}].control must be PQ or PV")
        q_mvar = _optional_number(generator, "q_mvar", default=0.0)
        kwargs: dict[str, Any] = {
            "bus": generator_bus,
            "control": control,
            "p_set": p_mw,
            "q_set": q_mvar,
        }
        if control == "PV":
            pv_voltage = _optional_number(
                generator,
                "v_mag_pu",
                default=1.0,
                strict_positive=True,
                maximum=2,
            )
            assert pv_voltage is not None
            previous = pv_voltage_by_bus.get(generator_bus)
            if previous is not None and not math.isclose(
                previous, pv_voltage, rel_tol=0, abs_tol=1e-12
            ):
                raise _error("invalid_network", "PV generators on one bus must share v_mag_pu")
            pv_voltage_by_bus[generator_bus] = pv_voltage
            net.buses.at[generator_bus, "v_mag_pu_set"] = pv_voltage
        net.add("Generator", generator_id, **kwargs)

    try:
        report = net.pf()
    except Exception as exc:
        raise _error("power_flow_failed", "PyPSA failed to solve the supplied network") from exc

    converged = True
    if isinstance(report, Mapping) and "converged" in report:
        values = report["converged"]
        try:
            converged = all(bool(value) for value in values.to_numpy().ravel())
        except Exception:
            try:
                converged = all(bool(value) for value in values)
            except TypeError:
                converged = bool(values)
    if not converged:
        raise _error("power_flow_not_converged", "PyPSA did not converge for this network")

    snapshot = "now"
    buses_out: list[Json] = []
    for bus_id in bus_ids:
        voltage_angle_rad = _series_value(net.buses_t.v_ang, snapshot, bus_id)
        buses_out.append(
            {
                "id": bus_id,
                "v_mag_pu": _series_value(net.buses_t.v_mag_pu, snapshot, bus_id),
                "v_ang_degree": (
                    math.degrees(voltage_angle_rad)
                    if math.isfinite(voltage_angle_rad)
                    else voltage_angle_rad
                ),
                "p_mw": _series_value(net.buses_t.p, snapshot, bus_id),
                "q_mvar": _series_value(net.buses_t.q, snapshot, bus_id),
            }
        )
    lines_out: list[Json] = []
    line_loss_mw = 0.0
    line_q_loss_mvar = 0.0
    for line_id in sorted(line_ids):
        p0 = _series_value(net.lines_t.p0, snapshot, line_id)
        p1 = _series_value(net.lines_t.p1, snapshot, line_id)
        q0 = _series_value(net.lines_t.q0, snapshot, line_id)
        q1 = _series_value(net.lines_t.q1, snapshot, line_id)
        loss = p0 + p1
        q_loss = q0 + q1
        if math.isfinite(loss):
            line_loss_mw += loss
        if math.isfinite(q_loss):
            line_q_loss_mvar += q_loss
        lines_out.append(
            {
                "id": line_id,
                "r_ohm": float(net.lines.loc[line_id, "r"]),
                "x_ohm": float(net.lines.loc[line_id, "x"]),
                "r_pu": float(net.lines.loc[line_id, "r_pu"]),
                "x_pu": float(net.lines.loc[line_id, "x_pu"]),
                "p_from_mw": p0,
                "p_to_mw": p1,
                "q_from_mvar": q0,
                "q_to_mvar": q1,
                "loss_mw": loss,
                "loss_mvar": q_loss,
            }
        )

    loads_out: list[Json] = []
    load_mw = 0.0
    for load_id in sorted(load_ids):
        p = _series_value(net.loads_t.p, snapshot, load_id)
        q = _series_value(net.loads_t.q, snapshot, load_id)
        load_mw += p if math.isfinite(p) else 0.0
        loads_out.append({"id": load_id, "p_mw": p, "q_mvar": q})

    generation_out: list[Json] = []
    non_slack_generation_mw = 0.0
    non_slack_generation_q_mvar = 0.0
    for generator_id in sorted(generator_ids):
        p = _series_value(net.generators_t.p, snapshot, generator_id)
        q = _series_value(net.generators_t.q, snapshot, generator_id)
        if generator_id != "__energy_agent_tools_slack__":
            non_slack_generation_mw += p if math.isfinite(p) else 0.0
            non_slack_generation_q_mvar += q if math.isfinite(q) else 0.0
        generation_out.append({"id": generator_id, "p_mw": p, "q_mvar": q})

    # PyPSA deliberately leaves the dispatch of a Slack generator as NaN in
    # generators_t.p; its solved injection is represented by the slack bus.  The
    # balance identity gives the finite generator result without exposing NaN in
    # the EnergyResult contract.
    generation_mw = load_mw + line_loss_mw
    generation_q_mvar = (
        sum(
            (item["q_mvar"] if isinstance(item["q_mvar"], (int, float)) else 0.0)
            for item in loads_out
        )
        + line_q_loss_mvar
    )
    slack_p = generation_mw - non_slack_generation_mw
    slack_q = generation_q_mvar - non_slack_generation_q_mvar
    for item in generation_out:
        if item["id"] == "__energy_agent_tools_slack__":
            item["p_mw"] = slack_p
            item["q_mvar"] = slack_q

    balance_error = generation_mw - load_mw - line_loss_mw
    return EnergyResult(
        data={
            "converged": True,
            "buses": sorted(buses_out, key=lambda item: str(item["id"])),
            "lines": lines_out,
            "loads": loads_out,
            "generation": generation_out,
            "totals": {
                "generation_mw": generation_mw,
                "load_mw": load_mw,
                "line_loss_mw": line_loss_mw,
                "balance_error_mw": balance_error,
                "generation_q_mvar": generation_q_mvar,
                "line_q_loss_mvar": line_q_loss_mvar,
            },
        },
        kind=DataKind.SIMULATED,
        unit="MW, Mvar, pu, degree",
        source="pypsa",
        timezone="UTC",
        resolution="steady-state",
        assumptions=[
            "PyPSA non-linear AC power flow with one automatically created slack generator",
            "line impedance was supplied to PyPSA in physical ohms; PyPSA derived r_pu/x_pu from bus nominal voltage",
            "loads and non-slack generators are fixed P/Q setpoints unless a generator is explicitly marked PV",
        ],
        quality="converged-power-flow",
        provenance=_solver_provenance("pypsa", _version(pypsa), "non-linear AC power flow"),
    )


# ---------------------------------------------------------------------------
# pandapipes


def _pandapipes_schema() -> Json:
    junction = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "pn_bar": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_PRESSURE_BAR},
            "tfluid_k": {"type": "number", "exclusiveMinimum": 0, "maximum": 2_000},
            "height_m": {"type": "number", "minimum": -1_000, "maximum": 10_000},
        },
        "required": ["id", "pn_bar"],
        "additionalProperties": False,
    }
    pipe = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "from_junction": {"type": "string", "minLength": 1, "maxLength": 80},
            "to_junction": {"type": "string", "minLength": 1, "maxLength": 80},
            "length_km": {"type": "number", "exclusiveMinimum": 0, "maximum": _MAX_LENGTH_KM},
            "diameter_m": {"type": "number", "exclusiveMinimum": 0, "maximum": 10},
            "k_mm": {"type": "number", "minimum": 0, "maximum": 100},
            "loss_coefficient": {"type": "number", "minimum": 0, "maximum": 10_000},
        },
        "required": ["id", "from_junction", "to_junction", "length_km", "diameter_m"],
        "additionalProperties": False,
    }
    flow = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "junction": {"type": "string", "minLength": 1, "maxLength": 80},
            "mdot_kg_per_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 1_000},
        },
        "required": ["id", "junction", "mdot_kg_per_s"],
        "additionalProperties": False,
    }
    network = {
        "type": "object",
        "properties": {
            "fluid": {
                "type": "string",
                "enum": ["water", "gas", "lgas", "hgas", "hydrogen", "methane"],
            },
            "junctions": {
                "type": "array",
                "items": junction,
                "minItems": 1,
                "maxItems": _MAX_JUNCTIONS,
            },
            "pipes": {"type": "array", "items": pipe, "maxItems": _MAX_PIPES},
            "sources": {"type": "array", "items": flow, "maxItems": _MAX_ELEMENTS},
            "sinks": {"type": "array", "items": flow, "maxItems": _MAX_ELEMENTS},
            "reference_junction": {"type": "string", "minLength": 1, "maxLength": 80},
            "reference_pressure_bar": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": _MAX_PRESSURE_BAR,
            },
            "reference_temperature_k": {"type": "number", "exclusiveMinimum": 0, "maximum": 2_000},
        },
        "required": ["fluid", "junctions", "pipes", "reference_junction", "reference_pressure_bar"],
        "additionalProperties": False,
    }
    return schema({"network": network}, required=["network"])


async def pandapipes_pipeflow(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Run a bounded pandapipes hydraulic pipeflow on explicit junctions and pipes."""

    del ctx
    try:
        import pandapipes as pp
    except Exception as exc:  # pragma: no cover - exercised on base installs
        raise _dependency_error("pandapipes", exc) from exc

    raw = _mapping(args, "args")
    network = _mapping(raw.get("network", raw), "network")
    fluid = _text(network.get("fluid"), "network.fluid", max_length=20).lower()
    if fluid == "gas":
        fluid = "lgas"
    allowed_fluids = {"water", "lgas", "hgas", "hydrogen", "methane"}
    if fluid not in allowed_fluids:
        raise _error("invalid_input", "network.fluid must be a supported preset fluid")
    junctions = _list(network.get("junctions"), "network.junctions", max_items=_MAX_JUNCTIONS)
    pipes = _list(network.get("pipes"), "network.pipes", max_items=_MAX_PIPES, nonempty=False)
    sources = _list(
        network.get("sources", []), "network.sources", max_items=_MAX_ELEMENTS, nonempty=False
    )
    sinks = _list(
        network.get("sinks", []), "network.sinks", max_items=_MAX_ELEMENTS, nonempty=False
    )
    reference = _text(network.get("reference_junction"), "network.reference_junction")
    reference_pressure = _number(
        network.get("reference_pressure_bar"),
        "network.reference_pressure_bar",
        strict_positive=True,
        maximum=_MAX_PRESSURE_BAR,
    )
    reference_temperature = _number(
        network.get("reference_temperature_k", 293.15),
        "network.reference_temperature_k",
        strict_positive=True,
        maximum=2_000,
    )

    junction_ids: set[str] = set()
    net = pp.create_empty_network(fluid=fluid)
    for idx, raw_junction in enumerate(junctions):
        junction = _mapping(raw_junction, f"network.junctions[{idx}]")
        junction_id = _id(junction, "id")
        if junction_id in junction_ids:
            raise _error("invalid_network", f"duplicate junction id {junction_id!r}")
        junction_ids.add(junction_id)
        pn_bar = _number(
            junction.get("pn_bar"),
            f"network.junctions[{idx}].pn_bar",
            strict_positive=True,
            maximum=_MAX_PRESSURE_BAR,
        )
        t_k = _optional_number(
            junction, "tfluid_k", default=reference_temperature, strict_positive=True, maximum=2_000
        )
        height = _optional_number(junction, "height_m", default=0.0, minimum=-1_000, maximum=10_000)
        pp.create_junction(
            net,
            pn_bar=pn_bar,
            tfluid_k=t_k,
            height_m=height,
            name=junction_id,
        )
    if reference not in junction_ids:
        raise _error(
            "invalid_network", "network.reference_junction must reference a declared junction"
        )

    def check_junction(value: Any, name: str) -> str:
        junction_id = _text(value, name)
        if junction_id not in junction_ids:
            raise _error("invalid_network", f"{name} refers to unknown junction {junction_id!r}")
        return junction_id

    # ``set`` iteration order is not stable across processes.  Rebuild the index
    # from the input order so IDs and result rows retain caller order.
    ordered_junctions = [
        _id(_mapping(raw_junction, f"network.junctions[{idx}]"), "id")
        for idx, raw_junction in enumerate(junctions)
    ]
    junction_index = {junction_id: idx for idx, junction_id in enumerate(ordered_junctions)}

    pipe_ids: set[str] = set()
    for idx, raw_pipe in enumerate(pipes):
        pipe = _mapping(raw_pipe, f"network.pipes[{idx}]")
        pipe_id = _id(pipe, "id")
        if pipe_id in pipe_ids:
            raise _error("invalid_network", f"duplicate pipe id {pipe_id!r}")
        pipe_ids.add(pipe_id)
        from_id = check_junction(pipe.get("from_junction"), f"network.pipes[{idx}].from_junction")
        to_id = check_junction(pipe.get("to_junction"), f"network.pipes[{idx}].to_junction")
        if from_id == to_id:
            raise _error(
                "invalid_network", f"network.pipes[{idx}] cannot connect a junction to itself"
            )
        length_km = _number(
            pipe.get("length_km"),
            f"network.pipes[{idx}].length_km",
            strict_positive=True,
            maximum=_MAX_LENGTH_KM,
        )
        diameter_m = _number(
            pipe.get("diameter_m"),
            f"network.pipes[{idx}].diameter_m",
            strict_positive=True,
            maximum=10,
        )
        k_mm = _optional_number(pipe, "k_mm", default=0.1, minimum=0, maximum=100)
        loss = _optional_number(pipe, "loss_coefficient", default=0.0, minimum=0, maximum=10_000)
        pp.create_pipe_from_parameters(
            net,
            from_junction=junction_index[from_id],
            to_junction=junction_index[to_id],
            length_km=length_km,
            inner_diameter_mm=diameter_m * 1_000.0,
            k_mm=k_mm,
            loss_coefficient=loss,
            name=pipe_id,
        )

    source_ids: set[str] = set()
    for idx, raw_source in enumerate(sources):
        source = _mapping(raw_source, f"network.sources[{idx}]")
        source_id = _id(source, "id")
        if source_id in source_ids:
            raise _error("invalid_network", f"duplicate source/sink id {source_id!r}")
        source_ids.add(source_id)
        junction_id = check_junction(source.get("junction"), f"network.sources[{idx}].junction")
        mdot = _number(
            source.get("mdot_kg_per_s"),
            f"network.sources[{idx}].mdot_kg_per_s",
            strict_positive=True,
            maximum=1_000,
        )
        pp.create_source(
            net, junction=junction_index[junction_id], mdot_kg_per_s=mdot, name=source_id
        )

    for idx, raw_sink in enumerate(sinks):
        sink = _mapping(raw_sink, f"network.sinks[{idx}]")
        sink_id = _id(sink, "id")
        if sink_id in source_ids:
            raise _error("invalid_network", f"duplicate source/sink id {sink_id!r}")
        source_ids.add(sink_id)
        junction_id = check_junction(sink.get("junction"), f"network.sinks[{idx}].junction")
        mdot = _number(
            sink.get("mdot_kg_per_s"),
            f"network.sinks[{idx}].mdot_kg_per_s",
            strict_positive=True,
            maximum=1_000,
        )
        pp.create_sink(net, junction=junction_index[junction_id], mdot_kg_per_s=mdot, name=sink_id)

    pp.create_ext_grid(
        net,
        junction=junction_index[reference],
        p_bar=reference_pressure,
        t_k=reference_temperature,
        name="__energy_agent_tools_reference__",
        type="pt",
    )
    try:
        pp.pipeflow(net, mode="hydraulics", max_iter_hyd=50, tol_p=1e-6, tol_m=1e-6)
    except Exception as exc:
        raise _error("pipeflow_failed", "pandapipes failed to solve the supplied network") from exc
    if not bool(net.get("converged", False)):
        raise _error("pipeflow_not_converged", "pandapipes did not converge for this network")

    junctions_out: list[Json] = []
    for junction_id in ordered_junctions:
        row = net.res_junction.loc[junction_index[junction_id]]
        junctions_out.append(
            {
                "id": junction_id,
                "p_bar": float(row.p_bar),
                "t_k": float(row.t_k) if hasattr(row, "t_k") else None,
            }
        )
    pipes_out: list[Json] = []
    for idx, raw_pipe in enumerate(pipes):
        pipe = _mapping(raw_pipe, f"network.pipes[{idx}]")
        row = net.res_pipe.loc[idx]
        pipes_out.append(
            {
                "id": str(pipe["id"]),
                "from_junction": str(pipe["from_junction"]),
                "to_junction": str(pipe["to_junction"]),
                "p_from_bar": float(row.p_from_bar),
                "p_to_bar": float(row.p_to_bar),
                "mdot_from_kg_per_s": float(row.mdot_from_kg_per_s),
                "mdot_to_kg_per_s": float(row.mdot_to_kg_per_s),
                "v_mean_m_per_s": float(row.v_mean_m_per_s),
            }
        )
    external_mdot = float(net.res_ext_grid.iloc[0].mdot_kg_per_s)
    supplied_mdot = sum(
        _number(_mapping(item, f"network.sources[{idx}]").get("mdot_kg_per_s"), "mdot_kg_per_s")
        for idx, item in enumerate(sources)
    )
    demand_mdot = sum(
        _number(_mapping(item, f"network.sinks[{idx}]").get("mdot_kg_per_s"), "mdot_kg_per_s")
        for idx, item in enumerate(sinks)
    )
    return EnergyResult(
        data={
            "converged": True,
            "fluid": fluid,
            "junctions": junctions_out,
            "pipes": pipes_out,
            "totals": {
                "source_mdot_kg_per_s": supplied_mdot,
                "sink_mdot_kg_per_s": demand_mdot,
                "external_grid_mdot_kg_per_s": external_mdot,
                # pandapipes reports external-grid flow as negative when it
                # supplies the network (and positive when it removes flow).
                "mass_balance_error_kg_per_s": supplied_mdot - external_mdot - demand_mdot,
            },
        },
        kind=DataKind.SIMULATED,
        unit="bar, K, kg/s, m/s",
        source="pandapipes",
        timezone="UTC",
        resolution="steady-state",
        assumptions=[
            "pandapipes hydraulic pipeflow on an operator-supplied topology",
            "the selected standard fluid preset determines compressibility and density",
            "junction pressures are absolute bar and pipe lengths/diameters are km/metres respectively",
        ],
        warnings=[
            "This is a bounded engineering simulation and is not a certification of network safety."
        ],
        quality="converged-pipeflow",
        provenance=_solver_provenance("pandapipes", _version(pp), "hydraulic pipeflow"),
    )


def _pypsa_available() -> bool:
    return importlib.util.find_spec("pypsa") is not None


def _pandapipes_available() -> bool:
    return importlib.util.find_spec("pandapipes") is not None


def register(registry: Registry) -> None:
    """Register optional PyPSA and pandapipes tools without importing solvers."""

    registry.add_toolkit(
        Toolkit(
            id=_PYPSA_TOOLKIT,
            name="PyPSA",
            description="Bounded explicit-network AC power flow using PyPSA.",
            runtime="python",
            status="experimental" if _pypsa_available() else "unavailable",
            docs_url=PYPSA_DOCS,
            categories=["power flow", "grid", "network simulation"],
        )
    )
    registry.add(
        Tool(
            name="pypsa.power_flow",
            toolkit=_PYPSA_TOOLKIT,
            description="Solve an explicit bounded AC network with PyPSA using bus kV, line ohms, load MW, and a declared slack bus.",
            input_schema=_pypsa_schema(),
            capabilities=[
                "power flow",
                "AC network",
                "PyPSA",
                "voltage",
                "line losses",
                "grid simulation",
            ],
            actions={Action.SIMULATE},
            result_kind=DataKind.SIMULATED,
            result_unit="MW, Mvar, pu, degree",
            dependencies=["pypsa"],
        ),
        pypsa_power_flow,
    )
    registry.add_toolkit(
        Toolkit(
            id=_PANDAPIPES_TOOLKIT,
            name="pandapipes",
            description="Bounded explicit water or gas hydraulic pipeflow using pandapipes.",
            runtime="python",
            status="experimental" if _pandapipes_available() else "unavailable",
            docs_url=PANDAPIPES_DOCS,
            categories=["pipeflow", "thermal network", "gas network", "water network"],
        )
    )
    registry.add(
        Tool(
            name="pandapipes.pipeflow",
            toolkit=_PANDAPIPES_TOOLKIT,
            description="Solve an explicit bounded water or gas network with fixed junction pressure, pipes, sources, and sinks.",
            input_schema=_pandapipes_schema(),
            capabilities=["pipeflow", "hydraulic network", "water", "gas", "pressure", "mass flow"],
            actions={Action.SIMULATE},
            result_kind=DataKind.SIMULATED,
            result_unit="bar, K, kg/s, m/s",
            dependencies=["pandapipes"],
        ),
        pandapipes_pipeflow,
    )


__all__ = ["pandapipes_pipeflow", "pypsa_power_flow", "register"]
