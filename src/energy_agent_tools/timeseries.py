"""Pure, bounded operations for energy time series.

The workbench stores opaque result envelopes.  This module is the semantic
layer above those artifacts: it parses timestamps once, rejects ambiguous
units and duplicate instants, and records the operation and input lineage on
every derived result.  It deliberately has no session, filesystem, network,
or framework dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .models import DataKind, EnergyError, EnergyResult, Json, QuantityShape

MAX_ROWS = 100_000
MAX_OUTPUT_ROWS = 100_000
MAX_BASELINE_WINDOW = 10_000
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60

_FREQUENCY_SECONDS = {
    "15min": 15 * 60,
    "30min": 30 * 60,
    "1h": 60 * 60,
    "1D": 24 * 60 * 60,
}


@dataclass(frozen=True)
class _Unit:
    dimension: str
    to_base: float
    base: str
    currency: str | None = None
    species: str | None = None


@dataclass(frozen=True)
class _Series:
    artifact_id: str
    result: EnergyResult
    rows: tuple[dict[str, Any], ...]
    times: tuple[pd.Timestamp, ...]
    values: tuple[float | None, ...]
    timestamp: str
    column: str


def operate(
    operation: str,
    inputs: list[tuple[str, EnergyResult]],
    parameters: Json,
) -> EnergyResult:
    """Run one bounded workbench operation over in-memory result envelopes.

    ``inputs`` carries artifact IDs solely for lineage; this function never
    reads or writes artifacts.  The public operation names are intentionally
    small and stable because the MCP connector exposes this function as one
    closed-schema tool.
    """

    if not isinstance(operation, str) or operation not in {
        "filter",
        "missing",
        "counter",
        "integrate_power",
        "cost",
        "carbon",
        "baseline",
        "compare",
        "normalize",
        "align",
    }:
        raise EnergyError("invalid_operation", "Unsupported time-series operation.")
    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Operation parameters must be an object.")
    if not inputs:
        raise EnergyError("missing_input", "At least one input artifact is required.")
    if any(not isinstance(artifact_id, str) or not artifact_id for artifact_id, _ in inputs):
        raise EnergyError("invalid_input", "Each input must include an artifact identifier.")

    if operation == "filter":
        return _filter(inputs, parameters)
    if operation == "missing":
        return _missing(inputs, parameters)
    if operation == "counter":
        return _counter(inputs, parameters)
    if operation == "integrate_power":
        return _integrate_power(inputs, parameters)
    if operation == "cost":
        return _rate_calculation("cost", inputs, parameters)
    if operation == "carbon":
        return _rate_calculation("carbon", inputs, parameters)
    if operation == "baseline":
        return _baseline(inputs, parameters)
    if operation == "compare":
        return _compare(inputs, parameters)
    if operation == "normalize":
        return _normalize(inputs, parameters)
    return _align(inputs, parameters)


def _require_input_count(inputs: list[tuple[str, EnergyResult]], count: int) -> None:
    if len(inputs) != count:
        raise EnergyError("invalid_input", f"This operation requires exactly {count} inputs.")


def _table(result: EnergyResult) -> list[dict[str, Any]]:
    if not isinstance(result.data, list) or not result.data:
        raise EnergyError("not_tabular", "Input must contain a nonempty list of row objects.")
    if len(result.data) > MAX_ROWS:
        raise EnergyError("input_too_large", "Input contains too many rows.")
    if any(not isinstance(row, dict) for row in result.data):
        raise EnergyError("not_tabular", "Every input row must be an object.")
    return [dict(row) for row in result.data]


def _name(parameters: Json, key: str, default: str) -> str:
    value = parameters.get(key, default)
    if not isinstance(value, str) or not value:
        raise EnergyError("invalid_parameters", f"{key} must be a non-empty string.")
    return value


def _parse_timestamp(value: Any) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError("invalid_timestamp", "Timestamp is not a valid ISO-8601 value.") from exc
    if pd.isna(parsed):
        raise EnergyError("invalid_timestamp", "Timestamp must not be null or NaT.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", "Each timestamp must include an explicit UTC offset.")
    return parsed.tz_convert("UTC")


def _timestamp_text(value: pd.Timestamp, timezone: str) -> str:
    return value.tz_convert(timezone).isoformat()


def _number(value: Any, *, allow_missing: bool = True) -> float | None:
    if value is None:
        if allow_missing:
            return None
        raise EnergyError("missing_value", "A required numeric value is missing.")
    if isinstance(value, bool):
        raise EnergyError("invalid_value", "Boolean values are not valid series numbers.")
    try:
        if pd.isna(value):
            if allow_missing:
                return None
            raise EnergyError("missing_value", "A required numeric value is missing.")
    except (TypeError, ValueError):
        pass
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EnergyError("invalid_value", "A series value is not numeric.") from exc
    if not isfinite(parsed):
        raise EnergyError("invalid_value", "Series values must be finite numbers or null.")
    return parsed


def _series(
    artifact_id: str,
    result: EnergyResult,
    parameters: Json,
    *,
    timestamp_key: str = "timestamp",
    column_key: str = "column",
    default_column: str = "value",
) -> _Series:
    rows = _table(result)
    timestamp = _name(parameters, timestamp_key, "timestamp")
    column = _name(parameters, column_key, default_column)
    missing_columns = [
        field for field in (timestamp, column) if any(field not in row for row in rows)
    ]
    if missing_columns:
        raise EnergyError("column_not_found", "Timestamp or value column is missing.")

    parsed: list[tuple[pd.Timestamp, dict[str, Any], float | None]] = []
    for row in rows:
        parsed.append((_parse_timestamp(row[timestamp]), row, _number(row[column])))
    parsed.sort(key=lambda item: item[0])
    times = [item[0] for item in parsed]
    if len(set(times)) != len(times):
        raise EnergyError("duplicate_timestamp", "Duplicate UTC timestamps are not allowed.")
    return _Series(
        artifact_id=artifact_id,
        result=result,
        rows=tuple(item[1] for item in parsed),
        times=tuple(times),
        values=tuple(item[2] for item in parsed),
        timestamp=timestamp,
        column=column,
    )


def _duration(value: str | None) -> int | None:
    if value is None:
        return None
    aliases = {
        **_FREQUENCY_SECONDS,
        "15m": 15 * 60,
        "30m": 30 * 60,
        "hourly": 60 * 60,
        "1d": 24 * 60 * 60,
    }
    return aliases.get(value)


def _infer_resolution(times: tuple[pd.Timestamp, ...]) -> int | None:
    if len(times) < 2:
        return None
    differences = {
        int((right - left).total_seconds())
        for left, right in zip(times[:-1], times[1:], strict=True)
    }
    if len(differences) != 1:
        return None
    value = next(iter(differences))
    return value if value > 0 else None


def _resolution_seconds(result: EnergyResult, times: tuple[pd.Timestamp, ...]) -> int | None:
    explicit = _duration(result.resolution)
    return explicit if explicit is not None else _infer_resolution(times)


def _resolution_label(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    for label, value in _FREQUENCY_SECONDS.items():
        if value == seconds:
            return label
    return None


def _require_same_resolution(left: _Series, right: _Series) -> str | None:
    left_resolution = _resolution_seconds(left.result, left.times)
    right_resolution = _resolution_seconds(right.result, right.times)
    if (
        left_resolution is not None
        and right_resolution is not None
        and left_resolution != right_resolution
    ):
        raise EnergyError("mixed_resolution", "Inputs have incompatible resolutions.")
    return _resolution_label(left_resolution or right_resolution)


def _frequency(parameters: Json) -> tuple[str, int]:
    value = parameters.get("frequency", "30min")
    if not isinstance(value, str) or value not in _FREQUENCY_SECONDS:
        raise EnergyError("invalid_frequency", "Use 15min, 30min, 1h or 1D frequency.")
    return value, _FREQUENCY_SECONDS[value]


def _bounds(parameters: Json) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    start = parameters.get("start")
    end = parameters.get("end")
    parsed_start = None if start is None else _parse_timestamp(start)
    parsed_end = None if end is None else _parse_timestamp(end)
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise EnergyError("invalid_range", "The filter start must not be after the end.")
    return parsed_start, parsed_end


def _unit_info(unit: str) -> _Unit:
    raw = unit.strip().replace(" ", "")
    lower = raw.lower()
    energy = {"wh": (1e-3, "kWh"), "kwh": (1.0, "kWh"), "mwh": (1e3, "kWh")}
    power = {"w": (1e-3, "kW"), "kw": (1.0, "kW"), "mw": (1e3, "kW")}
    if lower in energy:
        factor, base = energy[lower]
        return _Unit("energy", factor, base)
    if lower in power:
        factor, base = power[lower]
        return _Unit("power", factor, base)

    rate_match = re.fullmatch(r"(GBP_pence|p|GBP|USD|EUR)/(Wh|kWh|MWh)", raw, re.IGNORECASE)
    if rate_match:
        numerator = rate_match.group(1).lower()
        denominator = rate_match.group(2).lower()
        denominator_factor = energy[denominator][0]
        if numerator in {"p", "gbp_pence"}:
            currency = "GBP"
            numerator_factor = 0.01
        else:
            currency = numerator.upper()
            numerator_factor = 1.0
        factor = numerator_factor / denominator_factor
        return _Unit("rate", factor, f"{currency}/kWh", currency=currency)

    carbon_match = re.fullmatch(r"(g|kg)(CO2|CO2e)/(Wh|kWh|MWh)", raw, re.IGNORECASE)
    if carbon_match:
        mass = carbon_match.group(1).lower()
        species = carbon_match.group(2).lower()
        denominator = carbon_match.group(3).lower()
        mass_factor = 1.0 if mass == "g" else 1000.0
        carbon_denominator_factor = energy[denominator][0]
        factor = mass_factor / carbon_denominator_factor
        display_species = "gCO2e" if species == "co2e" else "gCO2"
        return _Unit(
            "carbon_rate",
            factor,
            f"{display_species}/kWh",
            species=display_species,
        )
    raise EnergyError("unknown_unit", "Unit is not in the supported energy unit registry.")


def _conversion(input_unit: str, target_unit: str) -> float:
    source = _unit_info(input_unit)
    target = _unit_info(target_unit)
    if source.dimension != target.dimension:
        raise EnergyError("unit_mismatch", "Input and target units have different dimensions.")
    if source.currency != target.currency or source.species != target.species:
        raise EnergyError("unit_mismatch", "Input and target units have different meanings.")
    return source.to_base / target.to_base


def _lineage(operation: str, inputs: list[tuple[str, EnergyResult]]) -> list[Json]:
    return [
        {
            "operation": operation,
            "version": 1,
            "inputs": [
                {
                    "artifact_id": artifact_id,
                    "kind": result.kind.value,
                    "source": result.source,
                    "unit": result.unit,
                    "quantity_shape": result.quantity_shape,
                    "timezone": result.timezone,
                    "resolution": result.resolution,
                    "provider": result.provider,
                    "site_id": result.site_id,
                    "asset_id": result.asset_id,
                    "original_unit": result.original_unit,
                    "field_units": result.field_units,
                    "provenance": result.provenance,
                }
                for artifact_id, result in inputs
            ],
        }
    ]


def _consistent_metadata(inputs: list[tuple[str, EnergyResult]], field: str) -> Any | None:
    values = [getattr(result, field) for _, result in inputs]
    if values and values[0] is not None and all(value == values[0] for value in values):
        return values[0]
    return None


def _merged_field_units(inputs: list[tuple[str, EnergyResult]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for _, result in inputs:
        for name, unit in result.field_units.items():
            if name in merged and merged[name] != unit:
                merged[name] = "mixed"
            else:
                merged[name] = unit
    return merged


def _derived(
    operation: str,
    data: Any,
    inputs: list[tuple[str, EnergyResult]],
    unit: str,
    *,
    resolution: str | None = None,
    assumptions: list[str] | None = None,
    warnings: list[str] | None = None,
    field_units: dict[str, str] | None = None,
    quantity_shape: QuantityShape | None = None,
) -> EnergyResult:
    all_warnings = [warning for _, result in inputs for warning in result.warnings]
    all_warnings.extend(warnings or [])
    original_units = [result.original_unit or result.unit for _, result in inputs]
    original_unit = (
        original_units[0] if all(unit == original_units[0] for unit in original_units) else None
    )
    merged_units = _merged_field_units(inputs)
    if field_units:
        merged_units.update(field_units)
    return EnergyResult(
        data=data,
        kind=DataKind.CALCULATED,
        unit=unit,
        source="workbench",
        timezone=inputs[0][1].timezone,
        resolution=resolution,
        quantity_shape=quantity_shape,
        provider=_consistent_metadata(inputs, "provider"),
        site_id=_consistent_metadata(inputs, "site_id"),
        asset_id=_consistent_metadata(inputs, "asset_id"),
        time_start=_consistent_metadata(inputs, "time_start"),
        time_end=_consistent_metadata(inputs, "time_end"),
        original_unit=original_unit,
        field_units=merged_units,
        assumptions=assumptions or [],
        warnings=list(dict.fromkeys(all_warnings)),
        quality="derived",
        provenance=_lineage(operation, inputs),
    )


def _filter(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    start, end = _bounds(parameters)
    minimum = parameters.get("minimum")
    maximum = parameters.get("maximum")
    minimum_value = None if minimum is None else _number(minimum, allow_missing=False)
    maximum_value = None if maximum is None else _number(maximum, allow_missing=False)
    if minimum_value is not None and maximum_value is not None and minimum_value > maximum_value:
        raise EnergyError("invalid_range", "The filter minimum must not be above the maximum.")
    selected = [
        dict(row)
        for row, timestamp, value in zip(series.rows, series.times, series.values, strict=True)
        if (start is None or timestamp >= start)
        and (end is None or timestamp <= end)
        and (minimum_value is None or (value is not None and value >= minimum_value))
        and (maximum_value is None or (value is not None and value <= maximum_value))
    ]
    return _derived(
        "filter",
        selected,
        inputs,
        series.result.unit,
        resolution=series.result.resolution,
        quantity_shape=series.result.quantity_shape,
        assumptions=[
            "Start and end bounds are inclusive after UTC normalization.",
            "Minimum and maximum apply inclusively to the selected numeric column.",
        ],
    )


def _missing(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    frequency, seconds = _frequency(parameters)
    period_ns = seconds * 1_000_000_000
    first_ns = series.times[0].value
    last_ns = series.times[-1].value
    for timestamp in series.times:
        if (timestamp.value - first_ns) % period_ns:
            raise EnergyError(
                "off_grid_timestamp",
                "Every timestamp must lie on the requested interval grid.",
            )
    span_ns = last_ns - first_ns
    expected_count = span_ns // period_ns + 1
    if expected_count > MAX_OUTPUT_ROWS:
        raise EnergyError("output_too_large", "Missing-interval expansion exceeds the row limit.")
    expected = pd.date_range(
        start=series.times[0],
        end=series.times[-1],
        freq=pd.Timedelta(seconds=seconds),
    )
    if len(expected) != expected_count:
        raise EnergyError("invalid_range", "Timestamp range could not be represented safely.")
    observed = {
        timestamp: (row, value)
        for timestamp, row, value in zip(series.times, series.rows, series.values, strict=True)
    }
    output: list[Json] = []
    missing_count = 0
    for timestamp in expected:
        row, value = observed.get(timestamp, ({}, None))
        if not row:
            missing_count += 1
            output.append(
                {
                    series.timestamp: _timestamp_text(timestamp, series.result.timezone),
                    series.column: None,
                    "missing": True,
                }
            )
        else:
            output_row = dict(row)
            output_row[series.timestamp] = _timestamp_text(timestamp, series.result.timezone)
            output_row[series.column] = value
            output_row["missing"] = value is None
            missing_count += int(value is None)
            output.append(output_row)
    return _derived(
        "missing",
        output,
        inputs,
        series.result.unit,
        resolution=frequency,
        quantity_shape=series.result.quantity_shape,
        assumptions=[
            f"Expected UTC instants at {frequency} intervals between the first and last observed row.",
            "Rows outside the observed coverage window are not inferred.",
        ],
        warnings=[
            f"Detected {missing_count} missing interval(s)."
            if missing_count
            else "No missing intervals detected."
        ],
    )


def _counter(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    unit = _unit_info(series.result.unit)
    if unit.dimension != "energy":
        raise EnergyError("unit_mismatch", "Counter readings must use an energy unit.")
    if series.result.quantity_shape in {"interval", "instantaneous"}:
        raise EnergyError(
            "quantity_shape_mismatch", "Counter differences require cumulative readings."
        )
    expected_seconds: int | None
    expected_frequency: str | None
    if "frequency" in parameters:
        expected_frequency, expected_seconds = _frequency(parameters)
    else:
        expected_seconds = _duration(series.result.resolution)
        expected_frequency = _resolution_label(expected_seconds)
    if expected_seconds is None:
        raise EnergyError(
            "missing_resolution",
            "Counter differences require a declared resolution or frequency parameter.",
        )
    output: list[Json] = []
    reset_count = 0
    missing_count = 0
    gap_count = 0
    previous: float | None = None
    last_valid: float | None = None
    previous_timestamp: pd.Timestamp | None = None
    seen = False
    for timestamp, _row, current in zip(series.times, series.rows, series.values, strict=True):
        status = "initial" if not seen else "missing"
        delta: float | None = None
        if not seen:
            seen = True
        elif (
            previous_timestamp is not None
            and (timestamp - previous_timestamp).total_seconds() != expected_seconds
        ):
            status = "gap"
            gap_count += 1
        elif previous is not None and current is not None:
            if current < previous:
                status = "counter_reset"
                reset_count += 1
            else:
                status = "ok"
                delta = current - previous
        else:
            if current is not None and last_valid is not None and current < last_valid:
                reset_count += 1
            missing_count += 1
        output.append(
            {
                "timestamp": _timestamp_text(
                    previous_timestamp
                    if previous_timestamp is not None
                    else timestamp - pd.Timedelta(seconds=expected_seconds),
                    series.result.timezone,
                ),
                "end": _timestamp_text(timestamp, series.result.timezone),
                "counter_observed_at": _timestamp_text(timestamp, series.result.timezone),
                series.column: delta,
                "counter_observation": {"value": current, "unit": series.result.unit},
                "status": status,
            }
        )
        previous = current
        previous_timestamp = timestamp
        if current is not None:
            last_valid = current
    warnings = ["The interval preceding the first counter reading is unobserved and remains null."]
    if reset_count:
        warnings.append(f"Detected {reset_count} counter reset(s); reset intervals are null.")
    if missing_count:
        warnings.append("Missing counter readings remain null; no intervals were interpolated.")
    if gap_count:
        warnings.append(
            f"Detected {gap_count} interval gap(s) against the expected {expected_frequency or expected_seconds}-resolution; gap differences are null."
        )
    derived = _derived(
        "counter",
        output,
        inputs,
        series.result.unit,
        resolution=expected_frequency,
        quantity_shape="interval",
        field_units={series.column: series.result.unit},
        assumptions=[
            "Each difference covers the preceding and current reading timestamps; timestamp is the interval start and end is the current observation.",
            "The initial null row covers one declared interval before the first reading; it does not infer consumption.",
        ],
        warnings=warnings,
    )

    return derived.model_copy(
        update={
            "time_start": _parse_timestamp(output[0]["timestamp"]).to_pydatetime(),
            "time_end": series.times[-1].to_pydatetime(),
        }
    )


def _interval_end(row: dict[str, Any], start: pd.Timestamp, end_column: str) -> pd.Timestamp:
    if end_column not in row:
        raise EnergyError("column_not_found", "The configured interval end column is missing.")
    end = _parse_timestamp(row[end_column])
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise EnergyError("invalid_interval", "Each interval end must be after its start.")
    if seconds > MAX_INTERVAL_SECONDS:
        raise EnergyError("invalid_interval", "Intervals may not exceed seven days.")
    return end


def _optional_ends(series: _Series, end_column: str = "end") -> tuple[pd.Timestamp, ...] | None:
    present = [end_column in row for row in series.rows]
    if not any(present):
        return None
    if not all(present):
        raise EnergyError("column_not_found", "Interval end must be present on every row.")
    ends = tuple(
        _interval_end(row, timestamp, end_column)
        for row, timestamp in zip(series.rows, series.times, strict=True)
    )
    for end, next_start in zip(ends[:-1], series.times[1:], strict=True):
        if end > next_start:
            raise EnergyError("overlapping_intervals", "Explicit intervals must not overlap.")
    return ends


def _integrate_power(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    unit = _unit_info(series.result.unit)
    if unit.dimension != "power":
        raise EnergyError("unit_mismatch", "Power integration requires W, kW or MW input.")
    method = parameters.get("method", "trapezoid")
    if method not in {"left", "trapezoid"}:
        raise EnergyError(
            "invalid_parameters", "Power integration method must be left or trapezoid."
        )
    end_column = parameters.get("end", "end")
    if not isinstance(end_column, str) or not end_column:
        raise EnergyError("invalid_parameters", "The interval end column must be a string.")
    ends = _optional_ends(series, end_column)
    explicit_end = ends is not None
    expected_seconds = _duration(series.result.resolution)
    if "frequency" in parameters:
        _, expected_seconds = _frequency(parameters)
    output: list[Json] = []
    missing_count = 0
    gap_count = 0
    if explicit_end:
        assert ends is not None
        for timestamp, end, value in zip(series.times, ends, series.values, strict=True):
            duration = int((end - timestamp).total_seconds())
            is_gap = expected_seconds is not None and duration != expected_seconds
            if is_gap:
                gap_count += 1
            energy = None if value is None or is_gap else value * unit.to_base * duration / 3600
            missing_count += int(energy is None)
            output_row: Json = {
                "timestamp": _timestamp_text(timestamp, series.result.timezone),
                "end": _timestamp_text(end, series.result.timezone),
                "energy": energy,
            }
            if is_gap:
                output_row["status"] = "gap"
            output.append(output_row)
    else:
        if len(series.times) < 2:
            raise EnergyError(
                "insufficient_rows", "At least two timestamps are required for integration."
            )
        for index, (timestamp, value) in enumerate(
            zip(series.times[:-1], series.values[:-1], strict=True)
        ):
            end = series.times[index + 1]
            seconds = (end - timestamp).total_seconds()
            if seconds <= 0 or seconds > MAX_INTERVAL_SECONDS:
                raise EnergyError(
                    "invalid_interval", "Adjacent timestamps have an invalid interval."
                )
            next_value = series.values[index + 1]
            is_gap = expected_seconds is not None and seconds != expected_seconds
            if is_gap:
                gap_count += 1
            if is_gap or value is None or (method == "trapezoid" and next_value is None):
                energy = None
                missing_count += 1
            elif method == "left":
                energy = value * unit.to_base * seconds / 3600
            else:
                assert next_value is not None
                energy = (value + next_value) / 2 * unit.to_base * seconds / 3600
            output_row = {
                "timestamp": _timestamp_text(timestamp, series.result.timezone),
                "end": _timestamp_text(end, series.result.timezone),
                "energy": energy,
            }
            if is_gap:
                output_row["status"] = "gap"
            output.append(output_row)
    warnings = (
        ["Intervals with missing power remain null; no interpolation was performed."]
        if missing_count
        else []
    )
    if gap_count:
        warnings.append(
            "Intervals that differ from the declared resolution remain null; unobserved hours were not integrated."
        )
    return _derived(
        "integrate_power",
        output,
        inputs,
        "kWh",
        resolution=_resolution_label(_resolution_seconds(series.result, series.times)),
        field_units={"energy": "kWh"},
        quantity_shape="interval",
        assumptions=[
            f"Power values were converted from {series.result.unit} to kW.",
            f"Integration method: {method}.",
        ],
        warnings=warnings,
    )


def _paired_series(
    inputs: list[tuple[str, EnergyResult]], parameters: Json
) -> tuple[_Series, _Series, str | None]:
    _require_input_count(inputs, 2)
    left_params = dict(parameters)
    right_params = dict(parameters)
    if "second_timestamp" in parameters:
        right_params["timestamp"] = parameters["second_timestamp"]
    if "second_column" in parameters:
        right_params["column"] = parameters["second_column"]
    left = _series(*inputs[0], left_params)
    right = _series(*inputs[1], right_params)
    resolution = _require_same_resolution(left, right)
    if left.times != right.times:
        raise EnergyError(
            "insufficient_coverage", "Inputs must cover the exact same UTC timestamps."
        )
    end_column = parameters.get("end", "end")
    if not isinstance(end_column, str) or not end_column:
        raise EnergyError("invalid_parameters", "The interval end column must be a string.")
    left_ends = _optional_ends(left, end_column)
    right_ends = _optional_ends(right, end_column)
    if (left_ends is None) != (right_ends is None):
        raise EnergyError(
            "interval_mismatch",
            "Both aligned inputs must provide interval ends when either does.",
        )
    if left_ends is not None and right_ends is not None:
        for left_start, left_end, right_start, right_end in zip(
            left.times, left_ends, right.times, right_ends, strict=True
        ):
            left_duration = left_end - left_start
            right_duration = right_end - right_start
            if left_duration != right_duration:
                raise EnergyError(
                    "interval_mismatch",
                    "Aligned inputs must have matching explicit interval durations.",
                )
    return left, right, resolution


def _rate_calculation(
    operation: str,
    inputs: list[tuple[str, EnergyResult]],
    parameters: Json,
) -> EnergyResult:
    left, right, resolution = _paired_series(inputs, parameters)
    left_unit = _unit_info(left.result.unit)
    right_unit = _unit_info(right.result.unit)
    if {left_unit.dimension, right_unit.dimension} != {
        "energy",
        "rate" if operation == "cost" else "carbon_rate",
    }:
        expected = (
            "energy plus a price rate" if operation == "cost" else "energy plus carbon intensity"
        )
        raise EnergyError("unit_mismatch", f"{operation} requires {expected} inputs.")
    if left_unit.dimension == "energy":
        energy, rate = left, right
        energy_unit, rate_unit = left_unit, right_unit
    else:
        energy, rate = right, left
        energy_unit, rate_unit = right_unit, left_unit
    if energy.result.quantity_shape in {"counter", "instantaneous"}:
        raise EnergyError(
            "quantity_shape_mismatch",
            "Cost and carbon calculations require interval energy. Convert counters or integrate power explicitly first.",
        )
    energy_values = dict(zip(energy.times, energy.values, strict=True))
    rate_values = dict(zip(rate.times, rate.values, strict=True))
    output: list[Json] = []
    missing_energy = 0
    for timestamp in energy.times:
        energy_value = energy_values[timestamp]
        rate_value = rate_values[timestamp]
        if rate_value is None:
            raise EnergyError(
                "missing_rate", "Rate or intensity coverage contains a missing value."
            )
        computed = None
        if energy_value is None:
            missing_energy += 1
        else:
            computed = energy_value * energy_unit.to_base * rate_value * rate_unit.to_base
        row: Json = {
            "timestamp": _timestamp_text(timestamp, energy.result.timezone),
            "energy": energy_value,
            "rate": rate_value,
            "cost" if operation == "cost" else "carbon": computed,
        }
        output.append(row)
    if operation == "cost":
        output_unit = rate_unit.currency or "currency"
        assumption = "Energy was converted to kWh and price to major currency per kWh."
    else:
        output_unit = rate_unit.species or "gCO2e"
        assumption = "Energy was converted to kWh and intensity to grams per kWh."
    warnings = (
        ["Rows with missing energy remain null; no energy was inferred."] if missing_energy else []
    )
    return _derived(
        operation,
        output,
        inputs,
        output_unit,
        resolution=resolution,
        field_units={
            "energy": energy.result.unit,
            "rate": rate.result.unit,
            "cost" if operation == "cost" else "carbon": output_unit,
        },
        assumptions=[
            assumption,
            "Inputs were joined by exact UTC interval start; no fill or nearest match was used.",
        ],
        warnings=warnings,
    )


def _baseline(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    window = parameters.get("window", 4)
    if (
        isinstance(window, bool)
        or not isinstance(window, int)
        or not 1 <= window <= MAX_BASELINE_WINDOW
    ):
        raise EnergyError(
            "invalid_parameters", "Baseline window must be an integer from 1 to 10000."
        )
    values = pd.Series(series.values, dtype="float64")
    baseline = values.rolling(window=window, min_periods=window).mean().shift(1)
    output: list[Json] = []
    for timestamp, _row, value, base in zip(
        series.times, series.rows, series.values, baseline, strict=True
    ):
        baseline_value = None if pd.isna(base) else float(base)
        output.append(
            {
                "timestamp": _timestamp_text(timestamp, series.result.timezone),
                series.column: value,
                "baseline": baseline_value,
                "residual": None
                if value is None or baseline_value is None
                else value - baseline_value,
            }
        )
    return _derived(
        "baseline",
        output,
        inputs,
        series.result.unit,
        resolution=series.result.resolution,
        field_units={
            series.column: series.result.unit,
            "baseline": series.result.unit,
            "residual": series.result.unit,
        },
        assumptions=[
            f"Baseline is the mean of the previous {window} observations; current values are excluded."
        ],
        warnings=["A baseline is null until the requested history window is complete."],
    )


def _period_key(timestamp: pd.Timestamp, frequency: str, timezone: str) -> date:
    local = timestamp.tz_convert(timezone)
    local_date = local.date()
    if frequency == "daily":
        return local_date
    if frequency == "weekly":
        return local_date - timedelta(days=local.weekday())
    return date(local.year, local.month, 1)


def _previous_period(key: date, frequency: str) -> date:
    if frequency == "daily":
        return key - timedelta(days=1)
    if frequency == "weekly":
        return key - timedelta(days=7)
    return date(key.year - 1, 12, 1) if key.month == 1 else date(key.year, key.month - 1, 1)


def _period_start(key: date, frequency: str, timezone: str) -> str:
    if frequency == "weekly":
        start = key
    elif frequency == "monthly":
        start = date(key.year, key.month, 1)
    else:
        start = key
    return pd.Timestamp(datetime.combine(start, time.min), tz=ZoneInfo(timezone)).isoformat()


def _compare(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    frequency = parameters.get("frequency", "daily")
    if frequency not in {"daily", "weekly", "monthly"}:
        raise EnergyError(
            "invalid_frequency", "Comparison frequency must be daily, weekly or monthly."
        )
    unit = _unit_info(series.result.unit)
    if unit.dimension != "energy":
        raise EnergyError("unit_mismatch", "Calendar comparison requires interval energy input.")
    grouped: dict[date, list[float]] = {}
    missing: dict[date, int] = {}
    for timestamp, value in zip(series.times, series.values, strict=True):
        key = _period_key(timestamp, frequency, series.result.timezone)
        if value is None:
            missing[key] = missing.get(key, 0) + 1
        else:
            grouped.setdefault(key, []).append(value * unit.to_base)
    output: list[Json] = []
    for key in sorted(set(grouped) | set(missing)):
        current_values = grouped.get(key, [])
        current = sum(current_values) if current_values else None
        previous_values = grouped.get(_previous_period(key, frequency), [])
        previous = sum(previous_values) if previous_values else None
        difference = None if current is None or previous is None else current - previous
        percent = None
        if difference is not None and previous is not None and previous != 0:
            percent = difference / previous * 100
        output.append(
            {
                "timestamp": _period_start(key, frequency, series.result.timezone),
                "period": key.isoformat(),
                "value": current,
                "previous": previous,
                "difference": difference,
                "percent_change": percent,
                "missing": missing.get(key, 0),
            }
        )
    return _derived(
        "compare",
        output,
        inputs,
        "kWh",
        resolution=frequency,
        field_units={
            "value": "kWh",
            "previous": "kWh",
            "difference": "kWh",
        },
        assumptions=[
            f"Values are summed into {frequency} local-calendar periods after conversion to kWh."
        ],
        warnings=[
            "Missing rows are excluded from period sums; comparison values are null when a prior period has no coverage."
        ],
    )


def _normalize(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    _require_input_count(inputs, 1)
    series = _series(*inputs[0], parameters)
    target = parameters.get("unit")
    if not isinstance(target, str) or not target:
        raise EnergyError("invalid_parameters", "normalize requires a target unit.")
    factor = _conversion(series.result.unit, target)
    output: list[Json] = []
    for timestamp, row, value in zip(series.times, series.rows, series.values, strict=True):
        output_row = dict(row)
        output_row[series.timestamp] = _timestamp_text(timestamp, series.result.timezone)
        output_row[series.column] = None if value is None else value * factor
        output.append(output_row)
    return _derived(
        "normalize",
        output,
        inputs,
        target,
        resolution=series.result.resolution,
        quantity_shape=series.result.quantity_shape,
        field_units={**series.result.field_units, series.column: target},
        assumptions=[
            f"Values converted from {series.result.unit} to {target}; original unit retained in lineage."
        ],
    )


def _align(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    left, right, resolution = _paired_series(inputs, parameters)
    right_name = right.column if right.column != left.column else f"{right.column}_right"
    output: list[Json] = []
    for timestamp, left_value, right_value in zip(
        left.times, left.values, right.values, strict=True
    ):
        output.append(
            {
                "timestamp": _timestamp_text(timestamp, left.result.timezone),
                left.column: left_value,
                right_name: right_value,
            }
        )
    return _derived(
        "align",
        output,
        inputs,
        "mixed",
        resolution=resolution,
        assumptions=[
            "Inputs were aligned on exact UTC timestamps; no fill, interpolation or nearest match was used."
        ],
    )
