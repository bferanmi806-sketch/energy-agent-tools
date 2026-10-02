"""Evidence-limited explanations for historical interval-energy spikes."""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any

import pandas as pd

from .models import DataKind, EnergyError, EnergyResult, Json
from .timeseries import (
    MAX_BASELINE_WINDOW,
    MAX_OUTPUT_ROWS,
    _derived,
    _name,
    _optional_ends,
    _parse_timestamp,
    _series,
    _timestamp_text,
    _unit_info,
    numeric_value,
)

MAX_EQUIPMENT_INPUTS = 10
MIN_WEATHER_OBSERVATIONS = 8
MIN_WEATHER_CORRELATION = 0.6
MIN_WEATHER_DEVIATION_C = 2.0


def explain(
    consumption: tuple[str, EnergyResult],
    parameters: Json,
    *,
    weather: tuple[str, EnergyResult] | None = None,
    equipment: list[tuple[str, EnergyResult]] | None = None,
) -> EnergyResult:
    """Find historical load spikes and report coincident evidence without causal claims."""

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Operation parameters must be an object.")
    inputs = [consumption]
    if weather is not None:
        inputs.append(weather)
    if equipment is not None and not isinstance(equipment, list):
        raise EnergyError("invalid_input", "Equipment inputs must be a list.")
    equipment_inputs = equipment if equipment is not None else []
    if len(equipment_inputs) > MAX_EQUIPMENT_INPUTS:
        raise EnergyError("input_too_large", "At most 10 equipment inputs are supported.")
    inputs.extend(equipment_inputs)
    if any(
        not isinstance(artifact_id, str) or not artifact_id or not isinstance(result, EnergyResult)
        for artifact_id, result in inputs
    ):
        raise EnergyError("invalid_input", "Each input must include an artifact and result.")

    column = _name(parameters, "column", "value")
    timestamp = _name(parameters, "timestamp", "timestamp")
    end_column = _name(parameters, "end_column", "end")
    window = _window(parameters)
    spike_ratio = _positive_number(parameters, "spike_ratio", 2.0, minimum=1.0, exclusive=True)
    min_excess_kwh = _positive_number(parameters, "min_excess_kwh", 0.1)

    load = _series(
        *consumption,
        {"column": column, "timestamp": timestamp},
    )
    _require_historical_energy(load.result, "Consumption")
    if load.result.quantity_shape != "interval":
        raise EnergyError(
            "quantity_shape_mismatch", "Consumption must be explicitly declared as interval energy."
        )
    load_unit = _energy_unit(load.result, "Consumption")
    load_ends = _optional_ends(load, end_column)
    if load_ends is None:
        raise EnergyError(
            "missing_interval_end", "Consumption must provide an explicit end for every interval."
        )
    if len(load.times) > MAX_OUTPUT_ROWS:
        raise EnergyError("output_too_large", "Spike analysis exceeds the row limit.")

    load_kwh = _finite_nonnegative_values(load.values, "Consumption")
    _require_consistent_interval_duration(load.times, load_ends, "Consumption")
    load_kwh = _convert_values(load_kwh, load_unit.to_base, "Consumption")
    load_baseline = _rolling_prior_mean(load_kwh, window)
    intervals = _load_intervals(
        load, load_ends, load_kwh, load_baseline, spike_ratio, min_excess_kwh
    )
    spikes = [row for row in intervals if row["is_spike"]]

    equipment_series = [
        _prepare_equipment(
            item,
            parameters,
            load,
            load_ends,
            end_column,
            load_kwh,
            window,
        )
        for item in equipment_inputs
    ]
    for index, site_value in enumerate(load_kwh):
        equipment_total = sum(item["values_kwh"][index] for item in equipment_series)
        if not isfinite(equipment_total) or equipment_total > site_value + 1e-9:
            raise EnergyError(
                "incompatible_equipment",
                "Equipment totals exceed consumption for at least one exact interval; check for overlapping submeters or incompatible inputs.",
            )

    weather_data: dict[pd.Timestamp, float | None] = {}
    weather_note: str | None = None
    weather_correlation: float | None = None
    weather_mean_c: float | None = None
    weather_count = 0
    if weather is not None:
        weather_data, weather_note = _prepare_weather(weather, parameters, load)
        if weather_note is None:
            eligible = [
                (weather_data[instant], value)
                for instant, value, baseline_row in zip(
                    load.times, load_kwh, intervals, strict=True
                )
                if baseline_row["baseline_kwh"] is not None
                and not baseline_row["is_spike"]
                and instant in weather_data
                and weather_data[instant] is not None
            ]
            weather_count = len(eligible)
            if weather_count:
                temperatures = [cast_float(item[0]) for item in eligible]
                observations = [cast_float(item[1]) for item in eligible]
                weather_mean_c = _mean(temperatures)
                weather_correlation = _correlation(temperatures, observations)

    for spike in spikes:
        instant = _parse_timestamp(spike["timestamp"])
        excess = spike["excess_kwh"]
        evidence: list[Json] = []
        missing: list[str] = []

        if not equipment_series:
            missing.append("No equipment interval data was supplied.")
        for item in equipment_series:
            baseline = item["baseline"][spike["_index"]]
            current = item["values_kwh"][spike["_index"]]
            if baseline is None:
                missing.append(
                    f"Equipment artifact {item['artifact_id']} lacks {window} prior observations."
                )
                continue
            increase = current - baseline
            if increase <= 0:
                missing.append(
                    f"Equipment artifact {item['artifact_id']} had no positive coincident increase."
                )
                continue
            bounded = min(increase, excess)
            evidence.append(
                {
                    "type": "coincident_equipment_increase",
                    "artifact_id": item["artifact_id"],
                    "asset_id": item["result"].asset_id,
                    "kind": item["result"].kind.value,
                    "unit": item["result"].unit,
                    "baseline_kwh": baseline,
                    "interval_kwh": current,
                    "increase_kwh": increase,
                    "overlap_with_site_excess_kwh": bounded,
                    "share_of_site_excess_upper_bound": bounded / excess if excess > 0 else None,
                    "interpretation": "Coincident measured increase; this does not establish cause.",
                }
            )

        if weather is None:
            missing.append("No weather series was supplied.")
        elif weather_note is not None:
            missing.append(weather_note)
        elif instant not in weather_data or weather_data[instant] is None:
            missing.append("No finite weather reading matched this exact interval start.")
        elif weather_count < MIN_WEATHER_OBSERVATIONS:
            missing.append(
                f"Only {weather_count} non-spike matched weather observations are available; "
                f"at least {MIN_WEATHER_OBSERVATIONS} are required."
            )
        elif weather_correlation is None:
            missing.append("Temperature or load did not vary enough to estimate an association.")
        elif abs(weather_correlation) < MIN_WEATHER_CORRELATION:
            missing.append(
                f"Non-spike temperature/load correlation ({weather_correlation:.3f}) is below "
                f"the {MIN_WEATHER_CORRELATION:.1f} reporting threshold."
            )
        else:
            temperature = weather_data[instant]
            assert temperature is not None and weather_mean_c is not None
            deviation = temperature - weather_mean_c
            if abs(deviation) < MIN_WEATHER_DEVIATION_C:
                missing.append(
                    f"Spike temperature differs by less than {MIN_WEATHER_DEVIATION_C:.1f}°C "
                    "from the non-spike mean."
                )
            elif deviation * weather_correlation <= 0:
                missing.append(
                    "Spike temperature moved opposite to the direction associated with higher load."
                )
            else:
                evidence.append(
                    {
                        "type": "observed_weather_association",
                        "artifact_id": weather[0],
                        "kind": weather[1].kind.value,
                        "unit": weather[1].unit,
                        "temperature_c": temperature,
                        "non_spike_mean_c": weather_mean_c,
                        "deviation_from_non_spike_mean_c": deviation,
                        "non_spike_observations": weather_count,
                        "temperature_load_correlation": weather_correlation,
                        "interpretation": (
                            "Observed weather association in supplied history; this does not "
                            "establish cause."
                        ),
                    }
                )

        spike.pop("_index")
        spike["supported_explanations"] = evidence
        spike["missing_evidence"] = missing

    for index, row in enumerate(intervals):
        row.pop("_index", None)
        row["timestamp"] = _timestamp_text(load.times[index], load.result.timezone)
        row["end"] = _timestamp_text(load_ends[index], load.result.timezone)

    inputs_for_result = [consumption]
    if weather is not None:
        inputs_for_result.append(weather)
    inputs_for_result.extend(equipment_inputs)
    warnings: list[str] = []
    if weather is not None and weather_note is not None:
        warnings.append(weather_note)
    if any(
        end != next_start for end, next_start in zip(load_ends[:-1], load.times[1:], strict=True)
    ):
        warnings.append(
            "Gaps between supplied load intervals were retained; no values were filled."
        )

    return _derived(
        "spike_analysis",
        {
            "intervals": intervals,
            "spikes": spikes,
            "summary": {
                "interval_count": len(intervals),
                "baseline_count": sum(row["baseline_kwh"] is not None for row in intervals),
                "spike_count": len(spikes),
                "window_observations": window,
                "spike_ratio_threshold": spike_ratio,
                "minimum_excess_kwh": min_excess_kwh,
                "equipment_inputs_are_not_aggregated": True,
                "weather_non_spike_observations": weather_count,
                "weather_temperature_load_correlation": weather_correlation,
            },
        },
        inputs_for_result,
        "kWh",
        resolution=load.result.resolution,
        assumptions=[
            f"The baseline is the mean of the previous {window} supplied observations; the current interval is excluded.",
            f"A spike requires a positive excess of at least {min_excess_kwh:g} kWh and a load-to-baseline ratio of at least {spike_ratio:g}; a zero baseline uses the absolute excess threshold because a ratio is undefined.",
            "Equipment increases and temperature/load associations are coincident evidence only; neither establishes causation.",
            "Equipment evidence is evaluated per artifact and is not summed because meter overlap is not declared.",
            f"Weather association requires at least {MIN_WEATHER_OBSERVATIONS} matched non-spike observations, absolute correlation of at least {MIN_WEATHER_CORRELATION:.1f}, and a spike temperature at least {MIN_WEATHER_DEVIATION_C:.1f}°C from the non-spike mean in the associated direction.",
            "Timestamps were matched exactly in UTC; no interpolation, resampling, or missing-value filling was performed.",
        ],
        warnings=warnings,
        field_units={
            "load_kwh": "kWh",
            "baseline_kwh": "kWh",
            "residual_kwh": "kWh",
            "excess_kwh": "kWh",
            "temperature_c": "°C",
            "increase_kwh": "kWh",
            "overlap_with_site_excess_kwh": "kWh",
        },
        quantity_shape="interval",
    )


def _window(parameters: Json) -> int:
    value = parameters.get("window", 4)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BASELINE_WINDOW
    ):
        raise EnergyError("invalid_parameters", "Window must be an integer from 1 to 10000.")
    return value


def _positive_number(
    parameters: Json,
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    exclusive: bool = False,
) -> float:
    value = numeric_value(parameters.get(name, default), allow_missing=False)
    assert value is not None
    if value < minimum or (exclusive and value == minimum):
        operator = "greater than" if exclusive else "at least"
        raise EnergyError("invalid_parameters", f"{name} must be {operator} {minimum:g}.")
    return value


def _require_historical_energy(result: EnergyResult, label: str) -> None:
    if result.kind not in {DataKind.METERED, DataKind.CALCULATED}:
        raise EnergyError(
            "unsupported_kind", f"{label} must be metered or calculated historical data."
        )


def _energy_unit(result: EnergyResult, label: str) -> Any:
    unit = _unit_info(result.unit)
    if unit.dimension != "energy":
        raise EnergyError("unit_mismatch", f"{label} must use Wh, kWh or MWh interval energy.")
    return unit


def _finite_nonnegative_values(values: tuple[float | None, ...], label: str) -> list[float]:
    output: list[float] = []
    for value in values:
        number = numeric_value(value, allow_missing=False)
        assert number is not None
        if number < 0:
            raise EnergyError("invalid_value", f"{label} energy values must be nonnegative.")
        output.append(number)
    return output


def _convert_values(values: list[float], factor: float, label: str) -> list[float]:
    converted = [value * factor for value in values]
    if any(not isfinite(value) for value in converted):
        raise EnergyError("invalid_value", f"Converted {label} values must be finite.")
    return converted


def _require_consistent_interval_duration(
    starts: tuple[pd.Timestamp, ...], ends: tuple[pd.Timestamp, ...], label: str
) -> None:
    durations = {end - start for start, end in zip(starts, ends, strict=True)}
    if len(durations) > 1:
        raise EnergyError("mixed_resolution", f"{label} intervals must have matching durations.")


def _rolling_prior_mean(values: list[float], window: int) -> list[float | None]:
    means = pd.Series(values, dtype="float64").rolling(window, min_periods=window).mean().shift(1)
    return [None if pd.isna(value) else float(value) for value in means]


def _load_intervals(
    load: Any,
    ends: tuple[pd.Timestamp, ...],
    values: list[float],
    baseline: list[float | None],
    spike_ratio: float,
    min_excess_kwh: float,
) -> list[Json]:
    output: list[Json] = []
    for index, (instant, end, value, base) in enumerate(
        zip(load.times, ends, values, baseline, strict=True)
    ):
        residual = None if base is None else value - base
        raw_ratio = None if base is None or base == 0 else value / base
        ratio = raw_ratio if raw_ratio is not None and isfinite(raw_ratio) else None
        is_spike = False
        if residual is not None and residual > 0 and residual >= min_excess_kwh:
            is_spike = base == 0 or (raw_ratio is not None and raw_ratio >= spike_ratio)
        output.append(
            {
                "timestamp": _timestamp_text(instant, load.result.timezone),
                "end": _timestamp_text(end, load.result.timezone),
                "load_kwh": value,
                "baseline_kwh": base,
                "residual_kwh": residual,
                "excess_kwh": max(residual, 0.0) if residual is not None else None,
                "load_to_baseline_ratio": ratio,
                "is_spike": is_spike,
                "_index": index,
            }
        )
    return output


def _prepare_equipment(
    item: tuple[str, EnergyResult],
    parameters: Json,
    load: Any,
    load_ends: tuple[pd.Timestamp, ...],
    end_column: str,
    load_kwh: list[float],
    window: int,
) -> dict[str, Any]:
    artifact_id, result = item
    _require_historical_energy(result, "Equipment")
    if result.quantity_shape != "interval":
        raise EnergyError(
            "quantity_shape_mismatch", "Equipment must be explicitly declared as interval energy."
        )
    if result.site_id is not None and load.result.site_id is not None:
        if result.site_id != load.result.site_id:
            raise EnergyError("incompatible_equipment", "Equipment and consumption sites differ.")
    column = _name(parameters, "equipment_column", "value")
    timestamp = _name(parameters, "equipment_timestamp", "timestamp")
    series = _series(artifact_id, result, {"column": column, "timestamp": timestamp})
    unit = _energy_unit(result, "Equipment")
    ends = _optional_ends(series, end_column)
    if ends is None:
        raise EnergyError(
            "missing_interval_end", "Every equipment interval must provide an explicit end."
        )
    if series.times != load.times or ends != load_ends:
        raise EnergyError(
            "interval_mismatch",
            "Equipment must match the exact consumption starts, ends and horizon.",
        )
    _require_consistent_interval_duration(series.times, ends, "Equipment")
    values = _convert_values(
        _finite_nonnegative_values(series.values, "Equipment"), unit.to_base, "Equipment"
    )
    if any(
        equipment_value > load_value + 1e-9
        for equipment_value, load_value in zip(values, load_kwh, strict=True)
    ):
        raise EnergyError(
            "incompatible_equipment",
            "An equipment interval exceeds total consumption for the same interval.",
        )
    return {
        "artifact_id": artifact_id,
        "result": result,
        "values_kwh": values,
        "baseline": _rolling_prior_mean(values, window),
    }


def _prepare_weather(
    item: tuple[str, EnergyResult], parameters: Json, load: Any
) -> tuple[dict[pd.Timestamp, float | None], str | None]:
    artifact_id, result = item
    if result.unit.strip().casefold() not in {
        "°c",
        "degc",
        "degree_celsius",
        "degrees_celsius",
        "celsius",
    }:
        raise EnergyError("unit_mismatch", "Weather must use degrees Celsius (°C).")
    column = _name(parameters, "weather_column", "value")
    timestamp = _name(parameters, "weather_timestamp", "timestamp")
    series = _series(artifact_id, result, {"column": column, "timestamp": timestamp})
    load_instants = set(load.times)
    values = {
        instant: numeric_value(value)
        for instant, value in zip(series.times, series.values, strict=True)
        if instant in load_instants
    }
    if result.kind == DataKind.FORECAST:
        return {}, "Weather input is a forecast; it is not used as observed explanation evidence."
    if result.kind not in {DataKind.METERED, DataKind.CALCULATED}:
        return {}, "Weather input is not metered or calculated historical data."
    return values, None


def _mean(values: list[float]) -> float:
    return sum(value / len(values) for value in values)


def cast_float(value: float | None) -> float:
    assert value is not None
    return value


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_scale = max(abs(value) for value in left_centered)
    right_scale = max(abs(value) for value in right_centered)
    if left_scale == 0 or right_scale == 0:
        return None
    normalized_left = [value / left_scale for value in left_centered]
    normalized_right = [value / right_scale for value in right_centered]
    cross = sum(a * b for a, b in zip(normalized_left, normalized_right, strict=True))
    left_square = sum(value * value for value in normalized_left)
    right_square = sum(value * value for value in normalized_right)
    correlation = cross / sqrt(left_square * right_square)
    return max(-1.0, min(1.0, correlation))
