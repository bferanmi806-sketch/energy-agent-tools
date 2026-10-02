"""Aggregate observed interval energy into coarser, start-anchored bins."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal
from math import isfinite
from typing import Any

from .models import EnergyError
from .timeseries import numeric_value
from .windows import bounds, instant

_ALLOWED_INTERVAL_MINUTES = {15, 30, 60}
_MAX_OUTPUT_BINS = 100_000
_MAX_NUMERIC_TEXT_LENGTH = 1_024
_MAX_NUMERIC_DIGITS = 1_000
_MAX_ALIGNMENT_EXPONENT_GAP = 2_000
# Caps each per-bin coefficient at about 4,000 decimal digits.
_MAX_ACCUMULATOR_BITS = 13_287
_UNIT_EXPONENT_TO_KWH = {"wh": -3, "kwh": 0, "mwh": 3}


def aggregate_interval_energy(
    rows: Iterable[dict[str, Any]],
    *,
    start: str,
    end: str,
    timestamp: str = "timestamp",
    end_column: str = "end",
    value: str = "value",
    unit: str,
    interval_minutes: int = 30,
) -> list[dict[str, Any]]:
    """Sum contiguous observed energy intervals into fixed-width kWh bins.

    Input rows must cover the requested half-open window exactly. Each observed
    interval must fit inside one output bin, so this operation never prorates a
    measurement across a target boundary.
    """

    left, right = bounds(start, end)
    if (
        isinstance(interval_minutes, bool)
        or not isinstance(interval_minutes, int)
        or interval_minutes not in _ALLOWED_INTERVAL_MINUTES
    ):
        raise EnergyError("invalid_frequency", "Use a target interval of 15, 30 or 60 minutes.")
    if any(not isinstance(column, str) or not column for column in (timestamp, end_column, value)):
        raise EnergyError("invalid_parameters", "Timestamp, end and value columns must be named.")
    if not isinstance(unit, str):
        raise EnergyError("unknown_unit", "Energy input must use Wh, kWh or MWh.")
    unit_exponent = _UNIT_EXPONENT_TO_KWH.get(unit.strip().lower())
    if unit_exponent is None:
        raise EnergyError("unknown_unit", "Energy input must use Wh, kWh or MWh.")

    step = timedelta(minutes=interval_minutes)
    duration = right - left
    if duration % step != timedelta(0):
        raise EnergyError(
            "invalid_range", "Window duration must be divisible by the target interval."
        )
    bin_count = duration // step
    if bin_count > _MAX_OUTPUT_BINS:
        raise EnergyError("output_too_large", "Interval aggregation exceeds 100000 output bins.")

    # A fixed number of per-bin accumulators keeps memory independent of input
    # history length while allowing exact sums of the source numeric text.
    totals = [0] * bin_count
    exponents: list[int | None] = [None] * bin_count

    try:
        iterator = iter(rows)
    except TypeError:
        raise EnergyError(
            "not_tabular", "Interval rows must be an iterable of row objects."
        ) from None

    expected_start = left
    previous_start = None
    for row in iterator:
        if not isinstance(row, dict):
            raise EnergyError("not_tabular", "Every interval row must be an object.")
        missing = [column for column in (timestamp, end_column, value) if column not in row]
        if missing:
            raise EnergyError(
                "column_not_found", "Every interval row requires timestamp, end and value columns."
            )

        observed_start = instant(row[timestamp])
        observed_end = instant(row[end_column])
        if observed_start < left or observed_start >= right:
            raise EnergyError("out_of_window", "An interval starts outside the requested window.")
        if observed_end <= observed_start:
            raise EnergyError("invalid_interval", "Each interval end must follow its start.")
        if observed_end > right:
            raise EnergyError(
                "interval_boundary_mismatch", "An observed interval crosses the window end."
            )

        if previous_start is not None and observed_start == previous_start:
            raise EnergyError(
                "duplicate_interval", "Energy rows contain duplicate interval starts."
            )
        if observed_start != expected_start:
            if observed_start < expected_start:
                raise EnergyError(
                    "overlapping_intervals", "Intervals must be ordered and must not overlap."
                )
            raise EnergyError("incomplete_coverage", "A gap exists between observed intervals.")

        bin_index = int((observed_start - left) // step)
        bin_end = left + (bin_index + 1) * step
        if observed_end > bin_end:
            raise EnergyError(
                "interval_boundary_mismatch",
                "An observed interval crosses a target aggregation boundary.",
            )

        coefficient, exponent = _numeric_parts(row[value], unit_exponent)
        current_exponent = exponents[bin_index]
        if coefficient:
            if current_exponent is None:
                totals[bin_index] = coefficient
                exponents[bin_index] = exponent
            elif exponent < current_exponent:
                _check_exponent_gap(current_exponent - exponent)
                totals[bin_index] = totals[bin_index] * 10 ** (current_exponent - exponent)
                totals[bin_index] += coefficient
                exponents[bin_index] = exponent
            else:
                _check_exponent_gap(exponent - current_exponent)
                totals[bin_index] += coefficient * 10 ** (exponent - current_exponent)
            _check_accumulator_size(totals[bin_index])

        previous_start = observed_start
        expected_start = observed_end

    if expected_start != right:
        raise EnergyError("incomplete_coverage", "Observed intervals do not cover the full window.")

    output: list[dict[str, Any]] = []
    for index, coefficient in enumerate(totals):
        bin_start = left + index * step
        bin_end = bin_start + step
        exact_total = _decimal_from_parts(coefficient, exponents[index] or 0)
        try:
            numeric_total = float(exact_total)
        except (OverflowError, ValueError):
            raise EnergyError(
                "invalid_value",
                "An aggregated energy total cannot be represented as a finite number.",
            ) from None
        if not isfinite(numeric_total):
            raise EnergyError(
                "invalid_value",
                "An aggregated energy total cannot be represented as a finite number.",
            )
        if numeric_total == 0.0 and exact_total != 0:
            raise EnergyError(
                "invalid_value", "An aggregated energy total is below the supported numeric range."
            )
        output.append(
            {
                "timestamp": bin_start.isoformat(),
                "end": bin_end.isoformat(),
                "value": numeric_total,
            }
        )
    return output


def _numeric_parts(raw: Any, unit_exponent: int) -> tuple[int, int]:
    """Validate with the shared series parser, then retain decimal text exactly."""

    if raw is None:
        raise EnergyError("missing_value", "A required numeric value is missing.")
    try:
        text = str(raw).strip()
    except (TypeError, ValueError):
        raise EnergyError("invalid_value", "A series value is not a decimal number.") from None
    if len(text) > _MAX_NUMERIC_TEXT_LENGTH:
        raise EnergyError("invalid_value", "A numeric value exceeds the supported text precision.")
    try:
        parsed_decimal = Decimal(text)
    except (ArithmeticError, TypeError, ValueError):
        raise EnergyError("invalid_value", "A series value is not a decimal number.") from None
    if not parsed_decimal.is_finite():
        raise EnergyError("invalid_value", "Series values must be finite numbers.")
    try:
        parsed_float = numeric_value(raw, allow_missing=False)
    except OverflowError:
        raise EnergyError("invalid_value", "A series value must be a finite number.") from None
    if parsed_float is None:
        raise EnergyError("missing_value", "A required numeric value is missing.")
    if parsed_float == 0.0 and parsed_decimal != 0:
        raise EnergyError("invalid_value", "A series value is below the supported numeric range.")

    parts = parsed_decimal.as_tuple()
    if not isinstance(parts.exponent, int):
        raise EnergyError("invalid_value", "A series value is not a finite decimal number.")
    if len(parts.digits) > _MAX_NUMERIC_DIGITS:
        raise EnergyError(
            "invalid_value", "A numeric value exceeds the supported decimal precision."
        )
    coefficient = int("".join(str(digit) for digit in parts.digits) or "0")
    if parts.sign:
        coefficient = -coefficient
    return coefficient, parts.exponent + unit_exponent


def _check_exponent_gap(gap: int) -> None:
    if gap > _MAX_ALIGNMENT_EXPONENT_GAP:
        raise EnergyError("invalid_value", "A numeric value exceeds the supported decimal scale.")


def _check_accumulator_size(coefficient: int) -> None:
    if abs(coefficient).bit_length() > _MAX_ACCUMULATOR_BITS:
        raise EnergyError(
            "invalid_value", "An aggregated total exceeds the supported decimal precision."
        )


def _decimal_from_parts(coefficient: int, exponent: int) -> Decimal:
    if coefficient == 0:
        return Decimal(0)
    digits = tuple(int(digit) for digit in str(abs(coefficient)))
    return Decimal((int(coefficient < 0), digits, exponent))
