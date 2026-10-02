"""Bounded, source-preserving comparison of generation and carbon intensity."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import fsum, isfinite
from typing import Any

from .models import DataKind, EnergyError, EnergyResult, Json

MAX_ROWS = 100_000

_POWER_TO_MW = {"mw": 1.0, "kw": 0.001, "gw": 1000.0}
_ENERGY_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
_CARBON_UNIT = re.compile(r"(g|kg)(co2e|co2)/(wh|kwh|mwh)", re.IGNORECASE)


@dataclass(frozen=True)
class _GenerationPoint:
    timestamp: datetime
    generation_mw: float
    end: datetime | None
    by_fuel_mw: dict[str, float] | None


@dataclass(frozen=True)
class _CarbonPoint:
    timestamp: datetime
    intensity_g_per_kwh: float
    end: datetime | None


def analyse(
    generation: tuple[str, EnergyResult],
    carbon: tuple[str, EnergyResult],
    parameters: Json,
) -> EnergyResult:
    """Join power observations to carbon-intensity rows at exact UTC starts.

    The operation compares source values; it does not integrate power into
    energy or calculate emissions mass. Fuel shares are emitted only when the
    caller supplies an explicit low-carbon fuel classification.
    """

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Grid analysis parameters must be an object.")
    _validate_input(generation, "generation")
    _validate_input(carbon, "carbon")

    generation_id, generation_result = generation
    carbon_id, carbon_result = carbon
    generation_rows = _table(generation_result, "generation")
    carbon_rows = _table(carbon_result, "carbon")
    if len(generation_rows) + len(carbon_rows) > MAX_ROWS:
        raise EnergyError("input_too_large", "Combined input contains too many rows.")

    timestamp_column = _name(parameters, "timestamp", "timestamp")
    generation_column = _name(parameters, "column", "value")
    carbon_timestamp_column = _name(parameters, "second_timestamp", "from")
    carbon_column = _name(parameters, "second_column", "value")
    generation_end_column = _name(parameters, "end_column", "end")
    carbon_end_column = _name(parameters, "second_end", "to")
    fuel_column = _optional_name(parameters, "fuel_column")
    low_carbon_fuels = _low_carbon_fuels(parameters)
    threshold = _optional_number(parameters, "carbon_threshold_g_per_kwh")
    if low_carbon_fuels is not None and fuel_column is None:
        raise EnergyError(
            "invalid_parameters", "low_carbon_fuels requires an explicit fuel_column."
        )

    generation_unit = _field_unit(generation_result, generation_column)
    power_factor = _power_factor(generation_unit)
    if generation_result.quantity_shape == "counter":
        raise EnergyError(
            "quantity_shape_mismatch", "Cumulative generation counters cannot be compared as power."
        )
    if generation_result.quantity_shape not in {None, "instantaneous"}:
        raise EnergyError(
            "quantity_shape_mismatch",
            "Generation power must be instantaneous or have unknown quantity shape.",
        )

    carbon_unit = _field_unit(carbon_result, carbon_column)
    carbon_factor, species, canonical_carbon_unit = _carbon_unit(carbon_unit)

    generation_ends = _ends(generation_rows, generation_end_column, "generation")
    carbon_ends = _ends(carbon_rows, carbon_end_column, "carbon")

    points = _generation_points(
        generation_rows,
        timestamp_column,
        generation_column,
        generation_end_column if generation_ends is not None else None,
        fuel_column,
        power_factor,
    )
    carbon_points = _carbon_points(
        carbon_rows,
        carbon_timestamp_column,
        carbon_column,
        carbon_end_column if carbon_ends is not None else None,
        carbon_factor,
    )
    if generation_ends is not None:
        _validate_interval_order(sorted(points, key=lambda point: point.timestamp))
    if carbon_ends is not None:
        _validate_interval_order(sorted(carbon_points, key=lambda point: point.timestamp))

    generation_times = {point.timestamp for point in points}
    carbon_times = {point.timestamp for point in carbon_points}
    if generation_times != carbon_times:
        raise EnergyError(
            "timestamp_mismatch",
            "Generation and carbon inputs must have exactly the same UTC start timestamps.",
        )
    carbon_by_time = {point.timestamp: point for point in carbon_points}
    if generation_ends is not None and carbon_ends is None:
        raise EnergyError(
            "missing_endpoints",
            "Generation endpoints require matching explicit carbon endpoints.",
        )
    if generation_ends is not None:
        for point in points:
            carbon_point = carbon_by_time[point.timestamp]
            if point.end != carbon_point.end:
                raise EnergyError(
                    "endpoint_mismatch",
                    "Generation and carbon endpoints must match exactly in UTC.",
                )

    supplied_fuels = _supplied_fuels(points)
    if low_carbon_fuels is not None:
        if not supplied_fuels:
            raise EnergyError(
                "invalid_parameters", "Fuel classification requires grouped generation fuel data."
            )
        unknown_fuels = set(low_carbon_fuels) - supplied_fuels
        if unknown_fuels:
            raise EnergyError(
                "invalid_parameters",
                "Every low-carbon fuel must appear in the supplied generation data.",
            )

    ordered_points = sorted(points, key=lambda point: point.timestamp)
    carbon_values = [
        carbon_by_time[point.timestamp].intensity_g_per_kwh for point in ordered_points
    ]
    generation_values = [point.generation_mw for point in ordered_points]
    output_rows: list[Json] = []
    for point in ordered_points:
        carbon_point = carbon_by_time[point.timestamp]
        row: Json = {
            "timestamp": _timestamp_text(point.timestamp),
            "generation_mw": point.generation_mw,
            "carbon_intensity_g_per_kwh": carbon_point.intensity_g_per_kwh,
        }
        if carbon_point.end is not None:
            row["end"] = _timestamp_text(carbon_point.end)
        if point.by_fuel_mw is not None:
            row["generation_by_fuel_mw"] = dict(sorted(point.by_fuel_mw.items()))
            if low_carbon_fuels is not None:
                if point.generation_mw > 0:
                    row["low_carbon_generation_fraction"] = (
                        sum(
                            value
                            for fuel, value in point.by_fuel_mw.items()
                            if fuel.casefold() in low_carbon_fuels
                        )
                        / point.generation_mw
                    )
                else:
                    row["low_carbon_generation_fraction"] = None
        output_rows.append(row)

    ranked_carbon_points = sorted(
        ordered_points,
        key=lambda point: (
            carbon_by_time[point.timestamp].intensity_g_per_kwh,
            point.timestamp,
        ),
    )
    summary: Json = {
        "generation_mw": _statistics(generation_values),
        "carbon_intensity_g_per_kwh": _statistics(carbon_values),
        "lower_carbon_intervals": [
            _timestamp_text(point.timestamp) for point in ranked_carbon_points
        ],
        "peak_generation_interval": _interval_summary(
            max(ordered_points, key=lambda point: point.generation_mw), carbon_by_time
        ),
        "lowest_carbon_interval": _interval_summary(ranked_carbon_points[0], carbon_by_time),
    }
    if threshold is not None:
        summary["carbon_threshold_g_per_kwh"] = threshold
        summary["intervals_at_or_below_carbon_threshold"] = [
            _timestamp_text(point.timestamp)
            for point in ranked_carbon_points
            if carbon_by_time[point.timestamp].intensity_g_per_kwh <= threshold
        ]

    has_matching_ends = generation_ends is not None and carbon_ends is not None
    endpoints_contiguous = _contiguous(ordered_points) if has_matching_ends else None
    horizon_claimed = bool(has_matching_ends and endpoints_contiguous)
    coverage: Json = {
        "generation_row_count": len(generation_rows),
        "carbon_row_count": len(carbon_rows),
        "aligned_timestamp_count": len(ordered_points),
        "timestamps_match_exactly": True,
        "generation_endpoints_provided": generation_ends is not None,
        "carbon_endpoints_provided": carbon_ends is not None,
        "endpoints_match": has_matching_ends if generation_ends is not None else None,
        "intervals_contiguous": endpoints_contiguous,
        "horizon_claimed": horizon_claimed,
    }
    data: Json = {
        "intervals": output_rows,
        "summary": summary,
        "coverage": coverage,
    }
    field_units = {
        "generation_mw": "MW",
        "carbon_intensity_g_per_kwh": canonical_carbon_unit,
    }
    if fuel_column is not None:
        field_units["generation_by_fuel_mw"] = "MW"
    if low_carbon_fuels is not None:
        field_units["low_carbon_generation_fraction"] = "1"
    if threshold is not None:
        field_units["carbon_threshold_g_per_kwh"] = canonical_carbon_unit

    assumptions = [
        "Power values are compared as timestamped samples in MW; no interval energy is inferred.",
        f"Carbon intensity retains source species {species}; no CO2/CO2e conversion was made.",
    ]
    if low_carbon_fuels is not None:
        assumptions.append(
            "Fuel shares classify only the explicitly listed low-carbon fuels within the supplied fuel set."
        )
    else:
        assumptions.append(
            "No low-carbon fuel classification was supplied, so no fuel shares were calculated."
        )
    if fuel_column is not None:
        assumptions.append(
            "Fuel breakdown contains only the supplied fuel categories and does not guarantee a complete grid mix."
        )

    inputs: list[tuple[str, EnergyResult]] = [generation, carbon]
    provenance = [
        {
            "operation": "grid_analysis",
            "version": 1,
            "inputs": [_input_lineage(artifact_id, result) for artifact_id, result in inputs],
        }
    ]
    return EnergyResult(
        data=data,
        kind=DataKind.CALCULATED,
        unit="mixed",
        source="workbench",
        timezone="UTC",
        resolution=(
            generation_result.resolution
            if generation_result.resolution == carbon_result.resolution
            else None
        ),
        time_start=ordered_points[0].timestamp,
        time_end=(ordered_points[-1].end if horizon_claimed else None),
        field_units=field_units,
        assumptions=assumptions,
        warnings=[
            "Lower-carbon ranking is advisory for supplied source rows and does not establish local grid conditions, stability, reserve adequacy, or device-control actions."
        ],
        quality="derived",
        provenance=provenance,
    )


def _validate_input(value: tuple[str, EnergyResult], label: str) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0]
        or not isinstance(value[1], EnergyResult)
    ):
        raise EnergyError(
            "invalid_input", f"{label} input must contain a nonempty artifact ID and EnergyResult."
        )


def _table(result: EnergyResult, label: str) -> list[dict[str, Any]]:
    if not isinstance(result.data, list) or not result.data:
        raise EnergyError("not_tabular", f"{label.capitalize()} input must be a nonempty row list.")
    if len(result.data) > MAX_ROWS:
        raise EnergyError("input_too_large", "An input contains too many rows.")
    if any(not isinstance(row, dict) for row in result.data):
        raise EnergyError("not_tabular", "Every input row must be an object.")
    return [dict(row) for row in result.data]


def _name(parameters: Json, key: str, default: str) -> str:
    value = parameters.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise EnergyError("invalid_parameters", f"{key} must be a nonempty string.")
    return value.strip()


def _optional_name(parameters: Json, key: str) -> str | None:
    if key not in parameters:
        return None
    value = parameters[key]
    if not isinstance(value, str) or not value.strip():
        raise EnergyError("invalid_parameters", f"{key} must be a nonempty string when supplied.")
    return value.strip()


def _low_carbon_fuels(parameters: Json) -> set[str] | None:
    if "low_carbon_fuels" not in parameters:
        return None
    value = parameters["low_carbon_fuels"]
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(fuel, str) or not fuel.strip() for fuel in value)
    ):
        raise EnergyError(
            "invalid_parameters", "low_carbon_fuels must be a nonempty list of fuel names."
        )
    fuels = [fuel.strip().casefold() for fuel in value]
    if len(set(fuels)) != len(fuels):
        raise EnergyError("invalid_parameters", "low_carbon_fuels cannot contain duplicates.")
    return set(fuels)


def _optional_number(parameters: Json, key: str) -> float | None:
    if key not in parameters:
        return None
    value = _number(parameters[key], key)
    if value < 0:
        raise EnergyError("invalid_parameters", f"{key} must be nonnegative.")
    return value


def _number(value: Any, label: str) -> float:
    if value is None:
        raise EnergyError("missing_value", f"{label} is missing; null is not treated as zero.")
    if isinstance(value, bool):
        raise EnergyError("invalid_value", f"{label} must be a finite number, not a boolean.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError("invalid_value", f"{label} must be a finite number.") from exc
    if not isfinite(parsed):
        raise EnergyError("invalid_value", f"{label} must be finite.")
    return parsed


def _scaled_number(value: Any, factor: float, label: str) -> float:
    scaled = _number(value, label) * factor
    if not isfinite(scaled):
        raise EnergyError("invalid_value", f"Converted {label} must be finite.")
    return scaled


def _safe_sum(values: Any, label: str) -> float:
    try:
        total = fsum(values)
    except (OverflowError, ValueError) as exc:
        raise EnergyError("invalid_value", f"{label} must be finite.") from exc
    if not isfinite(total):
        raise EnergyError("invalid_value", f"{label} must be finite.")
    return total


def _field_unit(result: EnergyResult, column: str) -> str:
    field_unit = result.field_units.get(column)
    if field_unit is None:
        return result.unit
    result_unit = result.unit.strip().lower().replace(" ", "")
    selected_unit = field_unit.strip().lower().replace(" ", "")
    if result_unit not in {"mixed", selected_unit} and not _units_compatible(
        result.unit, field_unit
    ):
        raise EnergyError(
            "unit_mismatch", "The selected field unit conflicts with the result unit."
        )
    return field_unit


def _units_compatible(result_unit: str, field_unit: str) -> bool:
    result_power = result_unit.strip().lower() in _POWER_TO_MW
    field_power = field_unit.strip().lower() in _POWER_TO_MW
    if result_power or field_power:
        return result_power and field_power
    result_carbon = _CARBON_UNIT.fullmatch(result_unit.strip().replace(" ", ""))
    field_carbon = _CARBON_UNIT.fullmatch(field_unit.strip().replace(" ", ""))
    return (
        result_carbon is not None
        and field_carbon is not None
        and result_carbon.group(2).lower() == field_carbon.group(2).lower()
    )


def _power_factor(unit: str) -> float:
    factor = _POWER_TO_MW.get(unit.strip().lower())
    if factor is None:
        raise EnergyError("unknown_unit", "Generation must use MW, kW, or GW power units.")
    return factor


def _carbon_unit(unit: str) -> tuple[float, str, str]:
    match = _CARBON_UNIT.fullmatch(unit.strip().replace(" ", ""))
    if match is None:
        raise EnergyError(
            "unknown_unit", "Carbon intensity must use g or kg CO2/CO2e per Wh, kWh, or MWh."
        )
    mass, raw_species, denominator = match.groups()
    species = "CO2e" if raw_species.lower() == "co2e" else "CO2"
    mass_factor = 1.0 if mass.lower() == "g" else 1000.0
    denominator_factor = _ENERGY_TO_KWH[denominator.lower()]
    return mass_factor / denominator_factor, species, f"g{species}/kWh"


def _ends(
    rows: list[dict[str, Any]],
    column: str,
    label: str,
) -> list[datetime] | None:
    presence = [column in row for row in rows]
    if not any(presence):
        return None
    if not all(presence):
        raise EnergyError(
            "missing_endpoints",
            f"Every {label} row must provide {column!r} when interval endpoints are used.",
        )
    return [_parse_timestamp(row[column], f"{label} endpoint") for row in rows]


def _validate_interval_order(
    points: Sequence[_GenerationPoint | _CarbonPoint],
) -> None:
    previous_end: datetime | None = None
    for point in points:
        if point.end is None or point.end <= point.timestamp:
            raise EnergyError("invalid_endpoint", "Each interval endpoint must follow its start.")
        if previous_end is not None and point.timestamp < previous_end:
            raise EnergyError("overlapping_intervals", "Source intervals must not overlap.")
        previous_end = point.end


def _generation_points(
    rows: list[dict[str, Any]],
    timestamp_column: str,
    value_column: str,
    end_column: str | None,
    fuel_column: str | None,
    factor: float,
) -> list[_GenerationPoint]:
    required = [timestamp_column, value_column]
    if fuel_column is not None:
        required.append(fuel_column)
    if end_column is not None:
        required.append(end_column)
    if any(any(column not in row for column in required) for row in rows):
        raise EnergyError("column_not_found", "A required generation column is missing.")

    grouped: dict[datetime, list[tuple[str | None, float, datetime | None]]] = defaultdict(list)
    seen: set[tuple[datetime, str | None]] = set()
    display_fuels: dict[str, str] = {}
    for row in rows:
        timestamp = _parse_timestamp(row[timestamp_column], "generation timestamp")
        value = _scaled_number(row[value_column], factor, "generation value")
        if value < 0:
            raise EnergyError("invalid_value", "Generation power cannot be negative.")
        end = _parse_timestamp(row[end_column], "generation endpoint") if end_column else None
        if end is not None and end <= timestamp:
            raise EnergyError("invalid_endpoint", "Each interval endpoint must follow its start.")
        fuel: str | None = None
        normalized_fuel: str | None = None
        if fuel_column is not None:
            raw_fuel = row[fuel_column]
            if not isinstance(raw_fuel, str) or not raw_fuel.strip():
                raise EnergyError("missing_value", "Each grouped generation row needs a fuel name.")
            fuel = raw_fuel.strip()
            normalized_fuel = fuel.casefold()
            display_fuels.setdefault(normalized_fuel, fuel)
        unique_key = (timestamp, normalized_fuel)
        if unique_key in seen:
            raise EnergyError(
                "duplicate_timestamp", "Duplicate generation timestamp/fuel rows are not allowed."
            )
        seen.add(unique_key)
        grouped[timestamp].append((normalized_fuel, value, end))

    points: list[_GenerationPoint] = []
    expected_fuels: set[str] | None = None
    for timestamp, rows_at_time in grouped.items():
        if fuel_column is None:
            if len(rows_at_time) != 1:
                raise EnergyError(
                    "duplicate_timestamp", "Duplicate generation timestamps are not allowed."
                )
            _, total, end = rows_at_time[0]
            by_fuel = None
        else:
            current_fuels = {fuel for fuel, _, _ in rows_at_time if fuel is not None}
            if expected_fuels is None:
                expected_fuels = current_fuels
            elif current_fuels != expected_fuels:
                raise EnergyError(
                    "missing_value",
                    "Grouped generation must include the same explicit fuel set at every timestamp.",
                )
            ends = {row_end for _, _, row_end in rows_at_time}
            if len(ends) > 1:
                raise EnergyError(
                    "endpoint_mismatch",
                    "All fuel rows at one generation timestamp need the same endpoint.",
                )
            end = next(iter(ends))
            by_fuel = {
                display_fuels[fuel]: value for fuel, value, _ in rows_at_time if fuel is not None
            }
            total = _safe_sum(by_fuel.values(), "grouped generation total")
        points.append(_GenerationPoint(timestamp, total, end, by_fuel))

    return points


def _carbon_points(
    rows: list[dict[str, Any]],
    timestamp_column: str,
    value_column: str,
    end_column: str | None,
    factor: float,
) -> list[_CarbonPoint]:
    required = [timestamp_column, value_column]
    if end_column is not None:
        required.append(end_column)
    if any(any(column not in row for column in required) for row in rows):
        raise EnergyError("column_not_found", "A required carbon column is missing.")
    points: list[_CarbonPoint] = []
    seen: set[datetime] = set()
    for row in rows:
        timestamp = _parse_timestamp(row[timestamp_column], "carbon timestamp")
        if timestamp in seen:
            raise EnergyError(
                "duplicate_timestamp", "Duplicate carbon UTC timestamps are not allowed."
            )
        seen.add(timestamp)
        value = _scaled_number(row[value_column], factor, "carbon intensity")
        end = _parse_timestamp(row[end_column], "carbon endpoint") if end_column else None
        if end is not None and end <= timestamp:
            raise EnergyError("invalid_endpoint", "Each interval endpoint must follow its start.")
        points.append(_CarbonPoint(timestamp, value, end))
    return points


def _supplied_fuels(points: list[_GenerationPoint]) -> set[str]:
    return {
        fuel.casefold()
        for point in points
        if point.by_fuel_mw is not None
        for fuel in point.by_fuel_mw
    }


def _parse_timestamp(value: Any, label: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError(
            "invalid_timestamp", f"{label} must be a valid ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", f"{label} must include an explicit UTC offset.")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.isoformat()


def _statistics(values: list[float]) -> Json:
    return {
        "min": min(values),
        "max": max(values),
        "mean": _safe_sum((value / len(values) for value in values), "mean"),
    }


def _interval_summary(
    point: _GenerationPoint, carbon_by_time: dict[datetime, _CarbonPoint]
) -> Json:
    carbon_point = carbon_by_time[point.timestamp]
    result: Json = {
        "timestamp": _timestamp_text(point.timestamp),
        "generation_mw": point.generation_mw,
        "carbon_intensity_g_per_kwh": carbon_point.intensity_g_per_kwh,
    }
    if carbon_point.end is not None:
        result["end"] = _timestamp_text(carbon_point.end)
    return result


def _contiguous(points: list[_GenerationPoint]) -> bool:
    if any(point.end is None for point in points):
        return False
    return all(points[index - 1].end == points[index].timestamp for index in range(1, len(points)))


def _input_lineage(artifact_id: str, result: EnergyResult) -> Json:
    return {
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
