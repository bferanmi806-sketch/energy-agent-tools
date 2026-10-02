"""Provider-independent, offline household and site consumption forecasting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from numbers import Real
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import DataKind, EnergyError, EnergyResult, Json

_WEEK = timedelta(days=7)
_MIN_HISTORY = timedelta(days=90)
_HOLDOUT = timedelta(days=14)
_MAX_HISTORY_ROWS = 100_000
_MAX_FORECAST_ROWS = 10_000
_MAX_HORIZON = timedelta(days=31)
_INTERVAL_MINUTES = {15, 30, 60}
_ENERGY_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}
_TEMPERATURE_UNITS = {"c", "°c", "degc", "celsius"}
_ALLOWED_PARAMETERS = {
    "start",
    "end",
    "timezone",
    "interval_minutes",
    "column",
    "timestamp",
    "end_column",
    "coverage",
    "temperature_column",
    "context_timestamp",
}

_Interval = tuple[datetime, datetime, float]


def forecast(
    history: tuple[str, EnergyResult],
    parameters: Json,
    *,
    historical_context: tuple[str, EnergyResult] | None = None,
    future_context: tuple[str, EnergyResult] | None = None,
) -> EnergyResult:
    """Forecast interval energy using a weekly calendar profile and optional temperature.

    The final fourteen days are a chronological validation and calibration set. No
    future context enters training: it is only applied after a temperature model
    earns its place by improving that held-out period.
    """

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Forecast parameters must be an object.")
    unknown = set(parameters) - _ALLOWED_PARAMETERS
    if unknown:
        raise EnergyError("invalid_parameters", "Forecast parameters contain unsupported fields.")
    required = {"start", "end", "timezone", "interval_minutes"}
    if not required <= parameters.keys():
        raise EnergyError(
            "invalid_parameters", "start, end, timezone and interval_minutes are required."
        )
    _validate_artifact(history, "history")
    if (historical_context is None) != (future_context is None):
        raise EnergyError(
            "missing_paired_context",
            "Temperature modeling requires both historical and future context artifacts.",
        )

    timezone_name = parameters["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name:
        raise EnergyError("invalid_parameters", "timezone must be a valid IANA timezone name.")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EnergyError(
            "invalid_parameters", "timezone must be a valid IANA timezone name."
        ) from exc

    interval_minutes = parameters["interval_minutes"]
    if (
        isinstance(interval_minutes, bool)
        or not isinstance(interval_minutes, int)
        or interval_minutes not in _INTERVAL_MINUTES
    ):
        raise EnergyError("invalid_parameters", "interval_minutes must be 15, 30 or 60.")
    interval = timedelta(minutes=interval_minutes)
    seconds = int(interval.total_seconds())
    column = _parameter_name(parameters, "column", "value")
    timestamp_column = _parameter_name(parameters, "timestamp", "timestamp")
    end_column = _parameter_name(parameters, "end_column", "end")
    context_timestamp = _parameter_name(parameters, "context_timestamp", "timestamp")
    temperature_column = _parameter_name(parameters, "temperature_column", "value")
    coverage = _coverage(parameters.get("coverage", 0.9))

    start = _timestamp(parameters["start"], "forecast start")
    finish = _timestamp(parameters["end"], "forecast end")
    if finish <= start:
        raise EnergyError("invalid_range", "Forecast end must be after forecast start.")
    duration = finish - start
    if duration > _MAX_HORIZON:
        raise EnergyError("horizon_too_large", "Forecast horizon must not exceed 31 days.")
    if duration.total_seconds() % seconds:
        raise EnergyError(
            "invalid_range", "Forecast horizon must contain a whole number of intervals."
        )
    forecast_count = int(duration.total_seconds() // seconds)
    if forecast_count > _MAX_FORECAST_ROWS:
        raise EnergyError("output_too_large", "Forecast exceeds the 10,000 interval limit.")

    source_result = history[1]
    rows = _energy_intervals(
        source_result,
        column=column,
        timestamp_column=timestamp_column,
        end_column=end_column,
        interval=interval,
    )
    if len(rows) > _MAX_HISTORY_ROWS:
        raise EnergyError("input_too_large", "History exceeds the 100,000 row limit.")
    if rows[-1][1] > start:
        raise EnergyError(
            "history_after_forecast_start", "Every historical interval must end by forecast start."
        )
    if rows[-1][1] - rows[0][0] < _MIN_HISTORY:
        raise EnergyError(
            "insufficient_history", "At least 90 days of contiguous metered history are required."
        )

    holdout_rows = int(_HOLDOUT.total_seconds() // seconds)
    if len(rows) <= holdout_rows:
        raise EnergyError("insufficient_history", "History is too short for chronological holdout.")
    training_rows = rows[:-holdout_rows]
    validation_rows = rows[-holdout_rows:]
    if training_rows[-1][1] - training_rows[0][0] < 8 * _WEEK:
        raise EnergyError(
            "insufficient_history", "At least eight complete weekly cycles are needed for training."
        )

    profile = _calendar_profile(training_rows, zone, interval_minutes)
    training_values = [value for _, _, value in training_rows]
    validation_values = [value for _, _, value in validation_rows]
    baseline_validation = [
        _profile_value(profile, start_time, zone, interval_minutes)
        for start_time, _, _ in validation_rows
    ]
    baseline_mae = _mae(validation_values, baseline_validation)

    context_inputs: list[tuple[str, EnergyResult]] = []
    model_context: dict[str, Any] = {"evaluated": False}
    temperature_beta = 0.0
    selected_method = "weekly_calendar_profile"
    validation_predictions = baseline_validation
    historical_temperatures: list[float] | None = None
    future_temperatures: list[float] | None = None
    temperature_mae: float | None = None

    if historical_context is not None and future_context is not None:
        _validate_artifact(historical_context, "historical context")
        _validate_artifact(future_context, "future context")
        if historical_context[1].kind != DataKind.METERED:
            raise EnergyError(
                "invalid_context", "Historical temperature context must be observed, not forecast."
            )
        if future_context[1].kind != DataKind.FORECAST:
            raise EnergyError(
                "invalid_context", "Future temperature context must have forecast kind."
            )
        _matching_site(source_result, historical_context[1], "historical")
        _matching_site(source_result, future_context[1], "future")
        historical_temperatures = _context_values(
            historical_context[1],
            expected=[row[0] for row in rows],
            timestamp_column=context_timestamp,
            value_column=temperature_column,
            label="historical temperature",
        )
        future_intervals = _future_intervals(start, forecast_count, interval)
        future_temperatures = _context_values(
            future_context[1],
            expected=[row[0] for row in future_intervals],
            timestamp_column=context_timestamp,
            value_column=temperature_column,
            label="future temperature",
        )
        if _unit_key(historical_context[1].unit) not in _TEMPERATURE_UNITS:
            raise EnergyError("invalid_context", "Temperature context units must be degrees Celsius.")
        if _unit_key(future_context[1].unit) not in _TEMPERATURE_UNITS:
            raise EnergyError("invalid_context", "Temperature context units must be degrees Celsius.")
        context_inputs = [historical_context, future_context]
        model_context = {
            "evaluated": True,
            "applied_to_forecast": False,
            "historical_artifact_id": historical_context[0],
            "future_artifact_id": future_context[0],
            "historical_kind": historical_context[1].kind.value,
            "future_kind": future_context[1].kind.value,
            "units": historical_context[1].unit,
            "feature": "temperature_celsius",
            "selection": "evaluated_on_chronological_holdout",
        }

        train_temperatures = historical_temperatures[:-holdout_rows]
        validation_temperatures = historical_temperatures[-holdout_rows:]
        slot_temperatures: dict[int, list[float]] = defaultdict(list)
        for (interval_start, _, _), temperature in zip(
            training_rows, train_temperatures, strict=True
        ):
            slot_temperatures[_calendar_key(interval_start, zone, interval_minutes)].append(
                temperature
            )
        slot_temperature_means = {
            key: mean(values) for key, values in slot_temperatures.items()
        }
        residuals = [actual - baseline for actual, baseline in zip(
            training_values, _profile_predictions(profile, training_rows, zone, interval_minutes), strict=True
        )]
        centered_temperature = [
            value
            - slot_temperature_means[
                _calendar_key(row[0], zone, interval_minutes)
            ]
            for row, value in zip(training_rows, train_temperatures, strict=True)
        ]
        denominator = sum(value * value for value in centered_temperature)
        if denominator > 1e-12:
            temperature_beta = sum(
                x * residual for x, residual in zip(centered_temperature, residuals, strict=True)
            ) / denominator
        candidate_validation = [
            max(
                0.0,
                baseline
                + temperature_beta
                * (
                    temperature
                    - slot_temperature_means[
                        _calendar_key(row[0], zone, interval_minutes)
                    ]
                ),
            )
            for row, baseline, temperature in zip(
                validation_rows,
                baseline_validation,
                validation_temperatures,
                strict=True,
            )
        ]
        temperature_mae = _mae(validation_values, candidate_validation)
        improvement = baseline_mae - temperature_mae
        if improvement > max(1e-9, baseline_mae * 0.02):
            selected_method = "temperature_adjusted_weekly_profile"
            validation_predictions = candidate_validation
            model_context["applied_to_forecast"] = True
        else:
            temperature_beta = 0.0

    validation_errors = [
        actual - prediction
        for actual, prediction in zip(validation_values, validation_predictions, strict=True)
    ]
    band_width = _quantile([abs(error) for error in validation_errors], coverage)
    future_intervals = _future_intervals(start, forecast_count, interval)
    output: list[Json] = []
    for index, (interval_start, interval_end, _) in enumerate(future_intervals):
        point = _profile_value(profile, interval_start, zone, interval_minutes)
        if selected_method == "temperature_adjusted_weekly_profile":
            assert future_temperatures is not None
            key = _calendar_key(interval_start, zone, interval_minutes)
            point += temperature_beta * (
                future_temperatures[index] - slot_temperature_means[key]
            )
        point = max(0.0, point)
        output.append(
            {
                "timestamp": interval_start.astimezone(zone).isoformat(),
                "end": interval_end.astimezone(zone).isoformat(),
                "value": point,
                "lower": max(0.0, point - band_width),
                "upper": point + band_width,
            }
        )

    lineage_inputs = [history, *context_inputs]
    provenance: list[Json] = [
        {
            "operation": "consumption_forecast",
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
                for artifact_id, result in lineage_inputs
            ],
        }
    ]
    total = sum(float(row["value"]) for row in output)
    model: Json = {
        "method": selected_method,
        "training_start": training_rows[0][0].isoformat(),
        "training_end": training_rows[-1][1].isoformat(),
        "heldout_start": validation_rows[0][0].isoformat(),
        "heldout_end": validation_rows[-1][1].isoformat(),
        "heldout_rows": len(validation_rows),
        "metrics": {
            "heldout_mae_kwh": _mae(validation_values, validation_predictions),
            "weekly_profile_mae_kwh": baseline_mae,
            "temperature_candidate_mae_kwh": temperature_mae,
        },
        "context_used": selected_method == "temperature_adjusted_weekly_profile",
        "context": model_context,
        "temperature_effect_kwh_per_c": temperature_beta,
        "nominal_interval_coverage": coverage,
        "band_method": "symmetric empirical absolute heldout errors",
        "band_width_kwh": band_width,
        "validation_order": "chronological_last_14_days",
    }
    return EnergyResult(
        data={
            "intervals": output,
            "summary": {
                "total_kwh": total,
                "interval_count": len(output),
                "start": start.isoformat(),
                "end": finish.isoformat(),
            },
            "model": model,
        },
        kind=DataKind.FORECAST,
        unit="kWh",
        source="offline_consumption_forecast",
        timezone=timezone_name,
        resolution=f"{interval_minutes}min",
        provider=source_result.provider,
        site_id=source_result.site_id,
        asset_id=source_result.asset_id,
        time_start=start,
        time_end=finish,
        quantity_shape="interval",
        original_unit=source_result.unit,
        field_units={"value": "kWh", "lower": "kWh", "upper": "kWh"},
        assumptions=[
            "The weekly calendar profile and optional temperature adjustment are empirical models of past consumption.",
            "The interval band is calibrated from the final 14 days of historical data; nominal coverage is not guaranteed.",
            "Per-interval coverage does not describe the probability that the complete forecast horizon is covered.",
        ],
        warnings=[
            "This offline forecast is provider-independent and has no live provider evidence."
        ],
        quality="empirical_forecast",
        provenance=provenance,
    )


def _validate_artifact(artifact: tuple[str, EnergyResult], label: str) -> None:
    if (
        not isinstance(artifact, tuple)
        or len(artifact) != 2
        or not isinstance(artifact[0], str)
        or not artifact[0].strip()
        or not isinstance(artifact[1], EnergyResult)
    ):
        raise EnergyError("invalid_input", f"{label} must include an artifact ID and EnergyResult.")


def _energy_intervals(
    result: EnergyResult,
    *,
    column: str,
    timestamp_column: str,
    end_column: str,
    interval: timedelta,
) -> list[_Interval]:
    if result.kind != DataKind.METERED:
        raise EnergyError("invalid_input", "Forecast history must have metered data kind.")
    if result.quantity_shape != "interval":
        raise EnergyError("invalid_shape", "Forecast history must contain interval energy.")
    unit = _unit_key(result.unit)
    if unit not in _ENERGY_TO_KWH:
        raise EnergyError("invalid_unit", "History units must be Wh, kWh or MWh interval energy.")
    if result.resolution is not None:
        declared_minutes = _resolution_minutes(result.resolution)
        if declared_minutes is not None and declared_minutes != int(interval.total_seconds() // 60):
            raise EnergyError(
                "invalid_interval", "Declared history resolution must match interval_minutes."
            )
    conversion = _ENERGY_TO_KWH[unit]
    raw_rows = _table(result.data)
    if not raw_rows:
        raise EnergyError("insufficient_history", "History must contain interval rows.")
    if len(raw_rows) > _MAX_HISTORY_ROWS:
        raise EnergyError("input_too_large", "History exceeds the 100,000 row limit.")
    intervals: list[_Interval] = []
    for row in raw_rows:
        if any(name not in row for name in (timestamp_column, end_column, column)):
            raise EnergyError("column_not_found", "History needs value, timestamp and end columns.")
        start = _timestamp(row[timestamp_column], "historical timestamp")
        end = _timestamp(row[end_column], "historical interval end")
        if end - start != interval:
            raise EnergyError(
                "invalid_interval", "Each historical interval must match interval_minutes exactly."
            )
        value = _number(row[column], allow_missing=False)
        assert value is not None
        if value < 0:
            raise EnergyError("invalid_value", "Historical energy must be nonnegative.")
        intervals.append((start, end, value * conversion))
    intervals.sort(key=lambda item: item[0])
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise EnergyError("overlapping_intervals", "History must not overlap or duplicate.")
        if current[0] > previous[1]:
            raise EnergyError("coverage_gap", "History must be contiguous with no missing intervals.")
    if result.time_start is not None and result.time_start.astimezone(UTC) != intervals[0][0]:
        raise EnergyError("invalid_coverage", "Declared history start does not match its first interval.")
    if result.time_end is not None and result.time_end.astimezone(UTC) != intervals[-1][1]:
        raise EnergyError("invalid_coverage", "Declared history end does not match its final interval.")
    return intervals


def _table(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, dict):
        data = data.get("intervals", data.get("rows"))
    if not isinstance(data, list) or any(not isinstance(row, Mapping) for row in data):
        raise EnergyError("invalid_shape", "Time-series data must be a list of row objects.")
    return data


def _context_values(
    result: EnergyResult,
    *,
    expected: list[datetime],
    timestamp_column: str,
    value_column: str,
    label: str,
) -> list[float]:
    rows = _table(result.data)
    if len(rows) != len(expected):
        raise EnergyError("invalid_context", f"{label} must cover every required timestamp exactly once.")
    values: dict[datetime, float] = {}
    for row in rows:
        if timestamp_column not in row or value_column not in row:
            raise EnergyError("column_not_found", f"{label} timestamp or value column is missing.")
        instant = _timestamp(row[timestamp_column], f"{label} timestamp")
        value = _number(row[value_column], allow_missing=False)
        assert value is not None
        if instant in values:
            raise EnergyError("invalid_context", f"{label} contains a duplicate timestamp.")
        values[instant] = value
    if set(values) != set(expected):
        raise EnergyError("invalid_context", f"{label} timestamps must match the required series exactly.")
    return [values[instant] for instant in expected]


def _matching_site(history: EnergyResult, context: EnergyResult, label: str) -> None:
    if history.site_id is not None and context.site_id is not None and history.site_id != context.site_id:
        raise EnergyError("invalid_context", f"{label} context belongs to a different site.")
    if history.asset_id is not None and context.asset_id is not None and history.asset_id != context.asset_id:
        raise EnergyError("invalid_context", f"{label} context belongs to a different asset.")


def _calendar_profile(
    rows: list[_Interval], zone: ZoneInfo, interval_minutes: int
) -> dict[int, float]:
    totals: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for start, _, value in rows:
        key = _calendar_key(start, zone, interval_minutes)
        totals[key] += value
        counts[key] += 1
    if not totals:
        raise EnergyError("insufficient_history", "Could not build a weekly calendar profile.")
    return {key: total / counts[key] for key, total in totals.items()}


def _calendar_key(instant: datetime, zone: ZoneInfo, interval_minutes: int) -> int:
    local = instant.astimezone(zone)
    slots_per_day = 24 * 60 // interval_minutes
    slot = (local.hour * 60 + local.minute) // interval_minutes
    return local.weekday() * slots_per_day + slot


def _profile_value(
    profile: dict[int, float], instant: datetime, zone: ZoneInfo, interval_minutes: int
) -> float:
    key = _calendar_key(instant, zone, interval_minutes)
    if key not in profile:
        raise EnergyError("insufficient_history", "History does not cover a required weekly time slot.")
    return profile[key]


def _profile_predictions(
    profile: dict[int, float], rows: list[_Interval], zone: ZoneInfo, interval_minutes: int
) -> list[float]:
    return [
        _profile_value(profile, start, zone, interval_minutes) for start, _, _ in rows
    ]


def _future_intervals(
    start: datetime, count: int, interval: timedelta
) -> list[_Interval]:
    return [
        (begin := start + index * interval, begin + interval, 0.0)
        for index in range(count)
    ]


def _mae(actual: list[float], predicted: list[float]) -> float:
    if len(actual) != len(predicted) or not actual:
        raise EnergyError("insufficient_history", "Chronological validation set is empty or mismatched.")
    return sum(abs(a - p) for a, p in zip(actual, predicted, strict=True)) / len(actual)


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise EnergyError("insufficient_history", "No held-out residuals are available for bands.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (ordered[upper_index] - ordered[lower_index])


def _timestamp(value: Any, label: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError("invalid_timestamp", f"{label} must be an offset-aware ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", f"{label} must include an explicit UTC offset.")
    return parsed.astimezone(UTC)


def _number(value: Any, *, allow_missing: bool) -> float | None:
    if value is None:
        if allow_missing:
            return None
        raise EnergyError("missing_value", "A numeric value is required for every interval.")
    if isinstance(value, bool) or isinstance(value, (str, bytes, bytearray)):
        raise EnergyError("invalid_value", "Series values must be finite numbers.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError("invalid_value", "Series values must be finite numbers.") from exc
    if not isfinite(parsed):
        raise EnergyError("invalid_value", "Series values must be finite numbers.")
    return parsed


def _parameter_name(parameters: Json, key: str, default: str) -> str:
    value = parameters.get(key, default)
    if not isinstance(value, str) or not value:
        raise EnergyError("invalid_parameters", f"{key} must be a nonempty column name.")
    return value


def _coverage(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise EnergyError("invalid_parameters", "coverage must be a number strictly between 0 and 1.")
    parsed = float(value)
    if not isfinite(parsed) or parsed <= 0 or parsed >= 1:
        raise EnergyError("invalid_parameters", "coverage must be a number strictly between 0 and 1.")
    return parsed


def _unit_key(unit: str) -> str:
    return unit.strip().lower().replace(" ", "")


def _resolution_minutes(resolution: str) -> int | None:
    normalized = resolution.strip().lower().replace(" ", "")
    if normalized.endswith("minutes"):
        return _positive_integer(normalized[: -len("minutes")])
    if normalized.endswith("minute"):
        return _positive_integer(normalized[: -len("minute")])
    if normalized.endswith("min"):
        return _positive_integer(normalized[:-3])
    if normalized.endswith("m"):
        return _positive_integer(normalized[:-1])
    if normalized.endswith("h"):
        hours = _positive_integer(normalized[:-1])
        return None if hours is None else hours * 60
    if normalized.endswith("s"):
        seconds = _positive_integer(normalized[:-1])
        return None if seconds is None or seconds % 60 else seconds // 60
    return None


def _positive_integer(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
