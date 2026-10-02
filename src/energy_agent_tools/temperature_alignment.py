"""Align temperature context to a bounded, start-anchored interval grid."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .models import DataKind, EnergyError, EnergyResult, Json

_SOURCE_CADENCES = {
    "15min": 15 * 60,
    "30min": 30 * 60,
    "60min": 60 * 60,
    "1h": 60 * 60,
}
_TARGET_CADENCES = {15: 15 * 60, 30: 30 * 60, 60: 60 * 60}
_MAX_ROWS = 100_000
_CELSIUS_UNITS = {"°c", "c", "degc", "degree celsius", "degrees celsius", "celsius"}
_ZERO_ORDER_HOLD = (
    "Temperature context uses zero-order hold: each target interval start uses the latest source "
    "value at or before that instant, only while its age is less than the declared source cadence. "
    "No future reading, interpolation or backfill is used."
)


def align_temperature_context(
    source: tuple[str, EnergyResult],
    *,
    start: str,
    end: str,
    interval_minutes: int = 30,
    timestamp: str = "timestamp",
    column: str = "temperature",
    variable: str = "temperature_2m",
) -> EnergyResult:
    """Align a temperature series to interval starts using bounded zero-order hold.

    ``end`` is exclusive. Long-form data is selected by ``variable`` and must
    declare Celsius units on each selected row.
    """

    if (
        not isinstance(source, tuple)
        or len(source) != 2
        or not isinstance(source[0], str)
        or not source[0]
        or not isinstance(source[1], EnergyResult)
    ):
        raise EnergyError(
            "invalid_input", "Temperature input must include an artifact ID and result."
        )
    artifact_id, result = source

    if result.kind not in {DataKind.METERED, DataKind.ESTIMATED, DataKind.FORECAST}:
        raise EnergyError(
            "invalid_kind", "Temperature context must be metered, estimated or forecast data."
        )
    source_cadence = _source_cadence(result.resolution)
    if (
        isinstance(interval_minutes, bool)
        or not isinstance(interval_minutes, int)
        or interval_minutes not in _TARGET_CADENCES
    ):
        raise EnergyError("invalid_frequency", "Use a target interval of 15, 30 or 60 minutes.")
    if any(not isinstance(name, str) or not name for name in (timestamp, column, variable)):
        raise EnergyError("invalid_parameters", "Timestamp, column and variable names must be set.")

    left = _instant(start)
    right = _instant(end)
    if left >= right:
        raise EnergyError("invalid_range", "Start must precede the exclusive end.")
    target_step = timedelta(seconds=_TARGET_CADENCES[interval_minutes])
    duration = right - left
    if duration % target_step != timedelta(0):
        raise EnergyError(
            "invalid_range", "Window duration must be divisible by the target interval."
        )
    target_count = duration // target_step
    if target_count > _MAX_ROWS:
        raise EnergyError("output_too_large", "Temperature alignment exceeds 100000 output rows.")

    if not isinstance(result.data, list):
        raise EnergyError("not_tabular", "Temperature input must contain a list of row objects.")
    if len(result.data) > _MAX_ROWS:
        raise EnergyError("input_too_large", "Temperature input contains too many rows.")
    if not result.data:
        raise EnergyError("insufficient_data", "Temperature input contains no rows.")
    if any(not isinstance(row, dict) for row in result.data):
        raise EnergyError("not_tabular", "Every temperature input row must be an object.")
    rows = result.data
    long_form = any("variable" in row for row in rows)

    if long_form:
        if result.unit.strip().casefold() != "mixed":
            _require_celsius(result.unit, "source")
        _require_declared_field_unit(result.field_units, variable)
        selected_rows = [row for row in rows if row.get("variable") == variable]
        if not selected_rows:
            raise EnergyError(
                "insufficient_data", "Temperature input has no rows for the requested variable."
            )
        parsed = _parse_long_rows(selected_rows, timestamp)
    else:
        _require_celsius(result.unit, "source")
        _require_declared_field_unit(result.field_units, column)
        parsed = _parse_wide_rows(rows, timestamp, column)

    target_rows: list[Json] = []
    source_index = 0
    exact_targets = True
    for index in range(target_count):
        target = left + index * target_step
        while source_index + 1 < len(parsed) and parsed[source_index + 1][0] <= target:
            source_index += 1
        source_time, temperature = parsed[source_index]
        age = target - source_time
        if age < timedelta(0):
            raise EnergyError(
                "incomplete_coverage",
                "No temperature reading exists at or before the window start.",
            )
        if age >= timedelta(seconds=source_cadence):
            raise EnergyError(
                "stale_source_value",
                "A source temperature is missing or too stale for the requested window.",
            )
        exact_targets &= target == source_time
        target_rows.append({"timestamp": _iso(target), "temperature": temperature})

    output_kind = result.kind
    warnings = list(result.warnings)
    if result.kind is DataKind.METERED and not exact_targets:
        output_kind = DataKind.ESTIMATED
        warnings.append(
            "Metered temperature values were held forward to target starts without exact source "
            "readings; the aligned result is labeled estimated."
        )

    target_resolution = {15: "15min", 30: "30min", 60: "1h"}[interval_minutes]
    provenance = [
        *result.provenance,
        {
            "operation": "temperature_alignment",
            "version": 1,
            "artifact_id": artifact_id,
            "input_kind": result.kind.value,
            "input_source": result.source,
            "input_provenance": result.provenance,
            "variable": variable if long_form else column,
            "requested_bounds": {
                "start": _iso(left),
                "end": _iso(right),
                "end_exclusive": True,
            },
            "coverage": {
                "target_intervals": target_count,
                "aligned_intervals": len(target_rows),
                "source_resolution": result.resolution,
                "target_resolution": target_resolution,
            },
            "assumption": _ZERO_ORDER_HOLD,
            "output_kind": output_kind.value,
        },
    ]
    assumptions = [*result.assumptions, _ZERO_ORDER_HOLD]
    return EnergyResult(
        data=target_rows,
        kind=output_kind,
        unit="degC",
        source=result.source,
        timezone="UTC",
        resolution=target_resolution,
        provider=result.provider,
        site_id=result.site_id,
        asset_id=result.asset_id,
        time_start=left,
        time_end=right,
        quantity_shape="instantaneous",
        original_unit=result.original_unit or result.unit,
        field_units={"temperature": "degC"},
        retrieved_at=result.retrieved_at,
        assumptions=assumptions,
        warnings=warnings,
        quality=result.quality,
        provenance=provenance,
    )


def _source_cadence(value: str | None) -> int:
    if not isinstance(value, str) or value not in _SOURCE_CADENCES:
        raise EnergyError(
            "invalid_resolution",
            "Source resolution must be explicitly set to 15min, 30min, 60min or 1h.",
        )
    seconds = _SOURCE_CADENCES[value]
    if seconds <= 0:
        raise EnergyError("invalid_resolution", "Source resolution must be positive.")
    return seconds


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EnergyError(
            "invalid_timestamp", "Timestamps must be ISO-8601 strings with UTC offsets."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise EnergyError("invalid_timestamp", "Timestamp is not a valid ISO-8601 value.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", "Each timestamp must include an explicit UTC offset.")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_celsius(value: Any, label: str) -> None:
    if not isinstance(value, str) or value.strip().casefold() not in _CELSIUS_UNITS:
        raise EnergyError("unit_mismatch", f"{label} temperature units must be Celsius.")


def _require_declared_field_unit(field_units: dict[str, str], field: str) -> None:
    if field in field_units:
        _require_celsius(field_units[field], "Declared field")


def _temperature(value: Any) -> float:
    if value is None:
        raise EnergyError("missing_value", "A required temperature value is missing.")
    if isinstance(value, bool):
        raise EnergyError("invalid_value", "Boolean values are not valid temperatures.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise EnergyError("invalid_value", "A temperature value is not numeric.") from None
    if not isfinite(parsed):
        raise EnergyError("invalid_value", "Temperature values must be finite numbers.")
    return parsed


def _parse_wide_rows(
    rows: list[dict[str, Any]], timestamp: str, column: str
) -> list[tuple[datetime, float]]:
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        if timestamp not in row or column not in row:
            raise EnergyError(
                "column_not_found", "Each row requires timestamp and temperature columns."
            )
        point = _instant(row[timestamp])
        if parsed and point <= parsed[-1][0]:
            raise EnergyError(
                "invalid_timestamp_order",
                "Relevant temperature timestamps must be strictly increasing.",
            )
        parsed.append((point, _temperature(row[column])))
    return parsed


def _parse_long_rows(rows: list[dict[str, Any]], timestamp: str) -> list[tuple[datetime, float]]:
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        if timestamp not in row or "value" not in row or "unit" not in row:
            raise EnergyError(
                "column_not_found",
                "Each selected long-form row requires timestamp, value and unit.",
            )
        _require_celsius(row["unit"], "Row")
        point = _instant(row[timestamp])
        if parsed and point <= parsed[-1][0]:
            raise EnergyError(
                "invalid_timestamp_order",
                "Relevant temperature timestamps must be strictly increasing.",
            )
        parsed.append((point, _temperature(row["value"])))
    return parsed
