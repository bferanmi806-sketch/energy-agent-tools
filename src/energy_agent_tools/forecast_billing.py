"""Forecast-based energy cost and bill estimates over explicit tariff intervals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from numbers import Real
from typing import Any

import pandas as pd

from .billing import calculate_bill
from .models import DataKind, EnergyError, EnergyResult, Json
from .timeseries import MAX_ROWS, _unit_info

MAX_OUTPUT_ROWS = 100_000
_PARAMETERS = {"tariff_timestamp", "tariff_end", "tariff_column", "billing"}
_CURRENCIES = {"GBP", "USD", "EUR"}
_DECIMAL_DIGITS_LIMIT = 10_000
_PRECISION_LIMIT = 50_000
_EXTRA_PRECISION = 100


@dataclass(frozen=True)
class _ForecastInterval:
    start: pd.Timestamp
    end: pd.Timestamp
    value: Decimal
    lower: Decimal
    upper: Decimal


@dataclass(frozen=True)
class _TariffInterval:
    start: pd.Timestamp
    end: pd.Timestamp
    raw_rate: Decimal
    rate: Decimal


def estimate(
    forecast: tuple[str, EnergyResult],
    tariff: tuple[str, EnergyResult],
    parameters: Json,
) -> EnergyResult:
    """Estimate forecast energy costs against caller-supplied tariff validity intervals.

    Energy is allocated uniformly through each forecast interval when a tariff
    changes inside it. The result contains independent lower and upper cost
    scenarios; those scenarios do not imply aggregate confidence coverage.
    """

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Forecast billing parameters must be an object.")
    unknown = set(parameters) - _PARAMETERS
    if unknown:
        raise EnergyError("invalid_parameters", "Forecast billing has unsupported parameters.")
    forecast_id, forecast_result = _input(forecast, "forecast")
    tariff_id, tariff_result = _input(tariff, "tariff")

    if forecast_result.kind != DataKind.FORECAST:
        raise EnergyError("invalid_input", "Forecast billing requires a forecast input.")
    if forecast_result.quantity_shape != "interval":
        raise EnergyError("quantity_shape_mismatch", "Forecast billing requires interval energy.")
    forecast_unit = _unit_info(forecast_result.unit)
    if forecast_unit.dimension != "energy":
        raise EnergyError("unit_mismatch", "Forecast billing requires energy values, not power.")

    forecast_intervals, forecast_metadata = _forecast_intervals(
        forecast_result, _decimal_factor(forecast_unit.to_base)
    )
    first = forecast_intervals[0].start
    finish = forecast_intervals[-1].end

    tariff_timestamp = _parameter_name(parameters, "tariff_timestamp", "from")
    tariff_end = _parameter_name(parameters, "tariff_end", "to")
    tariff_column = _parameter_name(parameters, "tariff_column", "value")
    tariff_unit = _unit_info(tariff_result.unit)
    if tariff_unit.dimension != "rate" or tariff_unit.currency not in _CURRENCIES:
        raise EnergyError(
            "unit_mismatch", "Tariff units must be a supported currency per energy unit."
        )
    currency = tariff_unit.currency
    rate_factor = _decimal_factor(tariff_unit.to_base)
    tariff_intervals = _tariff_intervals(
        tariff_result, tariff_timestamp, tariff_end, tariff_column, rate_factor
    )

    precision = _working_precision(
        [
            *(interval.value for interval in forecast_intervals),
            *(interval.lower for interval in forecast_intervals),
            *(interval.upper for interval in forecast_intervals),
            *(interval.raw_rate for interval in tariff_intervals),
            *(interval.rate for interval in tariff_intervals),
        ],
        max(len(forecast_intervals), len(tariff_intervals)),
    )
    point_rows: list[Json] = []
    lower_rows: list[Json] = []
    upper_rows: list[Json] = []
    interval_output: list[Json] = []
    segment_count = 0
    tariff_index = 0

    try:
        with localcontext() as context:
            context.prec = precision
            for interval in forecast_intervals:
                duration_ns = interval.end.value - interval.start.value
                cursor = interval.start
                while (
                    tariff_index < len(tariff_intervals)
                    and tariff_intervals[tariff_index].end <= cursor
                ):
                    tariff_index += 1

                tariff_cursor = tariff_index
                coefficient = Decimal(0)
                segments: list[Json] = []
                while cursor < interval.end:
                    if tariff_cursor >= len(tariff_intervals):
                        raise EnergyError(
                            "missing_rate_coverage", "Tariff intervals do not cover the forecast."
                        )
                    tariff_interval = tariff_intervals[tariff_cursor]
                    if tariff_interval.start > cursor:
                        raise EnergyError(
                            "missing_rate_coverage", "Tariff intervals do not cover the forecast."
                        )
                    if tariff_interval.end <= cursor:
                        tariff_cursor += 1
                        continue

                    segment_end = min(interval.end, tariff_interval.end)
                    segment_ns = segment_end.value - cursor.value
                    fraction = Decimal(segment_ns) / Decimal(duration_ns)
                    segment_energy = interval.value * fraction
                    segment_cost = segment_energy * tariff_interval.rate
                    coefficient += fraction * tariff_interval.rate
                    segments.append(
                        {
                            "timestamp": _timestamp_text(cursor, forecast_result.timezone),
                            "end": _timestamp_text(segment_end, forecast_result.timezone),
                            "forecast_kwh": segment_energy,
                            "rate": tariff_interval.rate,
                            "tariff_rate": tariff_interval.raw_rate,
                            "tariff_unit": tariff_result.unit,
                            "energy_cost": segment_cost,
                        }
                    )
                    segment_count += 1
                    if segment_count > MAX_OUTPUT_ROWS:
                        raise EnergyError(
                            "output_too_large",
                            "Tariff and forecast splits exceed the output limit.",
                        )
                    cursor = segment_end
                    if cursor == tariff_interval.end:
                        tariff_cursor += 1

                tariff_index = tariff_cursor
                point_cost = interval.value * coefficient
                lower_cost, upper_cost = _scenario_costs(
                    interval.lower, interval.upper, coefficient
                )
                timestamp = _timestamp_text(interval.start, forecast_result.timezone)
                end = _timestamp_text(interval.end, forecast_result.timezone)
                point_rows.append({"timestamp": timestamp, "end": end, "cost": point_cost})
                lower_rows.append({"timestamp": timestamp, "end": end, "cost": lower_cost})
                upper_rows.append({"timestamp": timestamp, "end": end, "cost": upper_cost})
                interval_output.append(
                    {
                        "timestamp": timestamp,
                        "end": end,
                        "forecast_kwh": interval.value,
                        "lower_kwh": interval.lower,
                        "upper_kwh": interval.upper,
                        "rate": coefficient,
                        "rate_unit": f"{currency}/kWh",
                        "energy_cost": point_cost,
                        "lower_energy_cost": lower_cost,
                        "upper_energy_cost": upper_cost,
                        "segments": segments,
                    }
                )
    except (ArithmeticError, ValueError) as exc:
        raise EnergyError(
            "precision_limit", "Forecast billing arithmetic exceeded its limit."
        ) from exc

    if len(interval_output) > MAX_OUTPUT_ROWS:
        raise EnergyError("output_too_large", "Forecast intervals exceed the output limit.")

    point_costs = _cost_result(
        point_rows, currency, forecast_result, forecast_id, tariff_result, tariff_id
    )
    lower_costs = _cost_result(
        lower_rows, currency, forecast_result, forecast_id, tariff_result, tariff_id
    )
    upper_costs = _cost_result(
        upper_rows, currency, forecast_result, forecast_id, tariff_result, tariff_id
    )

    billing = parameters.get("billing")
    assumptions = [
        "Calculated from forecast consumption; not a measured bill.",
        "Energy is distributed uniformly within each forecast interval when tariff rates change.",
        "Tariff rates are treated as fixed over their supplied validity intervals.",
        "Lower and upper values are per-interval cost scenarios, not aggregate confidence guarantees.",
    ]
    warnings = list(dict.fromkeys([*forecast_result.warnings, *tariff_result.warnings]))
    if billing is None:
        estimate_data: Json = {
            "energy_cost": _sum_rows(point_rows),
            "currency": currency,
            "complete_bill": False,
            "missing_components": ["standing_charge", "tax"],
        }
        lower_data: Json = {"energy_cost": _sum_rows(lower_rows), "currency": currency}
        upper_data: Json = {"energy_cost": _sum_rows(upper_rows), "currency": currency}
        warnings.append(
            "Standing charge and tax were not supplied; this is an energy-cost estimate."
        )
    else:
        bill_parameters = _billing_parameters(
            billing,
            first=first,
            finish=finish,
            timezone=forecast_result.timezone,
        )
        point_bill = calculate_bill([("forecast-cost", point_costs)], bill_parameters)
        lower_bill = calculate_bill([("forecast-cost-lower", lower_costs)], bill_parameters)
        upper_bill = calculate_bill([("forecast-cost-upper", upper_costs)], bill_parameters)
        estimate_data = {**point_bill.data, "complete_bill": True}
        lower_data = lower_bill.data
        upper_data = upper_bill.data
        assumptions.extend(point_bill.assumptions)

    provenance = [
        {
            "operation": "forecast_bill_estimate",
            "version": 1,
            "inputs": [
                _input_lineage(forecast_id, forecast_result, "consumption_forecast"),
                _input_lineage(tariff_id, tariff_result, "tariff_schedule"),
            ],
            "forecast_metadata": forecast_metadata,
            "tariff_units": {"input": tariff_result.unit, "normalized": f"{currency}/kWh"},
            "tariff_source": tariff_result.source,
            "billing": billing,
            "assumptions": assumptions,
        }
    ]
    data = {
        "calculation_basis": "forecast_consumption",
        "estimate": estimate_data,
        "uncertainty": {
            "lower": lower_data,
            "upper": upper_data,
            "interpretation": (
                "Each forecast interval uses the energy bound that minimizes or maximizes its "
                "cost; these scenarios are not joint coverage guarantees."
            ),
        },
        "intervals": interval_output,
    }
    field_units = {
        "forecast_kwh": "kWh",
        "lower_kwh": "kWh",
        "upper_kwh": "kWh",
        "rate": f"{currency}/kWh",
        "energy_cost": currency,
        "lower_energy_cost": currency,
        "upper_energy_cost": currency,
    }
    if billing is not None:
        field_units.update(
            {
                "standing_charge": currency,
                "taxable_subtotal": currency,
                "tax": currency,
                "total": currency,
            }
        )
    return EnergyResult(
        data=data,
        kind=DataKind.CALCULATED,
        unit=currency,
        source="workbench",
        timezone=forecast_result.timezone,
        resolution=forecast_result.resolution,
        provider=forecast_result.provider,
        site_id=forecast_result.site_id,
        asset_id=forecast_result.asset_id,
        time_start=first.to_pydatetime(warn=False),
        time_end=finish.to_pydatetime(warn=False),
        original_unit=currency,
        field_units=field_units,
        assumptions=list(dict.fromkeys(assumptions)),
        warnings=list(dict.fromkeys(warnings)),
        quality="derived",
        quantity_shape="interval",
        provenance=provenance,
    )


def _input(value: tuple[str, EnergyResult], name: str) -> tuple[str, EnergyResult]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0].strip()
        or not isinstance(value[1], EnergyResult)
    ):
        raise EnergyError(
            "invalid_input", f"The {name} input needs an artifact ID and energy result."
        )
    return value


def _forecast_intervals(
    result: EnergyResult, to_kwh: Decimal
) -> tuple[list[_ForecastInterval], Json]:
    if not isinstance(result.data, dict) or "intervals" not in result.data:
        raise EnergyError("not_tabular", "Forecast data must contain an intervals array.")
    if "summary" not in result.data or "model" not in result.data:
        raise EnergyError(
            "invalid_forecast", "Forecast data must include summary and model metadata."
        )
    rows = result.data["intervals"]
    if not isinstance(rows, list) or not rows:
        raise EnergyError("not_tabular", "Forecast intervals must be a nonempty array.")
    if len(rows) > MAX_ROWS:
        raise EnergyError("input_too_large", "Forecast contains too many intervals.")
    required = {"timestamp", "end", "value", "lower", "upper"}
    parsed: list[_ForecastInterval] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EnergyError("not_tabular", "Every forecast interval must be an object.")
        if not required <= row.keys():
            raise EnergyError(
                "column_not_found", "Forecast intervals need timestamp, end and bounds."
            )
        start = _timestamp(row["timestamp"], "forecast timestamp")
        end = _timestamp(row["end"], "forecast interval end")
        if end <= start:
            raise EnergyError(
                "invalid_interval", "Every forecast interval end must follow its start."
            )
        value = _multiply_exact(_decimal(row["value"], "forecast value"), to_kwh)
        lower = _multiply_exact(_decimal(row["lower"], "forecast lower bound"), to_kwh)
        upper = _multiply_exact(_decimal(row["upper"], "forecast upper bound"), to_kwh)
        if lower < 0 or not lower <= value <= upper:
            raise EnergyError(
                "invalid_forecast", "Forecast bounds must satisfy 0 <= lower <= value <= upper."
            )
        parsed.append(_ForecastInterval(start, end, value, lower, upper))
    parsed.sort(key=lambda interval: interval.start)
    cursor = parsed[0].start
    for interval in parsed:
        if interval.start < cursor:
            raise EnergyError(
                "overlapping_intervals", "Forecast intervals must not overlap or duplicate."
            )
        if interval.start > cursor:
            raise EnergyError("coverage_gap", "Forecast intervals must be contiguous without gaps.")
        cursor = interval.end
    metadata = {key: value for key, value in result.data.items() if key != "intervals"}
    return parsed, metadata


def _tariff_intervals(
    result: EnergyResult,
    timestamp_name: str,
    end_name: str,
    column_name: str,
    rate_factor: Decimal,
) -> list[_TariffInterval]:
    if not isinstance(result.data, list) or not result.data:
        raise EnergyError("not_tabular", "Tariff input must contain nonempty interval rows.")
    if len(result.data) > MAX_ROWS:
        raise EnergyError("input_too_large", "Tariff contains too many intervals.")
    required = {timestamp_name, end_name, column_name}
    parsed: list[_TariffInterval] = []
    for row in result.data:
        if not isinstance(row, dict):
            raise EnergyError("not_tabular", "Every tariff interval must be an object.")
        if not required <= row.keys():
            raise EnergyError(
                "column_not_found", "Tariff rows need explicit start, end and price columns."
            )
        if row[end_name] is None:
            raise EnergyError(
                "open_ended_tariff", "Every tariff interval needs an explicit end timestamp."
            )
        start = _timestamp(row[timestamp_name], "tariff timestamp")
        end = _timestamp(row[end_name], "tariff interval end")
        if end <= start:
            raise EnergyError(
                "invalid_interval", "Every tariff interval end must follow its start."
            )
        raw_rate = _decimal(row[column_name], "tariff price")
        parsed.append(_TariffInterval(start, end, raw_rate, _multiply_exact(raw_rate, rate_factor)))
    parsed.sort(key=lambda interval: interval.start)
    previous_end: pd.Timestamp | None = None
    for interval in parsed:
        if previous_end is not None and interval.start < previous_end:
            raise EnergyError(
                "overlapping_tariff", "Tariff intervals must not overlap or duplicate."
            )
        previous_end = interval.end
    return parsed


def _timestamp(value: Any, description: str) -> pd.Timestamp:
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        raise EnergyError("invalid_timestamp", f"{description} must be an aware timestamp.")
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError("invalid_timestamp", f"{description} is not a valid timestamp.") from exc
    if pd.isna(parsed):
        raise EnergyError("invalid_timestamp", f"{description} must not be null or NaT.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", f"{description} must include an explicit UTC offset.")
    return parsed.tz_convert("UTC")


def _decimal(value: Any, description: str) -> Decimal:
    if value is None:
        raise EnergyError("missing_value", f"{description} is missing.")
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal, str)):
        raise EnergyError("invalid_value", f"{description} must be a finite numeric value.")
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise EnergyError(
            "invalid_value", f"{description} must be a finite numeric value."
        ) from exc
    if not parsed.is_finite():
        raise EnergyError("invalid_value", f"{description} must be finite.")
    exponent = parsed.as_tuple().exponent
    assert isinstance(exponent, int)  # Finite Decimal exponents are integers.
    if (
        len(parsed.as_tuple().digits) > _DECIMAL_DIGITS_LIMIT
        or abs(exponent) > _DECIMAL_DIGITS_LIMIT
    ):
        raise EnergyError("invalid_value", f"{description} exceeds the supported decimal range.")
    return parsed


def _decimal_factor(value: float) -> Decimal:
    return Decimal(str(value))


def _multiply_exact(left: Decimal, right: Decimal) -> Decimal:
    precision = len(left.as_tuple().digits) + len(right.as_tuple().digits)
    with localcontext() as context:
        context.prec = max(1, precision)
        return left * right


def _working_precision(values: list[Decimal], count: int) -> int:
    nonzero = [value for value in values if value]
    if not nonzero:
        return 128
    lowest_exponent = min(int(value.as_tuple().exponent) for value in nonzero)
    highest_adjusted = max(value.adjusted() for value in nonzero)
    count_digits = len(str(max(count, 1)))
    precision = highest_adjusted - lowest_exponent + count_digits + _EXTRA_PRECISION
    if precision > _PRECISION_LIMIT:
        raise EnergyError(
            "precision_limit", "Forecast billing exceeds supported decimal precision."
        )
    return max(128, precision)


def _scenario_costs(
    lower: Decimal, upper: Decimal, coefficient: Decimal
) -> tuple[Decimal, Decimal]:
    if coefficient < 0:
        return upper * coefficient, lower * coefficient
    return lower * coefficient, upper * coefficient


def _sum_rows(rows: list[Json]) -> Decimal:
    costs = [row["cost"] for row in rows]
    with localcontext() as context:
        context.prec = _working_precision(costs, len(costs))
        return sum(costs, Decimal(0))


def _cost_result(
    rows: list[Json],
    currency: str,
    forecast: EnergyResult,
    forecast_id: str,
    tariff: EnergyResult,
    tariff_id: str,
) -> EnergyResult:
    return EnergyResult(
        data=rows,
        kind=DataKind.CALCULATED,
        unit=currency,
        source="workbench",
        timezone=forecast.timezone,
        resolution=forecast.resolution,
        site_id=forecast.site_id,
        provider=forecast.provider,
        asset_id=forecast.asset_id,
        quantity_shape="interval",
        provenance=[
            {
                "operation": "cost",
                "version": 1,
                "inputs": [
                    {"artifact_id": forecast_id, "kind": forecast.kind.value},
                    {"artifact_id": tariff_id, "kind": tariff.kind.value},
                ],
            }
        ],
    )


def _billing_parameters(
    billing: Any,
    *,
    first: pd.Timestamp,
    finish: pd.Timestamp,
    timezone: str,
) -> Json:
    if not isinstance(billing, dict) or set(billing) != {"standing_charge", "tax", "source"}:
        raise EnergyError(
            "invalid_parameters", "billing must include standing_charge, tax and source."
        )
    return {
        "start": first.tz_convert(timezone).isoformat(),
        "finish": finish.tz_convert(timezone).isoformat(),
        "timezone": timezone,
        "standing_charge": billing["standing_charge"],
        "tax": billing["tax"],
        "source": billing["source"],
    }


def _parameter_name(parameters: Json, key: str, default: str) -> str:
    value = parameters.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise EnergyError("invalid_parameters", f"{key} must be a nonempty string.")
    return value


def _timestamp_text(value: pd.Timestamp, timezone: str) -> str:
    return value.tz_convert(timezone).isoformat()


def _input_lineage(artifact_id: str, result: EnergyResult, role: str) -> Json:
    lineage: Json = {
        "role": role,
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
    return lineage
