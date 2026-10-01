from datetime import timedelta

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.time import day_window
from energy_agent_tools.windows import select_window


@pytest.mark.parametrize("date,count", [("2026-03-29", 46), ("2026-10-25", 50)])
def test_window_uses_utc_grid_for_dst_days(date, count):
    from datetime import date as Date

    start, end = day_window(Date.fromisoformat(date), "Europe/London")
    result = EnergyResult(
        data=[
            {"timestamp": (start + timedelta(minutes=30 * i)).isoformat(), "value": 1}
            for i in range(count + 1)
        ],
        kind=DataKind.METERED,
        unit="kWh",
        source="meter",
        resolution="30min",
    )
    selected = select_window(result, "source", start.isoformat(), end.isoformat(), "timestamp")
    assert len(selected.data) == count
    assert selected.kind == DataKind.METERED
    assert selected.provenance[-1]["coverage"]["missing_instants"] == 0
    assert selected.time_end == end


def test_window_keeps_forecast_kind_and_long_form_weather():
    source = EnergyResult(
        data=[
            {"timestamp": "2026-01-01T00:00:00Z", "variable": var, "value": 1}
            for var in ("temperature", "irradiance")
        ],
        kind=DataKind.FORECAST,
        unit="variable",
        source="weather",
        resolution="1h",
    )
    selected = select_window(
        source, "source", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "timestamp"
    )
    assert selected.kind == DataKind.FORECAST
    assert len(selected.data) == 2
    assert selected.provenance[-1]["coverage"]["observed_instants"] == 1


def test_window_refuses_partial_energy_intervals():
    source = EnergyResult(
        data=[{"timestamp": "2026-01-01T00:00:00Z", "value": 2}],
        kind=DataKind.METERED,
        unit="kWh",
        source="meter",
        resolution="1h",
    )
    with pytest.raises(EnergyError, match="cuts an observed interval"):
        select_window(source, "source", "2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z", "timestamp")


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"timestamp": "2026-01-01T00:00:00Z", "value": 1},
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 1},
        ],
        [
            {"timestamp": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z", "value": 1},
            {"timestamp": "2026-01-01T00:30:00Z", "to": "2026-01-01T01:30:00Z", "value": 1},
        ],
    ],
)
def test_window_refuses_double_counting_energy_intervals(rows):
    source = EnergyResult(
        data=rows, kind=DataKind.METERED, unit="kWh", source="meter", resolution="1h"
    )
    with pytest.raises(EnergyError, match="overlap|duplicate"):
        select_window(source, "source", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", "timestamp")
