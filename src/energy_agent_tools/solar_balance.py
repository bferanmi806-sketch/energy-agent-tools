"""Interval netting estimates for explicitly declared load and solar energy."""

from __future__ import annotations

from math import isfinite
from typing import Any, cast

import pandas as pd

from .models import EnergyError, EnergyResult, Json
from .timeseries import (
    MAX_OUTPUT_ROWS,
    _derived,
    _optional_ends,
    _paired_series,
    _parse_timestamp,
    _timestamp_text,
    _unit_info,
    numeric_value,
)


def reconcile(inputs: list[tuple[str, EnergyResult]], parameters: Json) -> EnergyResult:
    """Estimate interval solar self-use and residual import/export by netting.

    This operation requires the caller to state that the consumption series is
    total load and that no storage is present. It uses explicit interval ends
    from both sources and does not fill missing data or infer meter flows.
    """

    if not isinstance(parameters, dict):
        raise EnergyError("invalid_parameters", "Operation parameters must be an object.")
    if len(inputs) != 2:
        raise EnergyError("invalid_input", "Solar balance requires exactly two inputs.")
    if any(not isinstance(artifact_id, str) or not artifact_id for artifact_id, _ in inputs):
        raise EnergyError("invalid_input", "Each input must include an artifact identifier.")

    if "consumption_basis" not in parameters:
        raise EnergyError(
            "missing_assumption",
            "Declare consumption_basis='total_load' before estimating solar self-consumption.",
        )
    if parameters["consumption_basis"] != "total_load":
        raise EnergyError(
            "invalid_assumption",
            "Solar balance requires consumption_basis='total_load'; grid import is not total load.",
        )
    if "storage_mode" not in parameters:
        raise EnergyError(
            "missing_assumption",
            "Declare storage_mode='none'; battery behavior cannot be inferred.",
        )
    if parameters["storage_mode"] != "none":
        raise EnergyError(
            "invalid_assumption",
            "Solar balance currently requires storage_mode='none'.",
        )

    load, generation, resolution = _paired_series(inputs, parameters)
    for series, label in ((load, "Consumption"), (generation, "Solar generation")):
        if series.result.quantity_shape != "interval":
            raise EnergyError(
                "quantity_shape_mismatch",
                f"{label} must be explicitly declared as interval energy.",
            )

    load_unit = _unit_info(load.result.unit)
    generation_unit = _unit_info(generation.result.unit)
    if load_unit.dimension != "energy" or generation_unit.dimension != "energy":
        raise EnergyError(
            "unit_mismatch", "Solar balance requires energy values in Wh, kWh or MWh."
        )

    end_column = cast(str, parameters.get("end", "end"))
    second_end_column = cast(str, parameters.get("second_end", end_column))
    load_ends = _optional_ends(load, end_column)
    generation_ends = _optional_ends(generation, second_end_column)
    if load_ends is None or generation_ends is None:
        raise EnergyError(
            "missing_interval_end",
            "Both solar balance inputs must provide an explicit end for every interval.",
        )
    if len(load.times) > MAX_OUTPUT_ROWS:
        raise EnergyError("output_too_large", "Solar balance output exceeds the row limit.")

    start, finish = _horizon(parameters)
    if (start is None) != (finish is None):
        raise EnergyError(
            "invalid_range",
            "Provide both start and finish to declare a complete requested horizon.",
        )

    if start is not None and finish is not None:
        if load.times[0] != start or load_ends[-1] != finish:
            raise EnergyError(
                "insufficient_coverage",
                "Intervals must cover the requested horizon from start through finish.",
            )
        for end, next_start in zip(load_ends[:-1], load.times[1:], strict=True):
            if end != next_start:
                raise EnergyError(
                    "incomplete_horizon",
                    "Intervals must be contiguous across the requested horizon.",
                )

    has_gaps = any(
        end != next_start for end, next_start in zip(load_ends[:-1], load.times[1:], strict=True)
    )
    output: list[Json] = []
    totals = {
        "load_kwh": 0.0,
        "generation_kwh": 0.0,
        "self_consumption_kwh": 0.0,
        "estimated_import_kwh": 0.0,
        "estimated_export_kwh": 0.0,
    }
    for timestamp, end, load_value, generation_value in zip(
        load.times, load_ends, load.values, generation.values, strict=True
    ):
        load_number = numeric_value(load_value, allow_missing=False)
        generation_number = numeric_value(generation_value, allow_missing=False)
        assert load_number is not None and generation_number is not None
        if load_number < 0 or generation_number < 0:
            raise EnergyError(
                "invalid_value", "Load and solar generation values must be nonnegative."
            )

        load_kwh = load_number * load_unit.to_base
        generation_kwh = generation_number * generation_unit.to_base
        values = {
            "load_kwh": load_kwh,
            "generation_kwh": generation_kwh,
            "self_consumption_kwh": min(load_kwh, generation_kwh),
            "estimated_import_kwh": max(load_kwh - generation_kwh, 0.0),
            "estimated_export_kwh": max(generation_kwh - load_kwh, 0.0),
        }
        if any(not isfinite(value) for value in values.values()):
            raise EnergyError("invalid_value", "Converted energy values must be finite numbers.")
        output.append(
            {
                "timestamp": _timestamp_text(timestamp, load.result.timezone),
                "end": _timestamp_text(end, load.result.timezone),
                **values,
            }
        )
        for field, value in values.items():
            totals[field] += value
            if not isfinite(totals[field]):
                raise EnergyError("invalid_value", "Solar balance totals must remain finite.")

    load_total = totals["load_kwh"]
    generation_total = totals["generation_kwh"]
    self_total = totals["self_consumption_kwh"]
    summary: dict[str, Any] = {
        **totals,
        "self_consumption_fraction": (
            self_total / generation_total if generation_total > 0 else None
        ),
        "solar_load_fraction": self_total / load_total if load_total > 0 else None,
    }
    return _derived(
        "solar_balance",
        {"intervals": output, "summary": summary},
        inputs,
        "kWh",
        resolution=resolution,
        assumptions=[
            "Consumption was explicitly declared as total site load.",
            "Storage was explicitly declared absent; no battery charging or discharging is modeled.",
            "No conversion, distribution, inverter, or storage losses are modeled.",
        ],
        warnings=[
            "Interval netting cannot recover opposing import and export flows within an interval, whether simultaneous or alternating.",
            "Estimated import and export are interval netting estimates, not grid-meter readings.",
            *(
                [
                    "Gaps between supplied intervals were excluded; totals cover the supplied intervals only."
                ]
                if has_gaps and start is None
                else []
            ),
        ],
        field_units={
            "load_kwh": "kWh",
            "generation_kwh": "kWh",
            "self_consumption_kwh": "kWh",
            "estimated_import_kwh": "kWh",
            "estimated_export_kwh": "kWh",
            "self_consumption_fraction": "1",
            "solar_load_fraction": "1",
        },
        quantity_shape="interval",
    )


def _horizon(parameters: Json) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    start_raw = parameters.get("start")
    finish_raw = parameters.get("finish")
    start = None if start_raw is None else _parse_timestamp(start_raw)
    finish = None if finish_raw is None else _parse_timestamp(finish_raw)
    if start is not None and finish is not None and finish <= start:
        raise EnergyError("invalid_range", "The requested finish must be after start.")
    return start, finish
