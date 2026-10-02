"""Caller-defined tariff billing over calculated interval-cost rows."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, Inexact, Rounded, localcontext
from numbers import Real
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from .models import DataKind, EnergyError, EnergyResult, Json

MAX_ROWS = 100_000
_CURRENCIES = {"GBP", "USD", "EUR"}
_PARAMETERS = {
    "column",
    "timestamp",
    "end",
    "start",
    "finish",
    "timezone",
    "standing_charge",
    "tax",
    "source",
}


def calculate_bill(
    inputs: list[tuple[str, EnergyResult]],
    parameters: Json,
) -> EnergyResult:
    """Calculate one bill from a fully covered, calculated cost time series.

    The billing window is ``[start, finish)`` in the requested timezone. Both
    bounds must be local midnight, so daily standing charges apply to whole
    local calendar days, including daylight-saving days of 23 or 25 hours.
    """

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Billing parameters must be an object.")
    unknown = set(parameters) - _PARAMETERS
    if unknown:
        raise EnergyError("invalid_parameters", "Billing parameters contain unsupported fields.")
    required = {"start", "finish", "timezone", "standing_charge", "tax", "source"}
    if not required <= parameters.keys():
        raise EnergyError(
            "invalid_parameters",
            "Start, finish, timezone, standing_charge, tax and source are required.",
        )
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise EnergyError("invalid_input", "Billing requires exactly one input artifact.")
    artifact_id, result = inputs[0]
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise EnergyError("invalid_input", "The input must include an artifact identifier.")
    if not isinstance(result, EnergyResult):
        raise EnergyError("invalid_input", "The input must be an energy result.")
    if result.kind != DataKind.CALCULATED:
        raise EnergyError("invalid_input", "Billing accepts calculated interval-cost rows only.")
    if result.quantity_shape not in (None, "interval"):
        raise EnergyError("invalid_input", "Billing requires interval cost data.")
    if not _contains_cost_operation(result.provenance):
        raise EnergyError(
            "invalid_input", "Input provenance must include the timeseries cost operation."
        )
    if result.unit not in _CURRENCIES:
        raise EnergyError("invalid_currency", "Cost rows must use GBP, USD or EUR.")

    column = _parameter_name(parameters, "column", "cost")
    timestamp_name = _parameter_name(parameters, "timestamp", "timestamp")
    end_name = _parameter_name(parameters, "end", "end")
    timezone_name = parameters["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name:
        raise EnergyError("invalid_parameters", "timezone must be a valid IANA timezone name.")
    try:
        billing_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EnergyError(
            "invalid_parameters", "timezone must be a valid IANA timezone name."
        ) from exc

    start = _bound(parameters["start"], "start", billing_zone)
    finish = _bound(parameters["finish"], "finish", billing_zone)
    if finish <= start:
        raise EnergyError("invalid_range", "finish must be after start.")
    chargeable_days = (
        finish.tz_convert(billing_zone).date() - start.tz_convert(billing_zone).date()
    ).days
    if chargeable_days <= 0:
        raise EnergyError(
            "invalid_range", "The billing window must contain at least one full local calendar day."
        )

    standing_amount, standing_currency, standing_taxable = _standing_charge(
        parameters["standing_charge"]
    )
    if standing_currency != result.unit:
        raise EnergyError(
            "currency_mismatch", "Standing charge and interval costs must use the same currency."
        )
    tax_rate, energy_taxable = _tax(parameters["tax"])
    tariff_source = parameters["source"]
    if not isinstance(tariff_source, str) or not tariff_source.strip():
        raise EnergyError(
            "invalid_parameters", "source must identify the caller-supplied tariff schedule."
        )

    rows = _table(result)
    if any(name not in row for row in rows for name in (timestamp_name, end_name, column)):
        raise EnergyError("column_not_found", "Cost, timestamp or interval-end column is missing.")

    intervals: list[tuple[pd.Timestamp, pd.Timestamp, Decimal]] = []
    for row in rows:
        interval_start = _timestamp(row[timestamp_name], "interval timestamp")
        interval_end = _timestamp(row[end_name], "interval end")
        if interval_end <= interval_start:
            raise EnergyError("invalid_interval", "Every interval end must be after its start.")
        cost = _decimal(row[column], "interval cost")
        intervals.append((interval_start, interval_end, cost))
    intervals.sort(key=lambda item: item[0])

    cursor = start
    for interval_start, interval_end, _ in intervals:
        if interval_start < start or interval_end > finish:
            raise EnergyError(
                "outside_window", "Cost intervals must stay inside the complete billing window."
            )
        if interval_start < cursor:
            raise EnergyError(
                "overlapping_intervals", "Cost intervals must not overlap or duplicate."
            )
        if interval_start > cursor:
            raise EnergyError(
                "coverage_gap", "Cost intervals must cover the billing window without gaps."
            )
        cursor = interval_end
    if cursor != finish:
        raise EnergyError("insufficient_coverage", "Cost intervals must end exactly at finish.")

    costs = [item[2] for item in intervals]
    precision = _calculation_precision(costs, standing_amount, tax_rate, chargeable_days)
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            energy_cost = sum(costs, Decimal(0))
            standing_charge = standing_amount * chargeable_days
            taxable_subtotal = (energy_cost if energy_taxable else Decimal(0)) + (
                standing_charge if standing_taxable else Decimal(0)
            )
            tax = taxable_subtotal * tax_rate
            total = energy_cost + standing_charge + tax
    except (Inexact, Rounded) as exc:
        raise EnergyError(
            "precision_limit", "Billing arithmetic exceeded exact decimal precision."
        ) from exc

    assumptions = [
        "Tariff, standing charge and tax treatment were supplied by the caller.",
        "No statutory tariff or tax rate was inferred.",
    ]
    window = {
        "start": start.tz_convert(billing_zone).isoformat(),
        "finish": finish.tz_convert(billing_zone).isoformat(),
        "timezone": timezone_name,
    }
    source_record = {
        "operation": "calculate_bill",
        "version": 1,
        "inputs": [
            {
                "artifact_id": artifact_id,
                "source_kind": result.kind.value,
                "source": result.source,
                "unit": result.unit,
                "timezone": result.timezone,
                "resolution": result.resolution,
                "provider": result.provider,
                "site_id": result.site_id,
                "asset_id": result.asset_id,
                "provenance": result.provenance,
            }
        ],
        "tariff_schedule": {
            "source": tariff_source.strip(),
            "currency": result.unit,
            "standing_charge": {
                "amount_per_day": str(standing_amount),
                "taxable": standing_taxable,
            },
            "tax": {"rate": str(tax_rate), "energy_taxable": energy_taxable},
        },
        "components": {
            "energy_cost": "sum of interval costs within the complete billing window",
            "standing_charge": "amount_per_day multiplied by whole local calendar days",
            "taxable_subtotal": "caller-selected taxable energy cost and standing charge",
            "tax": "taxable_subtotal multiplied by the caller-supplied rate",
            "total": "energy_cost plus standing_charge plus tax",
        },
    }
    return EnergyResult(
        data={
            "energy_cost": energy_cost,
            "standing_charge": standing_charge,
            "chargeable_days": chargeable_days,
            "taxable_subtotal": taxable_subtotal,
            "tax": tax,
            "total": total,
            "currency": result.unit,
            "window": window,
        },
        kind=DataKind.CALCULATED,
        unit=result.unit,
        source="workbench",
        timezone=result.timezone,
        provider=result.provider,
        site_id=result.site_id,
        asset_id=result.asset_id,
        time_start=start.to_pydatetime(warn=False),
        time_end=finish.to_pydatetime(warn=False),
        original_unit=result.original_unit or result.unit,
        field_units={
            "energy_cost": result.unit,
            "standing_charge": result.unit,
            "taxable_subtotal": result.unit,
            "tax": result.unit,
            "total": result.unit,
        },
        assumptions=assumptions,
        warnings=list(result.warnings),
        quality="derived",
        provenance=[source_record],
    )


def _parameter_name(parameters: Json, key: str, default: str) -> str:
    value = parameters.get(key, default)
    if not isinstance(value, str) or not value:
        raise EnergyError("invalid_parameters", f"{key} must be a non-empty string.")
    return value


def _bound(value: Any, name: str, timezone: ZoneInfo) -> pd.Timestamp:
    if not isinstance(value, str):
        raise EnergyError("invalid_timestamp", f"{name} must be an aware ISO-8601 string.")
    parsed = _timestamp(value, name)
    local = parsed.tz_convert(timezone)
    if any((local.hour, local.minute, local.second, local.microsecond, local.nanosecond)):
        raise EnergyError("partial_day", f"{name} must be local midnight in the billing timezone.")
    return parsed


def _timestamp(value: Any, description: str) -> pd.Timestamp:
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        raise EnergyError(
            "invalid_timestamp", f"{description} must be an aware ISO-8601 timestamp."
        )
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnergyError(
            "invalid_timestamp", f"{description} is not a valid ISO-8601 timestamp."
        ) from exc
    if pd.isna(parsed):
        raise EnergyError("invalid_timestamp", f"{description} must not be null or NaT.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnergyError("naive_timestamp", f"{description} must include an explicit UTC offset.")
    return parsed.tz_convert("UTC")


def _table(result: EnergyResult) -> list[dict[str, Any]]:
    if not isinstance(result.data, list) or not result.data:
        raise EnergyError("not_tabular", "Input must contain nonempty interval rows.")
    if len(result.data) > MAX_ROWS:
        raise EnergyError("input_too_large", "Input contains too many interval rows.")
    if any(not isinstance(row, dict) for row in result.data):
        raise EnergyError("not_tabular", "Every interval row must be an object.")
    return result.data


def _decimal(value: Any, description: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise EnergyError(
            "invalid_value", f"{description} must be a finite number, not a boolean or string."
        )
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise EnergyError("invalid_value", f"{description} must be a finite number.") from exc
    if not parsed.is_finite():
        raise EnergyError("invalid_value", f"{description} must be a finite number.")
    if len(parsed.as_tuple().digits) > 10_000 or abs(parsed.as_tuple().exponent) > 10_000:
        raise EnergyError("invalid_value", f"{description} exceeds the supported decimal range.")
    return parsed


def _standing_charge(value: Any) -> tuple[Decimal, str, bool]:
    if not isinstance(value, dict) or set(value) != {"amount_per_day", "currency", "taxable"}:
        raise EnergyError(
            "invalid_parameters",
            "standing_charge must contain exactly amount_per_day, currency and taxable.",
        )
    amount = _decimal(value["amount_per_day"], "standing_charge.amount_per_day")
    if amount < 0:
        raise EnergyError("invalid_value", "standing_charge.amount_per_day must be non-negative.")
    currency = value["currency"]
    if not isinstance(currency, str) or currency not in _CURRENCIES:
        raise EnergyError("invalid_currency", "standing_charge.currency must be GBP, USD or EUR.")
    if not isinstance(value["taxable"], bool):
        raise EnergyError("invalid_parameters", "standing_charge.taxable must be a boolean.")
    return amount, currency, value["taxable"]


def _tax(value: Any) -> tuple[Decimal, bool]:
    if not isinstance(value, dict) or set(value) != {"rate", "energy_taxable"}:
        raise EnergyError("invalid_parameters", "tax must contain exactly rate and energy_taxable.")
    rate = _decimal(value["rate"], "tax.rate")
    if not Decimal(0) <= rate <= Decimal(1):
        raise EnergyError("invalid_value", "tax.rate must be between 0 and 1 inclusive.")
    if not isinstance(value["energy_taxable"], bool):
        raise EnergyError("invalid_parameters", "tax.energy_taxable must be a boolean.")
    return rate, value["energy_taxable"]


def _calculation_precision(
    costs: list[Decimal], standing_amount: Decimal, tax_rate: Decimal, chargeable_days: int
) -> int:
    values = [*costs, standing_amount, tax_rate]
    nonzero = [value for value in values if value]
    if not nonzero:
        return 64
    lowest_exponent = min(value.as_tuple().exponent for value in nonzero)
    highest_adjusted = max(value.adjusted() for value in nonzero)
    count_digits = len(str(max(1, len(costs), chargeable_days)))
    rate_digits = len(tax_rate.as_tuple().digits)
    precision = highest_adjusted - lowest_exponent + count_digits + rate_digits + 32
    if precision > 50_000:
        raise EnergyError(
            "precision_limit", "Billing inputs exceed the supported exact decimal precision."
        )
    return max(64, precision)


def _contains_cost_operation(provenance: list[Json]) -> bool:
    def contains(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("operation") == "cost":
                return True
            return any(contains(nested) for nested in value.values())
        if isinstance(value, list):
            return any(contains(nested) for nested in value)
        return False

    return contains(provenance)
