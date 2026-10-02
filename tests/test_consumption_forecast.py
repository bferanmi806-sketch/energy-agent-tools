from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import sin, tau
from zoneinfo import ZoneInfo

import pytest

from energy_agent_tools.consumption_forecast import forecast
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult

_ZONE = "Europe/London"
_STEP = timedelta(minutes=15)
_PARAMETERS = {
    "timezone": _ZONE,
    "interval_minutes": 15,
    "start": "2025-03-29T00:00:00+00:00",
    "end": "2025-04-06T01:00:00+01:00",
}


def _weekly_value(instant: datetime, *, interval_minutes: int = 15) -> float:
    local = instant.astimezone(ZoneInfo(_ZONE))
    slot = (local.hour * 60 + local.minute) // interval_minutes
    return 0.24 + local.weekday() * 0.035 + slot * 0.001


def _history(
    *,
    start: datetime = datetime(2024, 12, 29, tzinfo=UTC),
    days: int = 90,
    noisy: bool = False,
    temperature_effect: bool = False,
) -> EnergyResult:
    count = days * 24 * 60 // 15
    rows: list[dict[str, object]] = []
    for index in range(count):
        instant = start + index * _STEP
        temperature = _temperature(index)
        value = _weekly_value(instant)
        if temperature_effect:
            value += 0.18 * temperature
        if noisy:
            value += 0.018 * sin(index * 0.73) + 0.007 * sin(index * 0.19)
        rows.append(
            {
                "timestamp": instant.isoformat(),
                "end": (instant + _STEP).isoformat(),
                "value": value,
            }
        )
    return EnergyResult(
        data=rows,
        kind=DataKind.METERED,
        unit="kWh",
        source="synthetic-meter",
        timezone=_ZONE,
        resolution="15min",
        site_id="site-1",
        asset_id="meter-1",
        time_start=start,
        time_end=start + days * timedelta(days=1),
        quantity_shape="interval",
    )


def _temperature(index: int, *, offset: float = 0.0) -> float:
    return (
        8.0
        + 0.055 * index / 96
        + 4.0 * sin(tau * index / (96 * 34))
        + 1.3 * sin(tau * (index % 96) / 96)
        + offset
    )


def _history_context(history: EnergyResult) -> EnergyResult:
    rows = history.data
    assert isinstance(rows, list)
    values = [
        {"timestamp": row["timestamp"], "value": _temperature(index)}
        for index, row in enumerate(rows)
    ]
    return EnergyResult(
        data=values,
        kind=DataKind.METERED,
        unit="°C",
        source="synthetic-temperature-observations",
        timezone=_ZONE,
        site_id="site-1",
        asset_id="meter-1",
    )


def _future_context(
    start: datetime, count: int, *, offset: float = 0.0, history_rows: int = 90 * 96
) -> EnergyResult:
    rows = [
        {
            "timestamp": (start + index * _STEP).isoformat(),
            "value": _temperature(history_rows + index, offset=offset),
        }
        for index in range(count)
    ]
    return EnergyResult(
        data=rows,
        kind=DataKind.FORECAST,
        unit="°C",
        source="synthetic-weather-forecast",
        timezone=_ZONE,
        site_id="site-1",
        asset_id="meter-1",
    )


def _forecast(history: EnergyResult, parameters: dict[str, object] | None = None) -> EnergyResult:
    return forecast(
        ("history-1", history),
        dict(_PARAMETERS if parameters is None else parameters),
    )


def test_forecasts_eight_days_from_ninety_days_of_exact_weekly_history() -> None:
    history = _history()
    result = _forecast(history)

    intervals = result.data["intervals"]
    assert len(intervals) == 8 * 96
    assert result.kind is DataKind.FORECAST
    assert result.unit == "kWh"
    assert result.site_id == "site-1"
    assert result.asset_id == "meter-1"
    assert result.data["summary"]["interval_count"] == 8 * 96
    assert result.data["model"]["method"] == "weekly_calendar_profile"
    assert result.data["model"]["heldout_rows"] == 14 * 96
    assert result.provenance[0]["inputs"][0]["artifact_id"] == "history-1"
    assert result.provenance[0]["inputs"][0]["kind"] == "metered"
    assert all(row["value"] == pytest.approx(_weekly_value(
        datetime.fromisoformat(row["timestamp"]).astimezone(UTC)
    )) for row in intervals)


def test_temperature_candidate_must_win_holdout_and_future_context_does_not_leak() -> None:
    history = _history(temperature_effect=True, noisy=True)
    history_rows = history.data
    assert isinstance(history_rows, list)
    history_context = _history_context(history)
    start = datetime.fromisoformat(_PARAMETERS["start"]).astimezone(UTC)
    future_count = 8 * 96
    cold = _future_context(start, future_count, offset=0.0)
    warm = _future_context(start, future_count, offset=8.0)
    parameters = dict(_PARAMETERS)

    cold_result = forecast(
        ("history-1", history),
        parameters,
        historical_context=("historical-weather", history_context),
        future_context=("future-weather-cold", cold),
    )
    warm_result = forecast(
        ("history-1", history),
        parameters,
        historical_context=("historical-weather", history_context),
        future_context=("future-weather-warm", warm),
    )

    cold_model = cold_result.data["model"]
    warm_model = warm_result.data["model"]
    assert cold_model["method"] == "temperature_adjusted_weekly_profile"
    assert cold_model["metrics"] == warm_model["metrics"]
    assert cold_model["context_used"] is True
    assert cold_model["context"]["evaluated"] is True
    assert cold_model["context"]["historical_artifact_id"] == "historical-weather"
    assert cold_result.provenance[0]["inputs"][2]["kind"] == "forecast"
    assert cold_result.data["intervals"][0]["value"] < warm_result.data["intervals"][0]["value"]
    assert cold_model["metrics"]["temperature_candidate_mae_kwh"] < cold_model["metrics"][
        "weekly_profile_mae_kwh"
    ]


def test_holdout_residual_bands_are_ordered_and_nonzero_for_noisy_history() -> None:
    result = _forecast(_history(noisy=True))

    for row in result.data["intervals"]:
        assert 0 <= row["lower"] <= row["value"] <= row["upper"]
    assert any(row["lower"] < row["value"] for row in result.data["intervals"])
    assert result.data["model"]["nominal_interval_coverage"] == pytest.approx(0.9)
    assert result.data["model"]["band_method"] == "symmetric empirical absolute heldout errors"
    assert "not guaranteed" in " ".join(result.assumptions)
    assert "complete forecast horizon" in " ".join(result.assumptions)


@pytest.mark.parametrize(
    ("start", "finish", "expected_date", "expected_count"),
    [
        (
            datetime(2025, 3, 29, tzinfo=UTC),
            datetime(2025, 4, 1, tzinfo=UTC),
            "2025-03-30",
            92,
        ),
        (
            datetime(2025, 10, 25, tzinfo=UTC),
            datetime(2025, 10, 28, tzinfo=UTC),
            "2025-10-26",
            100,
        ),
    ],
)
def test_forecast_keeps_exact_utc_cadence_across_dst(
    start: datetime, finish: datetime, expected_date: str, expected_count: int
) -> None:
    history = _history()
    parameters = {
        "timezone": _ZONE,
        "interval_minutes": 15,
        "start": start.isoformat(),
        "end": finish.isoformat(),
    }
    result = _forecast(history, parameters)
    intervals = result.data["intervals"]
    local_starts = [datetime.fromisoformat(row["timestamp"]) for row in intervals]
    selected = [instant for instant in local_starts if instant.date().isoformat() == expected_date]

    assert len(intervals) == 3 * 96
    assert len(selected) == expected_count
    for previous, current in zip(intervals, intervals[1:], strict=False):
        assert datetime.fromisoformat(current["timestamp"]).astimezone(UTC) - datetime.fromisoformat(
            previous["timestamp"]
        ).astimezone(UTC) == _STEP
        assert datetime.fromisoformat(current["timestamp"]).astimezone(UTC) == datetime.fromisoformat(
            previous["end"]
        ).astimezone(UTC)


def test_refuses_short_history_gap_wrong_shape_power_and_negative_energy() -> None:
    full = _history()
    rows = full.data
    assert isinstance(rows, list)

    short = full.model_copy(
        update={
            "data": rows[:14 * 96],
            "time_end": datetime.fromisoformat(rows[14 * 96 - 1]["end"]),
        }
    )
    with pytest.raises(EnergyError, match="90 days"):
        _forecast(short)

    missing = rows[:]
    missing.pop(100)
    gapped = full.model_copy(update={"data": missing})
    with pytest.raises(EnergyError, match="contiguous"):
        _forecast(gapped)

    wrong_shape = full.model_copy(update={"quantity_shape": "instantaneous"})
    with pytest.raises(EnergyError, match="interval energy"):
        _forecast(wrong_shape)

    power = full.model_copy(update={"unit": "kW"})
    with pytest.raises(EnergyError, match="Wh, kWh or MWh"):
        _forecast(power)

    negative_rows = rows[:]
    negative_rows[0] = dict(negative_rows[0], value=-0.1)
    negative = full.model_copy(update={"data": negative_rows})
    with pytest.raises(EnergyError, match="nonnegative"):
        _forecast(negative)


def test_refuses_naive_timestamps_and_oversized_horizon() -> None:
    history = _history()
    naive = dict(_PARAMETERS, start="2025-03-29T00:00:00")
    with pytest.raises(EnergyError, match="explicit UTC offset"):
        _forecast(history, naive)

    too_long = dict(
        _PARAMETERS,
        start="2025-03-29T00:00:00+00:00",
        end="2025-05-01T00:00:00+01:00",
    )
    with pytest.raises(EnergyError, match="31 days"):
        _forecast(history, too_long)


def test_refuses_incomplete_or_mismatched_temperature_context() -> None:
    history = _history()
    history_context = _history_context(history)
    start = datetime.fromisoformat(_PARAMETERS["start"]).astimezone(UTC)
    future_context = _future_context(start, 8 * 96)

    with pytest.raises(EnergyError, match="both historical and future"):
        forecast(
            ("history-1", history),
            dict(_PARAMETERS),
            historical_context=("historical-weather", history_context),
        )

    bad_future = future_context.model_copy(update={"data": future_context.data[:-1]})
    with pytest.raises(EnergyError, match="cover every required timestamp"):
        forecast(
            ("history-1", history),
            dict(_PARAMETERS),
            historical_context=("historical-weather", history_context),
            future_context=("future-weather", bad_future),
        )
