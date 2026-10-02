import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools.interval_aggregation import aggregate_interval_energy
from energy_agent_tools.models import EnergyError


def _rows(
    start: datetime,
    intervals: list[tuple[int, int, object]],
) -> list[dict[str, object]]:
    return [
        {
            "timestamp": (start + timedelta(minutes=offset)).isoformat(),
            "end": (start + timedelta(minutes=finish)).isoformat(),
            "value": value,
        }
        for offset, finish, value in intervals
    ]


@pytest.mark.parametrize(
    ("unit", "measurement"),
    [("Wh", 250), ("kWh", 0.25), ("MWh", 0.00025)],
)
def test_sums_observed_energy_and_converts_to_kwh(unit, measurement):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _rows(base, [(0, 15, measurement), (15, 30, measurement)])

    result = aggregate_interval_energy(
        rows,
        start="2026-01-01T01:00:00+01:00",
        end="2026-01-01T01:30:00+01:00",
        unit=unit,
    )

    assert result == [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T00:30:00+00:00",
            "value": 0.5,
        }
    ]


def test_uses_exact_decimal_accumulation_for_numeric_text():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _rows(
        base,
        [
            (0, 10, "100000000000000000000"),
            (10, 20, "1"),
            (20, 30, "-100000000000000000000"),
        ],
    )

    result = aggregate_interval_energy(
        rows,
        start=base.isoformat(),
        end=(base + timedelta(minutes=30)).isoformat(),
        unit="kWh",
    )

    assert result[0]["value"] == 1.0


@pytest.mark.parametrize(
    "intervals",
    [
        [(15, 30, 1), (30, 60, 1)],  # Missing the requested beginning.
        [(0, 15, 1), (30, 60, 1)],  # Gap between intervals.
        [(0, 15, 1), (15, 30, 1)],  # Missing the requested end.
    ],
)
def test_rejects_incomplete_coverage(intervals):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(EnergyError) as caught:
        aggregate_interval_energy(
            _rows(base, intervals),
            start=base.isoformat(),
            end=(base + timedelta(hours=1)).isoformat(),
            unit="kWh",
        )
    assert caught.value.code == "incomplete_coverage"


def test_rejects_overlapping_and_duplicate_intervals():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    arguments = {
        "start": base.isoformat(),
        "end": (base + timedelta(minutes=30)).isoformat(),
        "unit": "kWh",
    }
    overlapping = _rows(base, [(0, 15, 1), (10, 30, 1)])
    with pytest.raises(EnergyError) as overlap_error:
        aggregate_interval_energy(overlapping, **arguments)
    assert overlap_error.value.code == "overlapping_intervals"

    duplicate = _rows(base, [(0, 15, 1), (0, 30, 1)])
    with pytest.raises(EnergyError) as duplicate_error:
        aggregate_interval_energy(duplicate, **arguments)
    assert duplicate_error.value.code == "duplicate_interval"


def test_rejects_interval_that_crosses_a_target_bin_edge():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _rows(base, [(0, 20, 1), (20, 40, 1), (40, 60, 1)])
    with pytest.raises(EnergyError) as caught:
        aggregate_interval_energy(
            rows,
            start=base.isoformat(),
            end=(base + timedelta(hours=1)).isoformat(),
            unit="kWh",
        )
    assert caught.value.code == "interval_boundary_mismatch"


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), True])
def test_rejects_nonfinite_and_boolean_values(raw):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _rows(base, [(0, 30, raw)])
    with pytest.raises(EnergyError) as caught:
        aggregate_interval_energy(
            rows,
            start=base.isoformat(),
            end=(base + timedelta(minutes=30)).isoformat(),
            unit="kWh",
        )
    assert caught.value.code == "invalid_value"


def test_rejects_missing_values_and_required_columns():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _rows(base, [(0, 30, None)])
    with pytest.raises(EnergyError) as missing_value:
        aggregate_interval_energy(
            rows,
            start=base.isoformat(),
            end=(base + timedelta(minutes=30)).isoformat(),
            unit="kWh",
        )
    assert missing_value.value.code == "missing_value"

    rows[0].pop("end")
    with pytest.raises(EnergyError) as missing_column:
        aggregate_interval_energy(
            rows,
            start=base.isoformat(),
            end=(base + timedelta(minutes=30)).isoformat(),
            unit="kWh",
        )
    assert missing_column.value.code == "column_not_found"


def test_rejects_precision_overflow_and_unrepresentable_float_totals():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    end = base + timedelta(minutes=30)
    overlong = "1." + "0" * 5_000
    with pytest.raises(EnergyError) as precision:
        aggregate_interval_energy(
            _rows(base, [(0, 30, overlong)]),
            start=base.isoformat(),
            end=end.isoformat(),
            unit="kWh",
        )
    assert precision.value.code == "invalid_value"

    with pytest.raises(EnergyError) as overflow:
        aggregate_interval_energy(
            _rows(base, [(0, 15, "1e308"), (15, 30, "1e308")]),
            start=base.isoformat(),
            end=end.isoformat(),
            unit="kWh",
        )
    assert overflow.value.code == "invalid_value"

    with pytest.raises(EnergyError) as underflow:
        aggregate_interval_energy(
            _rows(base, [(0, 30, "1e-323")]),
            start=base.isoformat(),
            end=end.isoformat(),
            unit="Wh",
        )
    assert underflow.value.code == "invalid_value"


def test_rejects_out_of_window_and_malformed_intervals():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    end = base + timedelta(minutes=30)
    with pytest.raises(EnergyError) as outside:
        aggregate_interval_energy(
            _rows(base, [(-15, 15, 1)]),
            start=base.isoformat(),
            end=end.isoformat(),
            unit="kWh",
        )
    assert outside.value.code == "out_of_window"

    with pytest.raises(EnergyError) as malformed:
        aggregate_interval_energy(
            _rows(base, [(0, 0, 1)]),
            start=base.isoformat(),
            end=end.isoformat(),
            unit="kWh",
        )
    assert malformed.value.code == "invalid_interval"


@pytest.mark.parametrize("unit", ["W", "GWh", "", "energy"])
def test_rejects_unsupported_units(unit):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(EnergyError) as caught:
        aggregate_interval_energy(
            [],
            start=base.isoformat(),
            end=(base + timedelta(minutes=30)).isoformat(),
            unit=unit,
        )
    assert caught.value.code == "unknown_unit"


def test_rejects_invalid_cadence_window_duration_and_output_size():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(EnergyError) as cadence:
        aggregate_interval_energy(
            [],
            start=base.isoformat(),
            end=(base + timedelta(minutes=30)).isoformat(),
            unit="kWh",
            interval_minutes=20,
        )
    assert cadence.value.code == "invalid_frequency"

    with pytest.raises(EnergyError) as duration:
        aggregate_interval_energy(
            [],
            start=base.isoformat(),
            end=(base + timedelta(minutes=45)).isoformat(),
            unit="kWh",
        )
    assert duration.value.code == "invalid_range"

    with pytest.raises(EnergyError) as size:
        aggregate_interval_energy(
            [],
            start="1900-01-01T00:00:00Z",
            end="2000-01-01T00:00:00Z",
            unit="kWh",
            interval_minutes=15,
        )
    assert size.value.code == "output_too_large"


def test_aggregates_129600_generated_minute_intervals_in_one_pass():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    count = 90 * 24 * 60

    def minute_history():
        for index in range(count):
            interval_start = base + timedelta(minutes=index)
            interval_end = interval_start + timedelta(minutes=1)
            yield {
                "timestamp": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "value": "0.0001",
            }

    tracemalloc.start()
    try:
        result = aggregate_interval_energy(
            minute_history(),
            start=base.isoformat(),
            end=(base + timedelta(days=90)).isoformat(),
            unit="kWh",
        )
        _, peak_python_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_python_bytes < 32 * 1024 * 1024
    assert len(result) == 4_320
    assert result[0]["value"] == pytest.approx(0.003)
    assert result[-1]["value"] == pytest.approx(0.003)
    assert sum(row["value"] for row in result) == pytest.approx(12.96)
