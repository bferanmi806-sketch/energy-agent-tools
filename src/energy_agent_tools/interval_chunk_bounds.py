"""Conservatively derive an index range from every row in a chunk."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import EnergyError
from .windows import instant

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECONDS_PER_DAY = 86_400_000_000
_MICROSECONDS_PER_SECOND = 1_000_000
_STANDARD_END_COLUMNS = ("interval_end", "to", "end")


def _utc_microseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * _MICROSECONDS_PER_DAY
        + delta.seconds * _MICROSECONDS_PER_SECOND
        + delta.microseconds
    )


def interval_chunk_bounds(
    rows: Iterable[dict[str, Any]],
    *,
    timestamp_column: str = "timestamp",
    end_column: str | None = None,
    resolution: timedelta | None = None,
) -> tuple[int, int] | None:
    """Return the full UTC-microsecond extent of rows, or ``None`` if uncertain.

    The helper only describes timestamp extents. Callers must choose columns whose
    semantics match the data they want to index.
    """
    minimum_start: int | None = None
    maximum_end: int | None = None

    try:
        for row in rows:
            if not isinstance(row, dict) or timestamp_column not in row:
                return None
            start_value = row[timestamp_column]
            if not isinstance(start_value, str):
                return None
            start = instant(start_value)

            selected_end_column = end_column
            if selected_end_column is None:
                selected_end_column = next(
                    (column for column in _STANDARD_END_COLUMNS if column in row), None
                )

            if selected_end_column is not None:
                if selected_end_column not in row:
                    return None
                end_value = row[selected_end_column]
                if not isinstance(end_value, str):
                    return None
                end = instant(end_value)
            elif isinstance(resolution, timedelta) and resolution > timedelta(0):
                end = start + resolution
            else:
                return None

            if end <= start:
                return None

            start_us = _utc_microseconds(start)
            end_us = _utc_microseconds(end)
            minimum_start = start_us if minimum_start is None else min(minimum_start, start_us)
            maximum_end = end_us if maximum_end is None else max(maximum_end, end_us)
    except (EnergyError, OverflowError, TypeError, ValueError, AttributeError):
        return None

    if minimum_start is None or maximum_end is None:
        return None
    return minimum_start, maximum_end
