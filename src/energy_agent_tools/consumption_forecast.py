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
_MIN_HISTORY = timedelta(days=70)
_HOLDOUT = timedelta(days=14)
_SELECTION = timedelta(days=7)
_CALIBRATION = timedelta(days=7)
MAX_HISTORY_GAP = timedelta(days=7)
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
    "history_end",
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
    """Forecast interval energy using a weekly profile and optional temperature.

    The final fourteen days are divided chronologically: the first seven select
    the model and the last seven calibrate its error bands. The selected model is
    then refit on all history through ``history_end`` before it produces the
    requested forecast. If omitted, ``history_end`` defaults to forecast start;
    an explicit history end may be up to seven days before forecast start.
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
    history_end = _timestamp(parameters.get("history_end", parameters["start"]), "history end")
    if finish <= start:
        raise EnergyError("invalid_range", "Forecast end must be after forecast start.")
    if history_end > start:
        raise EnergyError("invalid_history_end", "History end must be at or before forecast start.")
    history_to_forecast_gap = start - history_end
    if history_to_forecast_gap > MAX_HISTORY_GAP:
        raise EnergyError(
            "stale_history", "History end must be within seven days before forecast start."
        )
    history_to_forecast_gap_seconds = int(history_to_forecast_gap.total_seconds())
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
    if rows[-1][1] - rows[0][0] < _MIN_HISTORY:
        raise EnergyError(
            "insufficient_history", "At least 70 days of contiguous metered history are required."
        )
    if rows[-1][1] > history_end:
        raise EnergyError(
            "history_after_cutoff", "Every historical interval must end by history_end."
        )
    if rows[-1][1] < history_end:
        raise EnergyError(
            "invalid_coverage", "History must cover a contiguous window through history_end."
        )

    holdout_rows = int(_HOLDOUT.total_seconds() // seconds)
    selection_rows_count = int(_SELECTION.total_seconds() // seconds)
    calibration_rows_count = int(_CALIBRATION.total_seconds() // seconds)
    if len(rows) <= holdout_rows:
        raise EnergyError("insufficient_history", "History is too short for chronological holdout.")
    selection_start = history_end - _HOLDOUT
    calibration_start = history_end - _CALIBRATION
    evaluation_training_rows = [row for row in rows if row[1] <= selection_start]
    selection_rows = [
        row for row in rows if row[0] >= selection_start and row[1] <= calibration_start
    ]
    calibration_rows = [
        row for row in rows if row[0] >= calibration_start and row[1] <= history_end
    ]
    if (
        len(selection_rows) != selection_rows_count
        or len(calibration_rows) != calibration_rows_count
    ):
        raise EnergyError(
            "insufficient_history", "Could not form complete weekly evaluation windows."
        )
    if evaluation_training_rows[-1][1] - evaluation_training_rows[0][0] < 8 * _WEEK:
        raise EnergyError(
            "insufficient_history", "At least eight complete weekly cycles are needed for training."
        )

    evaluation_profile = _calendar_profile(evaluation_training_rows, zone, interval_minutes)
    evaluation_training_values = [value for _, _, value in evaluation_training_rows]
    selection_values = [value for _, _, value in selection_rows]
    calibration_values = [value for _, _, value in calibration_rows]
    baseline_selection = [
        _profile_value(evaluation_profile, interval_start, zone, interval_minutes)
        for interval_start, _, _ in selection_rows
    ]
    baseline_calibration = [
        _profile_value(evaluation_profile, interval_start, zone, interval_minutes)
        for interval_start, _, _ in calibration_rows
    ]
    baseline_selection_mae = _mae(selection_values, baseline_selection)

    context_inputs: list[tuple[str, EnergyResult]] = []
    model_context: dict[str, Any] = {"evaluated": False}
    selected_method = "weekly_calendar_profile"
    selection_predictions = baseline_selection
    calibration_predictions = baseline_calibration
    historical_temperatures: list[float] | None = None
    future_temperatures: list[float] | None = None
    candidate_selection: list[float] | None = None
    candidate_calibration: list[float] | None = None
    candidate_selection_mae: float | None = None
    candidate_calibration_mae: float | None = None

    if historical_context is not None and future_context is not None:
        _validate_artifact(historical_context, "historical context")
        _validate_artifact(future_context, "future context")
        if historical_context[1].kind not in {DataKind.METERED, DataKind.ESTIMATED}:
            raise EnergyError(
                "invalid_context",
                "Historical temperature context must be observed or estimated analysis, not forecast.",
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
            raise EnergyError(
                "invalid_context", "Temperature context units must be degrees Celsius."
            )
        if _unit_key(future_context[1].unit) not in _TEMPERATURE_UNITS:
            raise EnergyError(
                "invalid_context", "Temperature context units must be degrees Celsius."
            )
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
            "selection": "evaluated_on_first_chronological_7_day_window",
        }

        selection_temperature_start = len(evaluation_training_rows)
        calibration_temperature_start = selection_temperature_start + len(selection_rows)
        evaluation_training_temperatures = historical_temperatures[:selection_temperature_start]
        selection_temperatures = historical_temperatures[
            selection_temperature_start:calibration_temperature_start
        ]
        calibration_temperatures = historical_temperatures[calibration_temperature_start:]
        evaluation_beta, evaluation_slot_temperature_means = _fit_temperature_adjustment(
            evaluation_training_rows,
            evaluation_training_values,
            evaluation_training_temperatures,
            evaluation_profile,
            zone,
            interval_minutes,
        )
        candidate_selection = _temperature_predictions(
            selection_rows,
            baseline_selection,
            selection_temperatures,
            evaluation_beta,
            evaluation_slot_temperature_means,
            zone,
            interval_minutes,
        )
        candidate_calibration = _temperature_predictions(
            calibration_rows,
            baseline_calibration,
            calibration_temperatures,
            evaluation_beta,
            evaluation_slot_temperature_means,
            zone,
            interval_minutes,
        )
        candidate_selection_mae = _mae(selection_values, candidate_selection)
        candidate_calibration_mae = _mae(calibration_values, candidate_calibration)
        improvement = baseline_selection_mae - candidate_selection_mae
        if improvement > max(1e-9, baseline_selection_mae * 0.02):
            selected_method = "temperature_adjusted_weekly_profile"
            selection_predictions = candidate_selection
            calibration_predictions = candidate_calibration
            model_context["applied_to_forecast"] = True
    calibration_errors = [
        _finite(actual - prediction, "calibration residual")
        for actual, prediction in zip(calibration_values, calibration_predictions, strict=True)
    ]
    band_width = _quantile([abs(error) for error in calibration_errors], coverage)

    final_profile = _calendar_profile(rows, zone, interval_minutes)
    final_temperature_beta = 0.0
    final_slot_temperature_means: dict[int, float] = {}
    if selected_method == "temperature_adjusted_weekly_profile":
        assert historical_temperatures is not None
        final_temperature_beta, final_slot_temperature_means = _fit_temperature_adjustment(
            rows,
            [value for _, _, value in rows],
            historical_temperatures,
            final_profile,
            zone,
            interval_minutes,
        )

    future_intervals = _future_intervals(start, forecast_count, interval)
    output: list[Json] = []
    for index, (interval_start, interval_end, _) in enumerate(future_intervals):
        point = _profile_value(final_profile, interval_start, zone, interval_minutes)
        if selected_method == "temperature_adjusted_weekly_profile":
            assert future_temperatures is not None
            key = _calendar_key(interval_start, zone, interval_minutes)
            temperature_delta = _finite(
                future_temperatures[index] - final_slot_temperature_means[key],
                "forecast temperature deviation",
            )
            temperature_effect = _finite(
                final_temperature_beta * temperature_delta, "forecast temperature adjustment"
            )
            point = _finite(point + temperature_effect, "forecast interval value")
        point = max(0.0, _finite(point, "forecast interval value"))
        lower = max(0.0, _finite(point - band_width, "forecast lower band"))
        upper = _finite(point + band_width, "forecast upper band")
        output.append(
            {
                "timestamp": interval_start.astimezone(zone).isoformat(),
                "end": interval_end.astimezone(zone).isoformat(),
                "value": point,
                "lower": lower,
                "upper": upper,
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
    total = _finite(sum(float(row["value"]) for row in output), "forecast total")
    heldout_actuals = selection_values + calibration_values
    heldout_baseline = baseline_selection + baseline_calibration
    heldout_selected = selection_predictions + calibration_predictions
    heldout_candidate = (
        None
        if candidate_selection is None or candidate_calibration is None
        else candidate_selection + candidate_calibration
    )
    combined_candidate_mae = (
        None if heldout_candidate is None else _mae(heldout_actuals, heldout_candidate)
    )
    model: Json = {
        "method": selected_method,
        "history_end": history_end.isoformat(),
        "forecast_start": start.isoformat(),
        "history_to_forecast_gap_seconds": history_to_forecast_gap_seconds,
        "training_start": evaluation_training_rows[0][0].isoformat(),
        "training_end": evaluation_training_rows[-1][1].isoformat(),
        "evaluation_training_rows": len(evaluation_training_rows),
        "evaluation_training_start": evaluation_training_rows[0][0].isoformat(),
        "evaluation_training_end": evaluation_training_rows[-1][1].isoformat(),
        "final_training_rows": len(rows),
        "final_training_start": rows[0][0].isoformat(),
        "final_training_end": rows[-1][1].isoformat(),
        "heldout_start": selection_rows[0][0].isoformat(),
        "heldout_end": calibration_rows[-1][1].isoformat(),
        "heldout_rows": len(selection_rows) + len(calibration_rows),
        "selection_start": selection_rows[0][0].isoformat(),
        "selection_end": selection_rows[-1][1].isoformat(),
        "selection_rows": len(selection_rows),
        "calibration_start": calibration_rows[0][0].isoformat(),
        "calibration_end": calibration_rows[-1][1].isoformat(),
        "calibration_rows": len(calibration_rows),
        "final_model_refit": "all_metered_history_after_selection_and_calibration",
        "metrics": {
            "selection_mae_kwh": _mae(selection_values, selection_predictions),
            "weekly_profile_selection_mae_kwh": baseline_selection_mae,
            "temperature_candidate_selection_mae_kwh": candidate_selection_mae,
            "calibration_mae_kwh": _mae(calibration_values, calibration_predictions),
            "weekly_profile_calibration_mae_kwh": _mae(calibration_values, baseline_calibration),
            "temperature_candidate_calibration_mae_kwh": candidate_calibration_mae,
            "heldout_mae_kwh": _mae(heldout_actuals, heldout_selected),
            "weekly_profile_mae_kwh": _mae(heldout_actuals, heldout_baseline),
            "temperature_candidate_mae_kwh": combined_candidate_mae,
        },
        "heldout_diagnostics_note": (
            "Combined heldout diagnostics include the model-selection window and are not "
            "independent benchmark scores."
        ),
        "context_used": selected_method == "temperature_adjusted_weekly_profile",
        "context": model_context,
        "temperature_effect_kwh_per_c": final_temperature_beta,
        "nominal_interval_coverage": coverage,
        "band_method": "symmetric empirical absolute errors from the independent final 7-day calibration window",
        "band_width_kwh": band_width,
        "validation_order": "chronological_7day_selection_then_7day_calibration",
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
            "The interval band uses the final 7 days of a frozen evaluation model selected on the preceding 7 days; nominal coverage is not guaranteed.",
            "After selection and calibration, the chosen point model is refit on all history; combined heldout diagnostics are not independent benchmark scores.",
            "Per-interval coverage does not describe the probability that the complete forecast horizon is covered.",
            *(assumption for _, context in context_inputs for assumption in context.assumptions),
            *(
                [
                    f"History ends {history_to_forecast_gap_seconds} seconds before forecast start; unobserved changes during this gap are not modeled."
                ]
                if history_to_forecast_gap_seconds
                else []
            ),
        ],
        warnings=list(
            dict.fromkeys(
                [
                    "This empirical estimate depends on the supplied data quality and representativeness; check source data and provider evidence before operational use.",
                    *(
                        [
                            f"History ends {history_to_forecast_gap_seconds} seconds before forecast start; unobserved changes during this gap are not modeled."
                        ]
                        if history_to_forecast_gap_seconds
                        else []
                    ),
                    *source_result.warnings,
                    *(warning for _, context in context_inputs for warning in context.warnings),
                    *(
                        [
                            "Historical temperature uses estimated analysis; context-conditioned validation does not establish future weather forecast accuracy."
                        ]
                        if historical_context is not None
                        and historical_context[1].kind == DataKind.ESTIMATED
                        else []
                    ),
                ]
            )
        ),
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
        converted_value = _finite(value * conversion, "history unit conversion")
        intervals.append((start, end, converted_value))
    intervals.sort(key=lambda item: item[0])
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise EnergyError("overlapping_intervals", "History must not overlap or duplicate.")
        if current[0] > previous[1]:
            raise EnergyError(
                "coverage_gap", "History must be contiguous with no missing intervals."
            )
    if result.time_start is not None and result.time_start.astimezone(UTC) != intervals[0][0]:
        raise EnergyError(
            "invalid_coverage", "Declared history start does not match its first interval."
        )
    if result.time_end is not None and result.time_end.astimezone(UTC) != intervals[-1][1]:
        raise EnergyError(
            "invalid_coverage", "Declared history end does not match its final interval."
        )
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
        raise EnergyError(
            "invalid_context", f"{label} must cover every required timestamp exactly once."
        )
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
        raise EnergyError(
            "invalid_context", f"{label} timestamps must match the required series exactly."
        )
    return [values[instant] for instant in expected]


def _matching_site(history: EnergyResult, context: EnergyResult, label: str) -> None:
    if history.site_id is None or context.site_id is None:
        raise EnergyError(
            "invalid_context", f"{label} context requires the same known site as history."
        )
    if (
        history.site_id is not None
        and context.site_id is not None
        and history.site_id != context.site_id
    ):
        raise EnergyError("invalid_context", f"{label} context belongs to a different site.")


def _calendar_profile(
    rows: list[_Interval], zone: ZoneInfo, interval_minutes: int
) -> dict[int, float]:
    totals: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for start, _, value in rows:
        key = _calendar_key(start, zone, interval_minutes)
        totals[key] = _finite(totals[key] + value, "weekly profile accumulation")
        counts[key] += 1
    if not totals:
        raise EnergyError("insufficient_history", "Could not build a weekly calendar profile.")
    return {
        key: _finite(total / counts[key], "weekly calendar profile")
        for key, total in totals.items()
    }


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
        raise EnergyError(
            "insufficient_history", "History does not cover a required weekly time slot."
        )
    return profile[key]


def _profile_predictions(
    profile: dict[int, float], rows: list[_Interval], zone: ZoneInfo, interval_minutes: int
) -> list[float]:
    return [_profile_value(profile, start, zone, interval_minutes) for start, _, _ in rows]


def _fit_temperature_adjustment(
    rows: list[_Interval],
    values: list[float],
    temperatures: list[float],
    profile: dict[int, float],
    zone: ZoneInfo,
    interval_minutes: int,
) -> tuple[float, dict[int, float]]:
    if len(rows) != len(values) or len(rows) != len(temperatures):
        raise EnergyError(
            "invalid_context", "Temperature rows must align with consumption history."
        )
    slot_values: dict[int, list[float]] = defaultdict(list)
    for (instant, _, _), temperature in zip(rows, temperatures, strict=True):
        slot_values[_calendar_key(instant, zone, interval_minutes)].append(temperature)
    slot_means = {
        key: _mean(slot_temperatures, "temperature profile mean")
        for key, slot_temperatures in slot_values.items()
    }
    baseline = _profile_predictions(profile, rows, zone, interval_minutes)
    residuals = [
        _finite(value - prediction, "training residual")
        for value, prediction in zip(values, baseline, strict=True)
    ]
    centered = [
        _finite(
            temperature - slot_means[_calendar_key(row[0], zone, interval_minutes)],
            "centered temperature",
        )
        for row, temperature in zip(rows, temperatures, strict=True)
    ]
    denominator = _finite(
        sum(_finite(value * value, "temperature covariance term") for value in centered),
        "temperature covariance",
    )
    if denominator <= 1e-12:
        return 0.0, slot_means
    numerator = _finite(
        sum(
            _finite(x * residual, "temperature regression term")
            for x, residual in zip(centered, residuals, strict=True)
        ),
        "temperature regression numerator",
    )
    coefficient = _finite(numerator / denominator, "temperature regression coefficient")
    return coefficient, slot_means


def _temperature_predictions(
    rows: list[_Interval],
    baseline: list[float],
    temperatures: list[float],
    coefficient: float,
    slot_means: dict[int, float],
    zone: ZoneInfo,
    interval_minutes: int,
) -> list[float]:
    predictions: list[float] = []
    for row, profile_value, temperature in zip(rows, baseline, temperatures, strict=True):
        key = _calendar_key(row[0], zone, interval_minutes)
        delta = _finite(temperature - slot_means[key], "temperature deviation")
        adjustment = _finite(coefficient * delta, "temperature adjustment")
        prediction = _finite(profile_value + adjustment, "temperature candidate prediction")
        predictions.append(max(0.0, prediction))
    return predictions


def _future_intervals(start: datetime, count: int, interval: timedelta) -> list[_Interval]:
    return [(begin := start + index * interval, begin + interval, 0.0) for index in range(count)]


def _mae(actual: list[float], predicted: list[float]) -> float:
    if len(actual) != len(predicted) or not actual:
        raise EnergyError(
            "insufficient_history", "Chronological validation set is empty or mismatched."
        )
    errors = [
        _finite(abs(a - p), "absolute validation error")
        for a, p in zip(actual, predicted, strict=True)
    ]
    total_error = _finite(sum(errors), "validation error accumulation")
    return _finite(total_error / len(actual), "validation mean absolute error")


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise EnergyError("insufficient_history", "No held-out residuals are available for bands.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return _finite(
        ordered[lower_index] + fraction * (ordered[upper_index] - ordered[lower_index]),
        "empirical error quantile",
    )


def _mean(values: list[float], label: str) -> float:
    try:
        result = mean(values)
    except (OverflowError, ValueError) as exc:
        raise EnergyError(
            "numeric_overflow", f"{label} overflowed or could not be computed."
        ) from exc
    return _finite(float(result), label)


def _finite(value: float, label: str) -> float:
    if not isfinite(value):
        raise EnergyError("numeric_overflow", f"{label} overflowed or became non-finite.")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError(
            "invalid_timestamp", f"{label} must be an offset-aware ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", f"{label} must include an explicit UTC offset.")
    return parsed.astimezone(UTC)


def _number(value: Any, *, allow_missing: bool) -> float | None:
    if value is None:
        if allow_missing:
            return None
        raise EnergyError("missing_value", "A numeric value is required for every interval.")
    if isinstance(value, bool) or isinstance(value, (bytes, bytearray)):
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
        raise EnergyError(
            "invalid_parameters", "coverage must be a number strictly between 0 and 1."
        )
    parsed = float(value)
    if not isfinite(parsed) or parsed <= 0 or parsed >= 1:
        raise EnergyError(
            "invalid_parameters", "coverage must be a number strictly between 0 and 1."
        )
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
