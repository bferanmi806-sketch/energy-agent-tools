"""Local engineering calculations and simulations.

The connectors in this module deliberately accept complete, explicit inputs.  They do
not fetch weather, tariffs, or a network from the internet and they never execute user
supplied Python.  Optional heavy dependencies are imported inside handlers so the
base registry can still be loaded on installations that do not include the
``engineering`` extra.
"""

from __future__ import annotations

from datetime import datetime
from math import cos, isfinite, radians
from typing import Any
from zoneinfo import ZoneInfo

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

_TOOLKIT_ID = "engineering"
_UTC = "UTC"
_MAX_WEATHER_ROWS = 10_000
_MAX_NETWORK_BUSES = 500
_MAX_NETWORK_LINES = 2_000
_MAX_NETWORK_ELEMENTS = 5_000
_MAX_BATTERY_INTERVALS = 2_000
_SCHEMA_MAX_BATTERY_INTERVALS = 672
_BATTERY_SOLVER_TIME_LIMIT_S = 30.0


def _error(code: str, message: str, *, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _finite_number(
    value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid_input", f"{name} must be a number")
    result = float(value)
    if not isfinite(result):
        raise _error("invalid_input", f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise _error("invalid_input", f"{name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise _error("invalid_input", f"{name} must be <= {maximum}")
    return result


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("invalid_input", f"{name} must be an object")
    return value


def _required_list(
    value: Any, name: str, *, nonempty: bool = True, max_items: int | None = None
) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " and must not be empty" if nonempty else ""
        raise _error("invalid_input", f"{name} must be a list{suffix}")
    if max_items is not None and len(value) > max_items:
        raise _error("input_too_large", f"{name} cannot contain more than {max_items} items")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("invalid_timestamp", f"{name} is not an ISO-8601 timestamp") from exc
    else:
        raise _error("invalid_timestamp", f"{name} must be an ISO-8601 timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise _error("invalid_timestamp", f"{name} must include an explicit timezone offset")
    return result


def _zone(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error("invalid_input", f"{name} must be an IANA timezone name")
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise _error("invalid_input", f"{name} is not a valid IANA timezone") from exc
    return value


def _optional_number(
    mapping: dict[str, Any],
    *names: str,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return _finite_number(mapping[name], name, minimum=minimum, maximum=maximum)
    return default


def _number(
    mapping: dict[str, Any],
    *names: str,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    result = _optional_number(mapping, *names, default=default, minimum=minimum, maximum=maximum)
    if result is None:
        joined = " or ".join(names)
        raise _error("invalid_input", f"{joined} must be provided")
    return result


def _dependency_error(package: str, exc: Exception) -> EnergyError:
    return _error(
        "dependency_unavailable",
        f"{package} is required for this engineering tool; install the engineering extra",
    )


def _source_provenance(library: str, version: str, model: str) -> list[Json]:
    return [{"kind": "software", "library": library, "version": version, "model": model}]


def _pv_schema() -> Json:
    row = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "ghi_w_m2": {"type": "number"},
            "ghi": {"type": "number"},
            "dni_w_m2": {"type": ["number", "null"]},
            "dni": {"type": ["number", "null"]},
            "dhi_w_m2": {"type": ["number", "null"]},
            "dhi": {"type": ["number", "null"]},
            "temp_air_c": {"type": "number"},
            "temperature_c": {"type": "number"},
            "temp_cell_c": {"type": "number"},
            "wind_speed_m_s": {"type": "number", "minimum": 0},
            "duration_hours": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["timestamp"],
        "additionalProperties": False,
    }
    return schema(
        {
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "timezone": {"type": "string"},
            "elevation_m": {"type": "number", "minimum": -500, "maximum": 10000},
            "surface_tilt_deg": {"type": "number", "minimum": 0, "maximum": 180},
            "surface_azimuth_deg": {"type": "number", "minimum": 0, "maximum": 360},
            "dc_capacity_kw": {"type": "number", "exclusiveMinimum": 0},
            "inverter_capacity_kw": {"type": "number", "exclusiveMinimum": 0},
            "weather_source": {"type": "string"},
            "weather_kind": {"enum": [kind.value for kind in DataKind]},
            "albedo": {"type": "number", "minimum": 0, "maximum": 1},
            "losses_fraction": {"type": "number", "minimum": 0, "maximum": 1},
            "gamma_pdc_per_c": {"type": "number", "maximum": 0},
            "interval_hours": {"type": "number", "exclusiveMinimum": 0},
            "weather_rows": {
                "type": "array",
                "items": row,
                "minItems": 1,
                "maxItems": _MAX_WEATHER_ROWS,
            },
            "rows": {"type": "array", "items": row, "minItems": 1, "maxItems": _MAX_WEATHER_ROWS},
            "timestamps": {
                "type": "array",
                "items": {"type": "string", "format": "date-time"},
                "maxItems": _MAX_WEATHER_ROWS,
            },
            "irradiance": {
                "type": "array",
                "items": {"type": "object"},
                "maxItems": _MAX_WEATHER_ROWS,
            },
        },
        required=["latitude", "longitude", "timezone", "dc_capacity_kw"],
    )


def _power_flow_schema() -> Json:
    bus = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer"]},
            "name": {"type": "string"},
            "vn_kv": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["id", "vn_kv"],
        "additionalProperties": False,
    }
    line = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer"]},
            "from_bus": {"type": ["string", "integer"]},
            "to_bus": {"type": ["string", "integer"]},
            "length_km": {"type": "number", "exclusiveMinimum": 0},
            "r_ohm_per_km": {"type": "number", "minimum": 0},
            "x_ohm_per_km": {"type": "number", "minimum": 0},
            "c_nf_per_km": {"type": "number", "minimum": 0},
            "max_i_ka": {"type": "number", "exclusiveMinimum": 0},
            "max_loading_percent": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["id", "from_bus", "to_bus", "length_km", "r_ohm_per_km", "x_ohm_per_km"],
        "additionalProperties": False,
    }
    load = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer"]},
            "bus": {"type": ["string", "integer"]},
            "p_mw": {"type": "number"},
            "q_mvar": {"type": "number"},
        },
        "required": ["id", "bus", "p_mw"],
        "additionalProperties": False,
    }
    ext_grid = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer"]},
            "bus": {"type": ["string", "integer"]},
            "vm_pu": {"type": "number", "exclusiveMinimum": 0},
            "va_degree": {"type": "number"},
        },
        "required": ["bus"],
        "additionalProperties": False,
    }
    generator = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer"]},
            "bus": {"type": ["string", "integer"]},
            "p_mw": {"type": "number"},
            "vm_pu": {"type": "number", "exclusiveMinimum": 0},
            "q_mvar": {"type": "number"},
            "min_q_mvar": {"type": "number"},
            "max_q_mvar": {"type": "number"},
        },
        "required": ["id", "bus", "p_mw"],
        "additionalProperties": False,
    }
    network = {
        "type": "object",
        "properties": {
            "sn_mva": {"type": "number", "exclusiveMinimum": 0},
            "f_hz": {"type": "number", "exclusiveMinimum": 0},
            "buses": {"type": "array", "items": bus, "minItems": 1, "maxItems": _MAX_NETWORK_BUSES},
            "lines": {"type": "array", "items": line, "maxItems": _MAX_NETWORK_LINES},
            "loads": {"type": "array", "items": load, "maxItems": _MAX_NETWORK_ELEMENTS},
            "ext_grid": {"type": "array", "items": ext_grid, "maxItems": 32},
            "external_grids": {"type": "array", "items": ext_grid, "maxItems": 32},
            "slack_bus": {"type": ["string", "integer"]},
            "generators": {"type": "array", "items": generator, "maxItems": _MAX_NETWORK_ELEMENTS},
        },
        "required": ["buses"],
        "additionalProperties": False,
    }
    return schema({"network": network})


def _heat_loss_schema() -> Json:
    component = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "area_m2": {"type": "number", "exclusiveMinimum": 0},
            "u_value_w_m2k": {"type": "number", "minimum": 0},
        },
        "required": ["area_m2", "u_value_w_m2k"],
        "additionalProperties": False,
    }
    return schema(
        {
            "components": {"type": "array", "items": component, "minItems": 1},
            "geometry": {"type": "object"},
            "indoor_temp_c": {"type": "number"},
            "outdoor_temp_c": {"type": "number"},
            "volume_m3": {"type": "number", "minimum": 0},
            "air_changes_per_hour": {"type": "number", "minimum": 0},
            "heat_recovery_efficiency": {"type": "number", "minimum": 0, "maximum": 1},
            "thermal_bridge_w_per_k": {"type": "number", "minimum": 0},
            "internal_gains_kw": {"type": "number", "minimum": 0},
            "solar_gains_kw": {"type": "number", "minimum": 0},
            "duration_hours": {"type": "number", "exclusiveMinimum": 0},
            "design_margin_fraction": {"type": "number", "minimum": 0, "maximum": 1},
        },
        required=["indoor_temp_c", "outdoor_temp_c"],
    )


def _battery_schema() -> Json:
    interval = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "duration_hours": {"type": "number", "exclusiveMinimum": 0},
            "load_kw": {"type": "number", "minimum": 0},
            "pv_kw": {"type": "number", "minimum": 0},
            "price_per_kwh": {"type": "number"},
            "export_price_per_kwh": {"type": "number"},
            "carbon_intensity_g_per_kwh": {"type": "number", "minimum": 0},
        },
        "required": ["timestamp", "duration_hours", "load_kw", "pv_kw", "price_per_kwh"],
        "additionalProperties": False,
    }
    battery = {
        "type": "object",
        "properties": {
            "capacity_kwh": {"type": "number", "exclusiveMinimum": 0},
            "initial_soc_kwh": {"type": "number", "minimum": 0},
            "min_soc_kwh": {"type": "number", "minimum": 0},
            "max_charge_kw": {"type": "number", "exclusiveMinimum": 0},
            "max_discharge_kw": {"type": "number", "exclusiveMinimum": 0},
            "charge_efficiency": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            "discharge_efficiency": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            "target_final_soc_kwh": {"type": "number", "minimum": 0},
        },
        "required": ["capacity_kwh", "initial_soc_kwh", "max_charge_kw", "max_discharge_kw"],
        "additionalProperties": False,
    }
    return schema(
        {
            "intervals": {
                "type": "array",
                "items": interval,
                "minItems": 1,
                "maxItems": _SCHEMA_MAX_BATTERY_INTERVALS,
            },
            "battery": battery,
            "timezone": {"type": "string"},
            "objective": {"enum": ["cost", "carbon", "cost_and_carbon"]},
            "carbon_price_gbp_per_tonne": {"type": "number", "minimum": 0},
            "carbon_weight": {"type": "number", "minimum": 0},
            "carbon_species": {"type": "string", "enum": ["CO2", "CO2e"], "default": "CO2e"},
            "allow_grid_charging": {"type": "boolean"},
            "allow_grid_export": {"type": "boolean"},
        },
        required=["intervals", "battery"],
    )


async def estimate_solar_generation(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Estimate AC PV output from explicit weather rows using pvlib PVWatts."""
    del ctx
    try:
        import pandas as pd
        import pvlib
        from pvlib import inverter, irradiance, pvsystem, temperature
        from pvlib.location import Location
    except Exception as exc:  # pragma: no cover - exercised when the optional extra is absent
        raise _dependency_error("pvlib", exc) from exc

    args = _required_mapping(args, "args")
    latitude = _finite_number(args.get("latitude"), "latitude", minimum=-90, maximum=90)
    longitude = _finite_number(args.get("longitude"), "longitude", minimum=-180, maximum=180)
    timezone = _zone(args.get("timezone"), "timezone")
    elevation = _number(args, "elevation_m", default=0.0, minimum=-500, maximum=10000)
    tilt = _number(
        args, "surface_tilt_deg", default=latitude if latitude > 0 else 30.0, minimum=0, maximum=180
    )
    azimuth = _number(args, "surface_azimuth_deg", default=180.0, minimum=0, maximum=360)
    dc_capacity = _finite_number(args.get("dc_capacity_kw"), "dc_capacity_kw", minimum=0.000001)
    inverter_capacity = _number(args, "inverter_capacity_kw", default=dc_capacity, minimum=0.000001)
    albedo = _number(args, "albedo", default=0.2, minimum=0, maximum=1)
    losses = _number(args, "losses_fraction", default=0.14, minimum=0, maximum=1)
    gamma = _number(args, "gamma_pdc_per_c", default=-0.004, maximum=0)
    weather_source = args.get("weather_source", "caller-supplied weather rows")
    if not isinstance(weather_source, str) or not weather_source:
        raise _error("invalid_input", "weather_source must be a non-empty string")
    weather_kind = args.get("weather_kind", "unknown")
    if weather_kind != "unknown" and weather_kind not in {kind.value for kind in DataKind}:
        raise _error("invalid_input", "weather_kind must be a valid data kind")

    rows_value = args.get("weather_rows", args.get("rows"))
    if rows_value is None and "timestamps" in args and "irradiance" in args:
        timestamp_values = _required_list(args["timestamps"], "timestamps")
        irradiance_values = _required_list(args["irradiance"], "irradiance")
        if len(timestamp_values) != len(irradiance_values):
            raise _error("invalid_input", "timestamps and irradiance must have the same length")
        rows_value = [
            {"timestamp": timestamp, **_required_mapping(row, f"irradiance[{idx}]")}
            for idx, (timestamp, row) in enumerate(
                zip(timestamp_values, irradiance_values, strict=True)
            )
        ]
    rows = _required_list(rows_value, "weather_rows", max_items=_MAX_WEATHER_ROWS)

    parsed_timestamps: list[datetime] = []
    records: list[dict[str, Any]] = []
    assumptions: list[str] = [
        "pvlib PVWatts DC and inverter models are used with a fixed-tilt array",
        "14% aggregate system losses, 20% ground albedo, and Faiman cell-temperature coefficients are defaults unless supplied",
        "no shading, soiling, mismatch, snow, clipping beyond the inverter model, or curtailment is modeled",
    ]
    for idx, raw in enumerate(rows):
        row = _required_mapping(raw, f"weather_rows[{idx}]")
        timestamp = _timestamp(row.get("timestamp"), f"weather_rows[{idx}].timestamp")
        parsed_timestamps.append(timestamp.astimezone(ZoneInfo(timezone)))
        ghi_value = _optional_number(row, "ghi_w_m2", "ghi", minimum=0)
        if ghi_value is None:
            raise _error("invalid_input", f"weather_rows[{idx}] requires ghi_w_m2 or ghi")
        records.append({"ghi": max(0.0, ghi_value), "row": row})

    index = pd.DatetimeIndex(parsed_timestamps)
    if not index.is_monotonic_increasing:
        order = index.argsort()
        index = index[order]
        records = [records[int(i)] for i in order]
        assumptions.append("weather rows were sorted chronologically before modeling")
    if index.has_duplicates:
        raise _error("invalid_input", "weather timestamps must be unique")

    location = Location(latitude, longitude, tz=timezone, altitude=elevation)
    solar_position = location.get_solarposition(index)
    zenith = solar_position["apparent_zenith"]
    solar_azimuth = solar_position["azimuth"]
    ghi_series = pd.Series([record["ghi"] for record in records], index=index, dtype=float)

    dni_values: list[float | None] = []
    dhi_values: list[float | None] = []
    for record in records:
        row = record["row"]
        dni_values.append(_optional_number(row, "dni_w_m2", "dni", minimum=0))
        dhi_values.append(_optional_number(row, "dhi_w_m2", "dhi", minimum=0))
    dni = pd.Series(dni_values, index=index, dtype=float)
    dhi = pd.Series(dhi_values, index=index, dtype=float)
    missing_dni = dni.isna()
    missing_dhi = dhi.isna()
    if missing_dni.any():
        # DISC is pvlib's decomposition model.  It is used only where the caller did
        # not provide DNI, preserving supplied measurements verbatim (after clipping).
        try:
            disc = irradiance.disc(ghi_series, zenith, index)
            dni.loc[missing_dni] = disc["dni"].loc[missing_dni].fillna(0.0)
        except Exception as exc:
            raise _error("invalid_irradiance", "pvlib could not decompose GHI into DNI") from exc
        assumptions.append("missing DNI was estimated with pvlib DISC from GHI and solar zenith")
    if missing_dhi.any():
        direct_horizontal = dni * pd.Series(
            [max(0.0, cos(radians(float(z)))) for z in zenith], index=index
        )
        dhi.loc[missing_dhi] = (ghi_series - direct_horizontal).clip(lower=0.0).loc[missing_dhi]
        assumptions.append("missing DHI was calculated as GHI minus horizontal direct irradiance")
    dni = dni.fillna(0.0).clip(lower=0.0)
    dhi = dhi.fillna(0.0).clip(lower=0.0)

    poa = irradiance.get_total_irradiance(
        surface_tilt=float(tilt),
        surface_azimuth=float(azimuth),
        solar_zenith=zenith,
        solar_azimuth=solar_azimuth,
        dni=dni,
        ghi=ghi_series,
        dhi=dhi,
        albedo=float(albedo),
    )["poa_global"].clip(lower=0.0)

    temp_air_values: list[float] = []
    wind_values: list[float] = []
    temp_cell_values: list[float | None] = []
    duration_values: list[float | None] = []
    for record in records:
        row = record["row"]
        temp_air_values.append(_number(row, "temp_air_c", "temperature_c", default=20.0))
        wind_values.append(_number(row, "wind_speed_m_s", default=1.0, minimum=0))
        temp_cell_values.append(_optional_number(row, "temp_cell_c"))
        duration_values.append(_optional_number(row, "duration_hours"))
    temp_air = pd.Series(temp_air_values, index=index, dtype=float)
    wind_speed = pd.Series(wind_values, index=index, dtype=float)
    explicit_cell = pd.Series(temp_cell_values, index=index, dtype=float)
    cell_temperature = explicit_cell.copy()
    inferred_cell = cell_temperature.isna()
    if inferred_cell.any():
        cell_temperature.loc[inferred_cell] = temperature.faiman(
            poa.loc[inferred_cell], temp_air.loc[inferred_cell], wind_speed.loc[inferred_cell]
        )
    if explicit_cell.isna().all():
        assumptions.append(
            "cell temperature was estimated with pvlib Faiman from POA, air temperature, and wind"
        )
    pdc = pvsystem.pvwatts_dc(
        effective_irradiance=poa,
        temp_cell=cell_temperature,
        pdc0=float(dc_capacity) * 1000.0,
        gamma_pdc=float(gamma),
    ).clip(lower=0.0)
    pac = inverter.pvwatts(
        pdc=pdc,
        pdc0=float(inverter_capacity) * 1000.0,
        eta_inv_nom=0.96,
        eta_inv_ref=0.9637,
    )
    pac = (pac.clip(lower=0.0) * (1.0 - float(losses))).clip(
        upper=float(inverter_capacity) * 1000.0
    )

    default_interval = _number(args, "interval_hours", default=1.0, minimum=0.000001)
    durations: list[float] = []
    for idx, duration in enumerate(duration_values):
        if duration is not None:
            durations.append(
                _finite_number(duration, f"weather_rows[{idx}].duration_hours", minimum=0.000001)
            )
        elif idx + 1 < len(index):
            delta_hours = (index[idx + 1] - index[idx]).total_seconds() / 3600.0
            durations.append(delta_hours if delta_hours > 0 else float(default_interval))
        else:
            durations.append(float(default_interval))

    intervals: list[Json] = []
    for idx, timestamp in enumerate(index):
        intervals.append(
            {
                "timestamp": timestamp.isoformat(),
                "ghi_w_m2": float(ghi_series.iloc[idx]),
                "dni_w_m2": float(dni.iloc[idx]),
                "dhi_w_m2": float(dhi.iloc[idx]),
                "solar_zenith_deg": float(zenith.iloc[idx]),
                "poa_global_w_m2": float(poa.iloc[idx]),
                "cell_temperature_c": float(cell_temperature.iloc[idx]),
                "dc_power_kw": float(pdc.iloc[idx]) / 1000.0,
                "ac_power_kw": float(pac.iloc[idx]) / 1000.0,
                "duration_hours": durations[idx],
                "energy_kwh": float(pac.iloc[idx]) / 1000.0 * durations[idx],
            }
        )
    total_energy = sum(item["energy_kwh"] for item in intervals)
    if all(item["ac_power_kw"] <= 1e-9 for item in intervals):
        assumptions.append(
            "all modeled intervals have zero AC output because irradiance is below the PV model threshold"
        )
    return EnergyResult(
        data={
            "site": {
                "latitude": latitude,
                "longitude": longitude,
                "timezone": timezone,
                "elevation_m": elevation,
            },
            "system": {
                "dc_capacity_kw": dc_capacity,
                "inverter_capacity_kw": inverter_capacity,
                "surface_tilt_deg": tilt,
                "surface_azimuth_deg": azimuth,
            },
            "intervals": intervals,
            "total_energy_kwh": total_energy,
            "peak_ac_kw": max((item["ac_power_kw"] for item in intervals), default=0.0),
        },
        kind=DataKind.ESTIMATED,
        unit="kW_ac and kWh",
        source="pvlib",
        timezone=timezone,
        resolution="input interval",
        assumptions=assumptions,
        warnings=[],
        quality="model-estimate",
        provenance=[
            *_source_provenance("pvlib", pvlib.__version__, "PVWatts + DISC + Faiman"),
            {
                "kind": "input",
                "source": weather_source,
                "data_kind": weather_kind,
                "rows": len(intervals),
            },
        ],
    )


def _network_value(args: dict[str, Any]) -> dict[str, Any]:
    network = args.get("network", args)
    return _required_mapping(network, "network")


def _resolve_id(value: Any, known: dict[str, int], name: str) -> tuple[str, int]:
    key = str(value)
    if key not in known:
        raise _error("invalid_network", f"{name} refers to unknown bus {value!r}")
    return key, known[key]


async def run_power_flow(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Run an AC Newton-Raphson load flow on the caller's explicit pandapower network."""
    del ctx
    try:
        import pandapower as pp
    except Exception as exc:  # pragma: no cover - exercised when the optional extra is absent
        raise _dependency_error("pandapower", exc) from exc

    args = _required_mapping(args, "args")
    network = _network_value(args)
    bus_specs = _required_list(network.get("buses"), "network.buses", max_items=_MAX_NETWORK_BUSES)
    line_specs = network.get("lines", [])
    load_specs = network.get("loads", [])
    grid_specs = network.get("ext_grid", network.get("external_grids", []))
    if grid_specs is None:
        grid_specs = []
    if isinstance(grid_specs, dict):
        grid_specs = [grid_specs]
    generator_specs = network.get("generators", [])
    if (
        not isinstance(line_specs, list)
        or not isinstance(load_specs, list)
        or not isinstance(generator_specs, list)
    ):
        raise _error("invalid_network", "lines, loads, and generators must be lists")
    if len(line_specs) > _MAX_NETWORK_LINES:
        raise _error(
            "input_too_large", f"network.lines cannot contain more than {_MAX_NETWORK_LINES} items"
        )
    if len(load_specs) > _MAX_NETWORK_ELEMENTS:
        raise _error(
            "input_too_large",
            f"network.loads cannot contain more than {_MAX_NETWORK_ELEMENTS} items",
        )
    if len(generator_specs) > _MAX_NETWORK_ELEMENTS:
        raise _error(
            "input_too_large",
            f"network.generators cannot contain more than {_MAX_NETWORK_ELEMENTS} items",
        )
    if not isinstance(grid_specs, list):
        raise _error("invalid_network", "ext_grid must be a list or object")
    if len(grid_specs) > 32:
        raise _error("input_too_large", "network.ext_grid cannot contain more than 32 items")

    bus_ids: dict[str, int] = {}
    bus_labels: list[str] = []
    try:
        sn_mva = _number(network, "sn_mva", default=100.0, minimum=0.000001)
        f_hz = _number(network, "f_hz", default=50.0, minimum=0.000001)
        net = pp.create_empty_network(sn_mva=sn_mva, f_hz=f_hz)
        for idx, raw in enumerate(bus_specs):
            bus = _required_mapping(raw, f"network.buses[{idx}]")
            if "id" not in bus:
                raise _error("invalid_network", f"network.buses[{idx}] requires id")
            key = str(bus["id"])
            if key in bus_ids:
                raise _error("invalid_network", f"duplicate bus id {bus['id']!r}")
            vn_kv = _finite_number(
                bus.get("vn_kv"), f"network.buses[{idx}].vn_kv", minimum=0.000001
            )
            bus_ids[key] = int(pp.create_bus(net, vn_kv=vn_kv, name=str(bus.get("name", key))))
            bus_labels.append(key)

        seen_line_ids: set[str] = set()
        for idx, raw in enumerate(line_specs):
            line = _required_mapping(raw, f"network.lines[{idx}]")
            if "id" not in line:
                raise _error("invalid_network", f"network.lines[{idx}] requires id")
            line_id = str(line["id"])
            if line_id in seen_line_ids:
                raise _error("invalid_network", f"duplicate line id {line_id!r}")
            seen_line_ids.add(line_id)
            _, from_bus = _resolve_id(
                line.get("from_bus"), bus_ids, f"network.lines[{idx}].from_bus"
            )
            _, to_bus = _resolve_id(line.get("to_bus"), bus_ids, f"network.lines[{idx}].to_bus")
            if from_bus == to_bus:
                raise _error(
                    "invalid_network", f"network.lines[{idx}] cannot connect a bus to itself"
                )
            length = _finite_number(
                line.get("length_km"), f"network.lines[{idx}].length_km", minimum=0.000001
            )
            r = _finite_number(
                line.get("r_ohm_per_km"), f"network.lines[{idx}].r_ohm_per_km", minimum=0
            )
            x = _finite_number(
                line.get("x_ohm_per_km"), f"network.lines[{idx}].x_ohm_per_km", minimum=0
            )
            c = _number(line, "c_nf_per_km", default=0.0, minimum=0)
            max_i = _number(line, "max_i_ka", default=1.0, minimum=0.000001)
            pp.create_line_from_parameters(
                net,
                from_bus=from_bus,
                to_bus=to_bus,
                length_km=length,
                r_ohm_per_km=r,
                x_ohm_per_km=x,
                c_nf_per_km=c,
                max_i_ka=max_i,
                name=line_id,
                max_loading_percent=_number(
                    line, "max_loading_percent", default=100.0, minimum=0.000001
                ),
            )

        seen_load_ids: set[str] = set()
        for idx, raw in enumerate(load_specs):
            load = _required_mapping(raw, f"network.loads[{idx}]")
            load_id = str(load.get("id", f"load-{idx}"))
            if load_id in seen_load_ids:
                raise _error("invalid_network", f"duplicate load id {load_id!r}")
            seen_load_ids.add(load_id)
            _, bus_idx = _resolve_id(load.get("bus"), bus_ids, f"network.loads[{idx}].bus")
            p = _finite_number(load.get("p_mw"), f"network.loads[{idx}].p_mw")
            q = _number(load, "q_mvar", default=0.0)
            pp.create_load(net, bus=bus_idx, p_mw=p, q_mvar=q, name=load_id)

        generator_result_refs: list[tuple[int, str, int]] = []
        dispatchable_count = 0
        static_count = 0
        for idx, raw in enumerate(generator_specs):
            generator = _required_mapping(raw, f"network.generators[{idx}]")
            gen_id = str(generator.get("id", f"generator-{idx}"))
            _, bus_idx = _resolve_id(
                generator.get("bus"), bus_ids, f"network.generators[{idx}].bus"
            )
            p = _finite_number(generator.get("p_mw"), f"network.generators[{idx}].p_mw")
            if "q_mvar" in generator:
                # A fixed-P/Q static generator is useful for measured PV or
                # embedded generation.  A generator without q_mvar is modeled
                # as a pandapower PV generator controlled by vm_pu.
                pp.create_sgen(
                    net, bus=bus_idx, p_mw=p, q_mvar=_number(generator, "q_mvar"), name=gen_id
                )
                generator_result_refs.append((idx, "sgen", static_count))
                static_count += 1
            else:
                vm = _number(generator, "vm_pu", default=1.0, minimum=0.000001)
                pp.create_gen(
                    net,
                    bus=bus_idx,
                    p_mw=p,
                    vm_pu=vm,
                    min_q_mvar=_number(generator, "min_q_mvar", default=-1000.0),
                    max_q_mvar=_number(generator, "max_q_mvar", default=1000.0),
                    name=gen_id,
                )
                generator_result_refs.append((idx, "gen", dispatchable_count))
                dispatchable_count += 1

        if not grid_specs and network.get("slack_bus") is not None:
            grid_specs = [{"bus": network["slack_bus"]}]
        if not grid_specs:
            raise _error("invalid_network", "network requires at least one ext_grid or slack_bus")
        seen_grid_buses: set[int] = set()
        for idx, raw in enumerate(grid_specs):
            grid = _required_mapping(raw, f"network.ext_grid[{idx}]")
            _, grid_bus_idx = _resolve_id(grid.get("bus"), bus_ids, f"network.ext_grid[{idx}].bus")
            if grid_bus_idx in seen_grid_buses:
                raise _error("invalid_network", "a bus cannot have duplicate external grids")
            seen_grid_buses.add(grid_bus_idx)
            pp.create_ext_grid(
                net,
                bus=grid_bus_idx,
                vm_pu=_number(grid, "vm_pu", default=1.0, minimum=0.000001),
                va_degree=_number(grid, "va_degree", default=0.0),
                name=str(grid.get("id", f"grid-{idx}")),
            )
    except EnergyError:
        raise
    except Exception as exc:
        raise _error(
            "invalid_network", f"could not construct the pandapower network: {exc}"
        ) from exc

    try:
        pp.runpp(
            net,
            algorithm="nr",
            calculate_voltage_angles=True,
            init="auto",
            tolerance_mva=1e-8,
            max_iteration=50,
            numba=False,
        )
    except Exception as exc:
        not_converged = getattr(pp, "LoadflowNotConverged", ())
        if not_converged and isinstance(exc, not_converged):
            raise _error(
                "power_flow_not_converged", "pandapower did not converge for this network"
            ) from exc
        raise _error("power_flow_failed", f"pandapower failed to solve the network: {exc}") from exc
    if not bool(net.converged):
        raise _error("power_flow_not_converged", "pandapower did not converge for this network")

    buses_out: list[Json] = []
    for key, bus_idx in bus_ids.items():
        result = net.res_bus.loc[bus_idx]
        buses_out.append(
            {
                "id": key,
                "vm_pu": float(result.vm_pu),
                "va_degree": float(result.va_degree),
                "p_mw": float(result.p_mw),
                "q_mvar": float(result.q_mvar),
            }
        )
    lines_out: list[Json] = []
    for idx, raw in enumerate(line_specs):
        line = _required_mapping(raw, f"network.lines[{idx}]")
        result = net.res_line.loc[idx]
        p_from = float(result.p_from_mw)
        p_to = float(result.p_to_mw)
        q_from = float(result.q_from_mvar)
        q_to = float(result.q_to_mvar)
        lines_out.append(
            {
                "id": str(line["id"]),
                "from_bus": str(line["from_bus"]),
                "to_bus": str(line["to_bus"]),
                "p_from_mw": p_from,
                "q_from_mvar": q_from,
                "p_to_mw": p_to,
                "q_to_mvar": q_to,
                "loss_mw": p_from + p_to,
                "loss_mvar": q_from + q_to,
                "loading_percent": float(result.loading_percent),
            }
        )
    loads_out: list[Json] = []
    for idx, raw in enumerate(load_specs):
        load = _required_mapping(raw, f"network.loads[{idx}]")
        result = net.res_load.loc[idx]
        loads_out.append(
            {
                "id": str(load.get("id", f"load-{idx}")),
                "bus": str(load["bus"]),
                "p_mw": float(result.p_mw),
                "q_mvar": float(result.q_mvar),
            }
        )
    generation_out: list[Json] = []
    ext_grid_generation = 0.0
    ext_grid_reactive = 0.0
    for idx, raw in enumerate(grid_specs):
        grid = _required_mapping(raw, f"network.ext_grid[{idx}]")
        result = net.res_ext_grid.loc[idx]
        p = float(result.p_mw)
        q = float(result.q_mvar)
        ext_grid_generation += p
        ext_grid_reactive += q
        generation_out.append(
            {
                "id": str(grid.get("id", f"grid-{idx}")),
                "bus": str(grid["bus"]),
                "p_mw": p,
                "q_mvar": q,
                "kind": "external_grid",
            }
        )
    generator_generation = 0.0
    if len(generator_result_refs):
        for idx, result_kind, result_idx in generator_result_refs:
            raw = generator_specs[idx]
            generator = _required_mapping(raw, f"network.generators[{idx}]")
            result_table = net.res_gen if result_kind == "gen" else net.res_sgen
            result = result_table.loc[result_idx]
            p = float(result.p_mw)
            q = float(result.q_mvar)
            generator_generation += p
            generation_out.append(
                {
                    "id": str(generator.get("id", f"generator-{idx}")),
                    "bus": str(generator["bus"]),
                    "p_mw": p,
                    "q_mvar": q,
                    "kind": "generator" if result_kind == "gen" else "static_generator",
                }
            )
    line_loss = sum(item["loss_mw"] for item in lines_out)
    load_mw = sum(item["p_mw"] for item in loads_out)
    generation_mw = ext_grid_generation + generator_generation
    balance_error = generation_mw - load_mw - line_loss
    return EnergyResult(
        data={
            "converged": True,
            "buses": buses_out,
            "lines": lines_out,
            "loads": loads_out,
            "generation": generation_out,
            "totals": {
                "generation_mw": generation_mw,
                "load_mw": load_mw,
                "line_loss_mw": line_loss,
                "balance_error_mw": balance_error,
                "reactive_external_grid_mvar": ext_grid_reactive,
            },
        },
        kind=DataKind.SIMULATED,
        unit="MW, Mvar, pu",
        source="pandapower",
        timezone=_UTC,
        resolution="steady-state",
        assumptions=[
            "balanced three-phase AC Newton-Raphson power flow",
            "network topology, line impedances, loads, generators, and external grids were supplied by the caller",
            "line shunt capacitance defaults to zero and line thermal limit defaults to 100% when omitted",
        ],
        warnings=[],
        quality="converged-power-flow",
        provenance=_source_provenance("pandapower", pp.__version__, "AC Newton-Raphson"),
    )


def _components_from_heat_args(args: dict[str, Any]) -> list[dict[str, Any]]:
    components = args.get("components")
    if components is not None:
        return _required_list(components, "components")
    geometry = args.get("geometry")
    if not isinstance(geometry, dict):
        raise _error("invalid_input", "components or geometry must be provided")
    if isinstance(geometry.get("components"), list):
        return _required_list(geometry["components"], "geometry.components")
    derived: list[dict[str, Any]] = []
    for name in ("walls", "roof", "floor", "windows", "doors"):
        area = geometry.get(f"{name}_area_m2")
        u_value = geometry.get(f"{name}_u_value_w_m2k", geometry.get(f"{name}_u_value"))
        if area is not None or u_value is not None:
            if area is None or u_value is None:
                raise _error("invalid_input", f"geometry {name} requires area and U-value")
            derived.append({"name": name, "area_m2": area, "u_value_w_m2k": u_value})
    if not derived:
        raise _error("invalid_input", "geometry must include components or area/U-value pairs")
    return derived


async def calculate_heat_loss(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Calculate steady-state building heat loss from envelope and ventilation inputs."""
    del ctx
    args = _required_mapping(args, "args")
    indoor = _finite_number(args.get("indoor_temp_c"), "indoor_temp_c")
    outdoor = _finite_number(args.get("outdoor_temp_c"), "outdoor_temp_c")
    delta_t = indoor - outdoor
    components = _components_from_heat_args(args)
    component_out: list[Json] = []
    transmission_ua = 0.0
    for idx, raw in enumerate(components):
        component = _required_mapping(raw, f"components[{idx}]")
        area = _finite_number(component.get("area_m2"), f"components[{idx}].area_m2", minimum=0)
        u_value = _finite_number(
            component.get("u_value_w_m2k"), f"components[{idx}].u_value_w_m2k", minimum=0
        )
        ua = area * u_value
        transmission_ua += ua
        component_out.append(
            {
                "name": str(component.get("name", f"component-{idx}")),
                "area_m2": area,
                "u_value_w_m2k": u_value,
                "ua_w_per_k": ua,
                "heat_loss_w": max(0.0, ua * delta_t),
            }
        )

    volume = _optional_number(args, "volume_m3", default=None, minimum=0)
    ach = _number(args, "air_changes_per_hour", default=0.0, minimum=0)
    heat_recovery = _number(args, "heat_recovery_efficiency", default=0.0, minimum=0, maximum=1)
    ventilation_ua = 0.0
    if volume is not None and volume > 0 and ach > 0:
        # rho * cp * (volume * ACH / 3600) gives W/K.
        ventilation_ua = 1.2 * 1006.0 * volume * ach / 3600.0 * (1.0 - heat_recovery)
    thermal_bridge_ua = _number(args, "thermal_bridge_w_per_k", default=0.0, minimum=0)
    gross_loss_w = max(0.0, (transmission_ua + ventilation_ua + thermal_bridge_ua) * delta_t)
    internal_gains = _number(args, "internal_gains_kw", default=0.0, minimum=0)
    solar_gains = _number(args, "solar_gains_kw", default=0.0, minimum=0)
    gains_kw = float(internal_gains) + float(solar_gains)
    net_demand_kw = max(0.0, gross_loss_w / 1000.0 - gains_kw)
    margin = _number(args, "design_margin_fraction", default=0.0, minimum=0, maximum=1)
    design_capacity_kw = net_demand_kw * (1.0 + float(margin))
    duration = _number(args, "duration_hours", default=1.0, minimum=0.000001)
    assumptions = [
        "steady-state heat balance using U-values supplied by the caller",
        "air density is 1.2 kg/m³ and specific heat is 1006 J/(kg·K)",
        "heat recovery applies to ventilation losses only; thermal bridges are represented as a supplied aggregate W/K",
        "no solar or internal gains are assumed unless explicitly supplied",
    ]
    return EnergyResult(
        data={
            "temperature_difference_k": delta_t,
            "components": component_out,
            "transmission_ua_w_per_k": transmission_ua,
            "ventilation_ua_w_per_k": ventilation_ua,
            "thermal_bridge_ua_w_per_k": thermal_bridge_ua,
            "gross_heat_loss_kw": gross_loss_w / 1000.0,
            "gains_kw": gains_kw,
            "net_heating_demand_kw": net_demand_kw,
            "design_capacity_kw": design_capacity_kw,
            "energy_required_kwh": net_demand_kw * float(duration),
            "duration_hours": duration,
        },
        kind=DataKind.CALCULATED,
        unit="kW_th and kWh_th",
        source="energy-agent-tools:heat-loss",
        timezone=_UTC,
        resolution="steady-state",
        assumptions=assumptions,
        warnings=["This calculation is not a dynamic building simulation."],
        quality="engineering-calculation",
        provenance=[{"kind": "input", "components": len(component_out)}],
    )


def _battery_interval_value(
    interval: dict[str, Any], name: str, default: float | None = None, minimum: float | None = None
) -> float:
    value = interval.get(name, default)
    return _finite_number(value, name, minimum=minimum)


async def schedule_battery_charging(args: Json, ctx: ExecutionContext) -> EnergyResult:
    """Optimize a physically constrained battery schedule with a local MILP."""
    del ctx
    try:
        import numpy as np
        import scipy
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import lil_matrix
    except Exception as exc:  # pragma: no cover - exercised when scipy is unavailable
        raise _dependency_error("scipy", exc) from exc

    args = _required_mapping(args, "args")
    carbon_species = args.get("carbon_species", "CO2e")
    if carbon_species not in ("CO2", "CO2e"):
        raise _error("invalid_input", "carbon_species must be CO2 or CO2e")
    interval_specs = _required_list(
        args.get("intervals"), "intervals", max_items=_MAX_BATTERY_INTERVALS
    )
    battery = _required_mapping(args.get("battery"), "battery")
    n = len(interval_specs)
    parsed_timestamps: list[datetime] = []
    durations: list[float] = []
    loads: list[float] = []
    pv_values: list[float] = []
    prices: list[float] = []
    export_prices: list[float] = []
    carbon: list[float] = []
    for idx, raw in enumerate(interval_specs):
        interval = _required_mapping(raw, f"intervals[{idx}]")
        parsed_timestamps.append(
            _timestamp(interval.get("timestamp"), f"intervals[{idx}].timestamp")
        )
        durations.append(_battery_interval_value(interval, "duration_hours", minimum=0.000001))
        loads.append(_battery_interval_value(interval, "load_kw", minimum=0))
        pv_values.append(_battery_interval_value(interval, "pv_kw", minimum=0))
        prices.append(_battery_interval_value(interval, "price_per_kwh"))
        export_prices.append(_battery_interval_value(interval, "export_price_per_kwh", default=0.0))
        carbon.append(
            _battery_interval_value(interval, "carbon_intensity_g_per_kwh", default=0.0, minimum=0)
        )
    if any(
        left >= right for left, right in zip(parsed_timestamps, parsed_timestamps[1:], strict=False)
    ):
        raise _error("invalid_timestamp", "battery interval timestamps must be strictly increasing")

    capacity = _finite_number(battery.get("capacity_kwh"), "battery.capacity_kwh", minimum=0.000001)
    initial_soc = _finite_number(
        battery.get("initial_soc_kwh"), "battery.initial_soc_kwh", minimum=0
    )
    min_soc = _number(battery, "min_soc_kwh", default=0.0, minimum=0)
    max_charge = _finite_number(
        battery.get("max_charge_kw"), "battery.max_charge_kw", minimum=0.000001
    )
    max_discharge = _finite_number(
        battery.get("max_discharge_kw"), "battery.max_discharge_kw", minimum=0.000001
    )
    charge_efficiency = _number(
        battery, "charge_efficiency", default=0.95, minimum=0.000001, maximum=1
    )
    discharge_efficiency = _number(
        battery, "discharge_efficiency", default=0.95, minimum=0.000001, maximum=1
    )
    target_final = _number(battery, "target_final_soc_kwh", default=initial_soc, minimum=0)
    if min_soc > capacity or initial_soc > capacity or target_final > capacity:
        raise _error("invalid_input", "battery state of charge values must be within capacity")
    if initial_soc < min_soc:
        raise _error("invalid_input", "initial_soc_kwh cannot be below min_soc_kwh")
    objective = args.get("objective", "cost")
    if objective not in {"cost", "carbon", "cost_and_carbon"}:
        raise _error("invalid_input", "objective must be cost, carbon, or cost_and_carbon")
    allow_grid_charging = args.get("allow_grid_charging", True)
    allow_grid_export = args.get("allow_grid_export", False)
    if not isinstance(allow_grid_charging, bool) or not isinstance(allow_grid_export, bool):
        raise _error("invalid_input", "allow_grid_charging and allow_grid_export must be booleans")
    carbon_price = _number(args, "carbon_price_gbp_per_tonne", default=100.0, minimum=0)
    carbon_weight = _number(args, "carbon_weight", default=1.0, minimum=0)
    result_timezone = (
        _zone(args["timezone"], "timezone") if args.get("timezone") is not None else _UTC
    )

    # Variables are [charge, discharge, grid import, grid export, curtailment,
    # state of charge (n+1), mode binary] for each interval.  The binary mode
    # prevents physically impossible simultaneous charging and discharging.
    charge_idx = 0
    discharge_idx = n
    import_idx = 2 * n
    export_idx = 3 * n
    curtail_idx = 4 * n
    soc_idx = 5 * n
    mode_idx = 6 * n + 1
    variable_count = mode_idx + n
    lower = np.zeros(variable_count, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)
    for idx in range(n):
        lower[charge_idx + idx] = 0.0
        upper[charge_idx + idx] = max_charge
        if not allow_grid_charging:
            upper[charge_idx + idx] = min(max_charge, max(0.0, pv_values[idx] - loads[idx]))
        upper[discharge_idx + idx] = max_discharge
        # Bound grid exchange even when an input tariff is negative.  This keeps
        # the optimization finite and reflects the maximum useful exchange in an
        # interval; it also prevents an accidental import/export arbitrage loop.
        upper[import_idx + idx] = loads[idx] + max_charge
        upper[export_idx + idx] = (pv_values[idx] + max_discharge) if allow_grid_export else 0.0
        integrality[mode_idx + idx] = 1
        upper[mode_idx + idx] = 1.0
    lower[soc_idx : soc_idx + n + 1] = min_soc
    upper[soc_idx : soc_idx + n + 1] = capacity
    lower[soc_idx] = upper[soc_idx] = initial_soc
    lower[soc_idx + n] = max(min_soc, float(target_final))

    eq_count = 2 * n
    ub_count = 2 * n
    matrix_eq = lil_matrix((eq_count, variable_count), dtype=float)
    rhs_eq = np.zeros(eq_count, dtype=float)
    for idx, duration in enumerate(durations):
        # grid_import + discharge - charge - grid_export - curtail = load - pv
        matrix_eq[idx, import_idx + idx] = 1.0
        matrix_eq[idx, discharge_idx + idx] = 1.0
        matrix_eq[idx, charge_idx + idx] = -1.0
        matrix_eq[idx, export_idx + idx] = -1.0
        matrix_eq[idx, curtail_idx + idx] = -1.0
        rhs_eq[idx] = loads[idx] - pv_values[idx]
        # soc[t+1] - soc[t] - eta_c * charge * dt + discharge / eta_d * dt = 0
        row = n + idx
        matrix_eq[row, soc_idx + idx] = -1.0
        matrix_eq[row, soc_idx + idx + 1] = 1.0
        matrix_eq[row, charge_idx + idx] = -float(charge_efficiency) * duration
        matrix_eq[row, discharge_idx + idx] = duration / float(discharge_efficiency)
    matrix_ub = lil_matrix((ub_count, variable_count), dtype=float)
    rhs_ub = np.zeros(ub_count, dtype=float)
    for idx in range(n):
        # charge <= max_charge * mode
        matrix_ub[idx, charge_idx + idx] = 1.0
        matrix_ub[idx, mode_idx + idx] = -max_charge
        # discharge + max_discharge * mode <= max_discharge
        matrix_ub[n + idx, discharge_idx + idx] = 1.0
        matrix_ub[n + idx, mode_idx + idx] = max_discharge
        rhs_ub[n + idx] = max_discharge

    objective_coeff = np.zeros(variable_count, dtype=float)
    if objective in {"cost", "cost_and_carbon"}:
        for idx, duration in enumerate(durations):
            objective_coeff[import_idx + idx] += prices[idx] * duration
            objective_coeff[export_idx + idx] -= export_prices[idx] * duration
    if objective in {"carbon", "cost_and_carbon"}:
        carbon_coeff_scale = (
            1.0
            if objective == "carbon"
            else float(carbon_weight) * float(carbon_price) / 1_000_000.0
        )
        for idx, duration in enumerate(durations):
            objective_coeff[import_idx + idx] += carbon[idx] * duration * carbon_coeff_scale
    # A tiny curtailment penalty makes otherwise equivalent solutions deterministic.
    for idx, duration in enumerate(durations):
        objective_coeff[curtail_idx + idx] += 1e-7 * duration

    try:
        solution = milp(
            c=objective_coeff,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=[
                LinearConstraint(matrix_eq.tocsr(), rhs_eq, rhs_eq),
                LinearConstraint(matrix_ub.tocsr(), -np.inf, rhs_ub),
            ],
            options={"presolve": True, "time_limit": _BATTERY_SOLVER_TIME_LIMIT_S},
        )
    except Exception as exc:
        raise _error("battery_solver_failed", f"local battery optimizer failed: {exc}") from exc
    if not bool(solution.success) or solution.x is None:
        status = getattr(solution, "status", None)
        message = str(getattr(solution, "message", ""))
        if status == 1 or "time" in message.lower() or "limit" in message.lower():
            raise _error(
                "battery_solver_timeout",
                "battery optimizer exceeded its 30 second time limit",
                retryable=True,
            )
        raise _error("battery_infeasible", f"battery schedule is infeasible: {solution.message}")

    values = solution.x
    schedule: list[Json] = []
    baseline_cost = 0.0
    total_cost = 0.0
    total_carbon = 0.0
    for idx, duration in enumerate(durations):
        charge = max(0.0, float(values[charge_idx + idx]))
        discharge = max(0.0, float(values[discharge_idx + idx]))
        grid_import = max(0.0, float(values[import_idx + idx]))
        grid_export = max(0.0, float(values[export_idx + idx]))
        curtailment = max(0.0, float(values[curtail_idx + idx]))
        soc_end = float(values[soc_idx + idx + 1])
        interval_cost = (
            grid_import * duration * prices[idx] - grid_export * duration * export_prices[idx]
        )
        interval_carbon = grid_import * duration * carbon[idx]
        baseline_import = max(0.0, loads[idx] - pv_values[idx])
        baseline_export = max(0.0, pv_values[idx] - loads[idx]) if allow_grid_export else 0.0
        baseline_cost += (
            baseline_import * duration * prices[idx]
            - baseline_export * duration * export_prices[idx]
        )
        total_cost += interval_cost
        total_carbon += interval_carbon
        schedule.append(
            {
                "timestamp": parsed_timestamps[idx].isoformat(),
                "duration_hours": duration,
                "load_kw": loads[idx],
                "pv_kw": pv_values[idx],
                "price_per_kwh": prices[idx],
                "charge_kw": charge,
                "discharge_kw": discharge,
                "grid_import_kw": grid_import,
                "grid_export_kw": grid_export,
                "curtailment_kw": curtailment,
                "soc_kwh": soc_end,
                "cost": interval_cost,
                "carbon_g": interval_carbon,
            }
        )

    # Validate the solver result against the same physical equations returned to the caller.
    for idx, item in enumerate(schedule):
        if idx == 0:
            soc_before = initial_soc
        else:
            soc_before = schedule[idx - 1]["soc_kwh"]
        soc_expected = (
            soc_before
            + item["charge_kw"] * float(charge_efficiency) * durations[idx]
            - (item["discharge_kw"] / float(discharge_efficiency) * durations[idx])
        )
        if abs(soc_expected - item["soc_kwh"]) > 2e-5:
            raise _error(
                "battery_solver_failed", "optimizer returned an inconsistent state of charge"
            )
        balance = (
            item["grid_import_kw"]
            + item["pv_kw"]
            + item["discharge_kw"]
            - item["load_kw"]
            - item["charge_kw"]
            - item["grid_export_kw"]
            - item["curtailment_kw"]
        )
        if abs(balance) > 2e-5:
            raise _error(
                "battery_solver_failed", "optimizer returned an inconsistent power balance"
            )

    return EnergyResult(
        data={
            "schedule": schedule,
            "summary": {
                "initial_soc_kwh": initial_soc,
                "final_soc_kwh": schedule[-1]["soc_kwh"],
                "target_final_soc_kwh": target_final,
                "total_cost": total_cost,
                "baseline_cost_without_battery": baseline_cost,
                "cost_savings": baseline_cost - total_cost,
                "total_carbon_g": total_carbon,
                "carbon_species": carbon_species,
                "total_grid_import_kwh": sum(
                    item["grid_import_kw"] * item["duration_hours"] for item in schedule
                ),
                "total_grid_export_kwh": sum(
                    item["grid_export_kw"] * item["duration_hours"] for item in schedule
                ),
                "total_curtailed_kwh": sum(
                    item["curtailment_kw"] * item["duration_hours"] for item in schedule
                ),
            },
        },
        kind=DataKind.SIMULATED,
        unit=f"kW, kWh, currency, g{carbon_species}",
        field_units={"carbon_g": f"g{carbon_species}", "total_carbon_g": f"g{carbon_species}"},
        source="energy-agent-tools:battery-optimizer",
        # Interval timestamps may carry a fixed UTC offset (for example ``+01:00``),
        # which is not an IANA zone accepted by EnergyResult.  The result envelope
        # therefore uses UTC while preserving each original offset in the rows.
        timezone=result_timezone,
        resolution="input interval",
        assumptions=[
            "battery state transitions use the supplied charge/discharge efficiencies and power limits",
            "load and PV values are treated as interval-average real power",
            "grid import/export prices and carbon intensities are exogenous inputs; degradation and demand charges are omitted",
            f"Carbon totals retain the supplied {carbon_species} basis.",
            "a local mixed-integer linear optimizer prevents simultaneous charging and discharging",
        ],
        warnings=[],
        quality="feasible-optimized-schedule",
        provenance=_source_provenance("scipy", scipy.__version__, "MILP battery dispatch"),
    )


def register(registry: Registry) -> None:
    """Register the local engineering toolkit and its handlers."""
    registry.add_toolkit(
        Toolkit(
            id=_TOOLKIT_ID,
            name="Energy Engineering",
            description="Local pvlib, pandapower, and native engineering calculations",
            runtime="python",
            status="experimental",
            auth_required=False,
        )
    )
    registry.add(
        Tool(
            name="engineering.estimate_solar_generation",
            dependencies=["pvlib"],
            toolkit=_TOOLKIT_ID,
            description="Estimate fixed-tilt PV AC output from timezone-aware irradiance and weather rows",
            input_schema=_pv_schema(),
            capabilities=[
                "solar generation",
                "PV estimate",
                "pvlib",
                "irradiance",
                "weather",
                "estimate_solar_generation",
            ],
            actions={Action.CALCULATE, Action.SIMULATE},
        ),
        estimate_solar_generation,
    )
    registry.add(
        Tool(
            name="engineering.run_power_flow",
            dependencies=["pandapower"],
            toolkit=_TOOLKIT_ID,
            description="Run an AC power flow on explicit buses, lines, loads, generators, and external grids",
            input_schema=_power_flow_schema(),
            capabilities=[
                "power flow",
                "AC network",
                "pandapower",
                "voltage",
                "line losses",
                "run_power_flow",
                "run_simulation",
            ],
            actions={Action.SIMULATE},
        ),
        run_power_flow,
    )
    registry.add(
        Tool(
            name="engineering.calculate_heat_loss",
            toolkit=_TOOLKIT_ID,
            description="Calculate steady-state envelope, ventilation, and thermal-bridge heat loss",
            input_schema=_heat_loss_schema(),
            capabilities=[
                "heat loss",
                "building physics",
                "HVAC sizing",
                "thermal",
                "perform_engineering_calculation",
            ],
            actions={Action.CALCULATE},
        ),
        calculate_heat_loss,
    )
    registry.add(
        Tool(
            name="engineering.schedule_battery_charging",
            dependencies=["scipy"],
            toolkit=_TOOLKIT_ID,
            description="Optimize a feasible battery charge and discharge schedule against price or carbon",
            input_schema=_battery_schema(),
            capabilities=[
                "battery",
                "charging schedule",
                "tariff optimization",
                "carbon optimization",
                "storage",
                "plan_battery_charging",
                "perform_engineering_calculation",
            ],
            actions={Action.CALCULATE, Action.SIMULATE},
        ),
        schedule_battery_charging,
    )


__all__ = [
    "calculate_heat_loss",
    "estimate_solar_generation",
    "register",
    "run_power_flow",
    "schedule_battery_charging",
]
