from datetime import timedelta
from tracemalloc import get_traced_memory, start, stop

import pytest

from energy_agent_tools.interval_chunk_bounds import interval_chunk_bounds


def test_offset_equivalence_uses_exact_utc_microseconds():
    rows = [
        {
            "timestamp": "1970-01-01T01:00:00.000000+01:00",
            "interval_end": "1970-01-01T01:00:00.000001+01:00",
        },
        {
            "timestamp": "1970-01-01T00:00:00Z",
            "interval_end": "1970-01-01T00:00:00.000001Z",
        },
    ]

    assert interval_chunk_bounds(rows) == (0, 1)


def test_bounds_cover_unsorted_rows_with_pre_epoch_microsecond_precision():
    rows = [
        {
            "timestamp": "1970-01-01T00:00:00.000001Z",
            "interval_end": "1970-01-01T00:00:00.000004Z",
        },
        {
            "timestamp": "1969-12-31T23:59:59.999999Z",
            "interval_end": "1970-01-01T00:00:00.000002Z",
        },
        {
            "timestamp": "1969-12-31T23:59:59.999998Z",
            "interval_end": "1969-12-31T23:59:59.999999Z",
        },
    ]

    assert interval_chunk_bounds(rows) == (-2, 4)


def test_custom_timestamp_and_end_columns_are_used():
    rows = [
        {
            "begins": "2026-01-01T00:00:00Z",
            "finishes": "2026-01-01T00:30:00Z",
            "interval_end": "not a timestamp",
        }
    ]

    assert interval_chunk_bounds(rows, timestamp_column="begins", end_column="finishes") == (
        1_767_225_600_000_000,
        1_767_227_400_000_000,
    )


@pytest.mark.parametrize(
    ("row", "expected_end"),
    [
        (
            {
                "timestamp": "1970-01-01T00:00:00Z",
                "interval_end": "1970-01-01T00:00:01Z",
                "to": "1970-01-01T00:00:02Z",
                "end": "1970-01-01T00:00:03Z",
            },
            1_000_000,
        ),
        (
            {
                "timestamp": "1970-01-01T00:00:00Z",
                "to": "1970-01-01T00:00:02Z",
                "end": "1970-01-01T00:00:03Z",
            },
            2_000_000,
        ),
        (
            {
                "timestamp": "1970-01-01T00:00:00Z",
                "end": "1970-01-01T00:00:03Z",
            },
            3_000_000,
        ),
    ],
)
def test_standard_end_columns_use_declared_priority(row, expected_end):
    assert interval_chunk_bounds([row]) == (0, expected_end)


def test_present_but_invalid_higher_priority_end_does_not_fall_through():
    rows = [
        {
            "timestamp": "1970-01-01T00:00:00Z",
            "interval_end": None,
            "to": "1970-01-01T00:00:01Z",
        }
    ]

    assert interval_chunk_bounds(rows) is None


def test_resolution_infers_end_only_when_positive():
    rows = [{"timestamp": "1970-01-01T00:00:00.000001Z"}]

    assert interval_chunk_bounds(rows, resolution=timedelta(minutes=15)) == (
        1,
        900_000_001,
    )
    assert interval_chunk_bounds(rows, resolution=timedelta(0)) is None
    assert interval_chunk_bounds(rows, resolution=timedelta(seconds=-1)) is None


@pytest.mark.parametrize(
    "bad_row",
    [
        {},
        {"timestamp": None, "end": "2026-01-01T00:01:00Z"},
        {"timestamp": "2026-01-01T00:00:00"},
        {"timestamp": "2026-01-01T00:00:00Z"},
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "interval_end": "2026-01-01T00:00:00Z",
        },
        {
            "timestamp": "2026-01-01T00:01:00Z",
            "interval_end": "2026-01-01T00:00:00Z",
        },
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "interval_end": 123,
        },
    ],
)
def test_one_bad_row_invalidates_the_entire_chunk(bad_row):
    good_row = {
        "timestamp": "2026-01-01T00:00:00Z",
        "interval_end": "2026-01-01T00:01:00Z",
    }

    assert interval_chunk_bounds([good_row, bad_row]) is None


def test_explicit_end_column_must_exist_on_every_row():
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "finish": "2026-01-01T00:01:00Z",
        },
        {"timestamp": "2026-01-01T00:01:00Z"},
    ]

    assert interval_chunk_bounds(rows, end_column="finish") is None


def test_timestamp_and_resolution_overflow_fall_back_to_no_bounds():
    overflowing_offset = [
        {
            "timestamp": "0001-01-01T00:00:00+23:59",
            "end": "0001-01-01T00:01:00+23:59",
        }
    ]
    overflowing_resolution = [{"timestamp": "9999-12-31T23:59:59.999999Z"}]

    assert interval_chunk_bounds(overflowing_offset) is None
    assert (
        interval_chunk_bounds(overflowing_resolution, resolution=timedelta(microseconds=1)) is None
    )


def test_empty_chunk_has_no_bounds():
    assert interval_chunk_bounds([]) is None


def test_generator_is_consumed_once_with_bounded_memory():
    class OnePassRows:
        def __init__(self, count: int) -> None:
            self.count = count
            self.iterations = 0

        def __iter__(self):
            if self.iterations:
                raise AssertionError("rows must be consumed only once")
            self.iterations += 1
            for _ in range(self.count):
                yield {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "interval_end": "2026-01-01T00:01:00Z",
                }

    rows = OnePassRows(20_000)
    start()
    try:
        result = interval_chunk_bounds(rows)
        _, peak_bytes = get_traced_memory()
    finally:
        stop()

    assert result is not None
    assert rows.iterations == 1
    assert peak_bytes < 500_000
