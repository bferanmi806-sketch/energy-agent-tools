"""Select observed rows without changing their measurement kind or inventing coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from .models import EnergyError, EnergyResult, Json


def instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, AttributeError):
        raise EnergyError("invalid_timestamp", "Timestamps require explicit UTC offsets.") from None


def bounds(start: str, end: str) -> tuple[datetime, datetime]:
    left, right = instant(start), instant(end)
    if left >= right:
        raise EnergyError("invalid_range", "Start must precede the exclusive end.")
    return left, right


def select_window(
    result: EnergyResult,
    artifact_id: str,
    start: str,
    end: str,
    timestamp: str,
    end_column: str | None = None,
) -> EnergyResult:
    left, right = bounds(start, end)
    if not isinstance(result.data, list) or len(result.data) > 100_000:
        raise EnergyError("not_tabular", "Window selection requires at most 100000 rows.")
    selected: list[Json] = []
    observed: set[datetime] = set()
    step = None
    if result.resolution:
        try:
            step = pd.Timedelta(result.resolution).to_pytimedelta()
            if step.total_seconds() <= 0:
                step = None
        except (ValueError, TypeError, OverflowError):
            pass
    observed_ends: list[datetime] = []
    energy_intervals: list[tuple[datetime, datetime | None]] = []
    is_energy = result.unit in {"Wh", "kWh", "MWh"}
    for row in result.data:
        if not isinstance(row, dict) or timestamp not in row:
            raise EnergyError("timestamp_required", "Every row requires the timestamp column.")
        point = instant(row[timestamp])
        if end_column is not None and end_column not in row:
            raise EnergyError("column_not_found", "Declared interval end column is missing.")
        edge = (
            row[end_column]
            if end_column
            else next((row[key] for key in ("interval_end", "to", "end") if key in row), None)
        )
        finish = instant(edge) if edge is not None else point + step if step else None
        if finish is not None and finish <= point:
            raise EnergyError("invalid_interval", "Interval end must follow its start.")
        if finish is not None and point < left < finish:
            raise EnergyError("interval_boundary_mismatch", "The start cuts an observed interval.")
        if left <= point < right:
            if finish is not None and finish > right:
                raise EnergyError(
                    "interval_boundary_mismatch", "The end cuts an observed interval."
                )
            if is_energy:
                if point in observed:
                    raise EnergyError(
                        "duplicate_interval", "Energy rows contain duplicate interval starts."
                    )
                energy_intervals.append((point, finish))
            selected.append(dict(row))
            observed.add(point)
            if finish is not None:
                observed_ends.append(finish)
    if is_energy:
        ordered = sorted(energy_intervals)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous[1] is not None and previous[1] > current[0]:
                raise EnergyError(
                    "overlapping_intervals", "Energy rows contain overlapping intervals."
                )
    if not selected:
        raise EnergyError("insufficient_data", "No observations exist in the requested window.")
    coverage: Json = {
        "requested_start": left.isoformat(),
        "requested_end": right.isoformat(),
        "end_exclusive": True,
        "observed_instants": len(observed),
        "expected_instants": None,
        "missing_instants": None,
    }
    warnings = list(result.warnings)
    missing_values = sum("value" in row and row["value"] is None for row in selected)
    coverage["missing_values"] = missing_values
    if missing_values:
        warnings.append(
            f"Requested window contains {missing_values} missing value(s); no values were filled."
        )
    if step and (right - left) % step == pd.Timedelta(0):
        count = int((right - left) / step)
        if count > 100_000:
            raise EnergyError("output_too_large", "Coverage checking exceeds 100000 intervals.")
        expected = {left + i * step for i in range(count)}
        if observed <= expected:
            missing = len(expected - observed)
            coverage.update(expected_instants=count, missing_instants=missing)
            if missing:
                warnings.append(
                    f"Requested window contains {missing} missing timestamp interval(s); totals cover observed data only."
                )
        else:
            warnings.append(
                "Observed timestamps do not match the requested resolution grid; complete coverage is unverified."
            )
    else:
        warnings.append(
            "Complete window coverage is unverified because a compatible fixed resolution is unavailable."
        )
    data = result.model_dump()
    data.update(
        data=selected,
        time_start=min(observed),
        time_end=max(observed_ends) if observed_ends else None,
        warnings=list(dict.fromkeys(warnings)),
        provenance=[
            *result.provenance,
            {
                "operation": "window",
                "artifact_id": artifact_id,
                "input_kind": result.kind.value,
                "source": result.source,
                "coverage": coverage,
            },
        ],
        assumptions=[
            *result.assumptions,
            "Window selection preserves observed values and source kind; no interpolation or prorating.",
        ],
    )
    return EnergyResult.model_validate(data)
