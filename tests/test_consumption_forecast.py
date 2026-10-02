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
    assert result.data["model"]["selection_rows"] == 7 * 96
    assert result.data["model"]["calibration_rows"] == 7 * 96
    assert (
        result.data["model"]["final_training_start"]
        == result.data["model"]["evaluation_training_start"]
    )
    assert (
        result.data["model"]["final_training_end"] > result.data["model"]["evaluation_training_end"]
    )
    assert all(
        row["value"]
        == pytest.approx(_weekly_value(datetime.fromisoformat(row["timestamp"]).astimezone(UTC)))
        for row in intervals
    )


def test_accepts_three_calendar_months_that_are_under_ninety_days() -> None:
    start = datetime(2025, 2, 1, tzinfo=UTC)
    history = _history(start=start, days=89)
    forecast_start = start + timedelta(days=89)
    parameters = {
        "timezone": _ZONE,
        "interval_minutes": 15,
        "start": forecast_start.isoformat(),
        "end": (forecast_start + timedelta(days=8)).isoformat(),
    }

    result = _forecast(history, parameters)

    assert len(result.data["intervals"]) == 8 * 96
    assert result.data["model"]["selection_rows"] == 7 * 96
    assert result.data["model"]["calibration_rows"] == 7 * 96


def test_forecasts_eight_local_days_of_thirty_minute_energy() -> None:
    start = datetime(2025, 7, 1, tzinfo=UTC)
    history_end = datetime(2025, 10, 1, tzinfo=UTC)
    interval = timedelta(minutes=30)
    rows = [
        {
            "timestamp": (start + index * interval).isoformat(),
            "end": (start + (index + 1) * interval).isoformat(),
            "value": 0.5,
        }
        for index in range(92 * 48)
    ]
    history = EnergyResult(
        data=rows,
        kind=DataKind.METERED,
        unit="kWh",
        source="synthetic-half-hour-meter",
        timezone=_ZONE,
        resolution="30min",
        site_id="site-1",
        asset_id="meter-1",
        time_start=start,
        time_end=history_end,
        quantity_shape="interval",
    )

    result = _forecast(
        history,
        {
            "timezone": _ZONE,
            "interval_minutes": 30,
            "start": history_end.isoformat(),
            "end": (history_end + timedelta(days=8)).isoformat(),
        },
    )

    assert len(result.data["intervals"]) == 8 * 48
    assert result.data["summary"]["total_kwh"] == pytest.approx(192.0)


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
    assert (
        cold_model["metrics"]["temperature_candidate_mae_kwh"]
        < cold_model["metrics"]["weekly_profile_mae_kwh"]
    )


def test_calibration_rows_do_not_change_selection_and_final_refit_uses_all_history() -> None:
    history = _history(temperature_effect=True, noisy=True)
    original_rows = history.data
    assert isinstance(original_rows, list)
    changed_rows = [dict(row) for row in original_rows]
    for index in range(len(changed_rows) - 7 * 96, len(changed_rows)):
        changed_rows[index]["value"] = float(changed_rows[index]["value"]) + 0.6
    changed_history = history.model_copy(update={"data": changed_rows})
    historical_context = _history_context(history)
    start = datetime.fromisoformat(_PARAMETERS["start"]).astimezone(UTC)
    future_context = _future_context(start, 8 * 96)

    original = forecast(
        ("history-1", history),
        dict(_PARAMETERS),
        historical_context=("weather-history", historical_context),
        future_context=("weather-forecast", future_context),
    )
    changed = forecast(
        ("history-1", changed_history),
        dict(_PARAMETERS),
        historical_context=("weather-history", historical_context),
        future_context=("weather-forecast", future_context),
    )
    original_model = original.data["model"]
    changed_model = changed.data["model"]

    assert original_model["method"] == changed_model["method"]
    assert original_model["method"] == "temperature_adjusted_weekly_profile"
    assert original_model["selection_rows"] == 7 * 96
    assert original_model["calibration_rows"] == 7 * 96
    assert original_model["metrics"]["selection_mae_kwh"] == pytest.approx(
        changed_model["metrics"]["selection_mae_kwh"]
    )
    assert original_model["metrics"]["temperature_candidate_selection_mae_kwh"] == pytest.approx(
        changed_model["metrics"]["temperature_candidate_selection_mae_kwh"]
    )
    assert original_model["final_training_end"] == changed_model["final_training_end"]
    assert original.data["intervals"][0]["value"] != pytest.approx(
        changed.data["intervals"][0]["value"]
    )


def test_holdout_residual_bands_are_ordered_and_nonzero_for_noisy_history() -> None:
    result = _forecast(_history(noisy=True))

    for row in result.data["intervals"]:
        assert 0 <= row["lower"] <= row["value"] <= row["upper"]
    assert any(row["lower"] < row["value"] for row in result.data["intervals"])
    assert result.data["model"]["nominal_interval_coverage"] == pytest.approx(0.9)
    assert result.data["model"]["band_method"].startswith(
        "symmetric empirical absolute errors from the independent final 7-day"
    )
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
        assert (
            datetime.fromisoformat(current["timestamp"]).astimezone(UTC)
            - datetime.fromisoformat(previous["timestamp"]).astimezone(UTC)
            == _STEP
        )
        assert datetime.fromisoformat(current["timestamp"]).astimezone(
            UTC
        ) == datetime.fromisoformat(previous["end"]).astimezone(UTC)


def test_refuses_short_history_gap_wrong_shape_power_and_negative_energy() -> None:
    full = _history()
    rows = full.data
    assert isinstance(rows, list)

    short = full.model_copy(
        update={
            "data": rows[: 14 * 96],
            "time_end": datetime.fromisoformat(rows[14 * 96 - 1]["end"]),
        }
    )
    with pytest.raises(EnergyError, match="70 days"):
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


def test_weather_context_may_use_a_different_asset_at_the_same_site() -> None:
    history = _history(temperature_effect=True, noisy=True)
    historical_weather = _history_context(history).model_copy(update={"asset_id": "weather-1"})
    start = datetime.fromisoformat(_PARAMETERS["start"]).astimezone(UTC)
    future_weather = _future_context(start, 8 * 96).model_copy(update={"asset_id": "weather-1"})

    result = forecast(
        ("history-1", history),
        dict(_PARAMETERS),
        historical_context=("historical-weather", historical_weather),
        future_context=("future-weather", future_weather),
    )

    assert result.data["model"]["context_used"] is True
    assert result.provenance[0]["inputs"][1]["asset_id"] == "weather-1"
    assert result.provenance[0]["inputs"][2]["asset_id"] == "weather-1"

    foreign_site = historical_weather.model_copy(update={"site_id": "site-2"})
    with pytest.raises(EnergyError, match="different site"):
        forecast(
            ("history-1", history),
            dict(_PARAMETERS),
            historical_context=("historical-weather", foreign_site),
            future_context=("future-weather", future_weather),
        )


def test_rejects_numeric_overflow_in_conversion_context_model_and_summary() -> None:
    history = _history()
    rows = history.data
    assert isinstance(rows, list)
    mwh_rows = [dict(row, value=1e308) for row in rows]
    mwh_history = history.model_copy(update={"data": mwh_rows, "unit": "MWh"})
    with pytest.raises(EnergyError, match="overflow"):
        _forecast(mwh_history)

    profile_rows = [dict(row, value=1e308) for row in rows]
    profile_overflow = history.model_copy(update={"data": profile_rows})
    with pytest.raises(EnergyError, match="overflow"):
        _forecast(profile_overflow)

    temperature_history = _history(temperature_effect=True, noisy=True)
    extreme_context = _history_context(temperature_history)
    weather_rows = extreme_context.data
    assert isinstance(weather_rows, list)
    extreme_values = [
        dict(row, value=1e308 if (index // 96) % 2 else -1e308)
        for index, row in enumerate(weather_rows)
    ]
    extreme_context = extreme_context.model_copy(update={"data": extreme_values})
    start = datetime.fromisoformat(_PARAMETERS["start"]).astimezone(UTC)
    with pytest.raises(EnergyError, match="overflow"):
        forecast(
            ("history-1", temperature_history),
            dict(_PARAMETERS),
            historical_context=("extreme-weather-history", extreme_context),
            future_context=("future-weather", _future_context(start, 8 * 96)),
        )

    huge_rows = [dict(row, value=1e305) for row in rows]
    huge_history = history.model_copy(update={"data": huge_rows})
    forecast_start = history.time_end
    assert forecast_start is not None
    huge_horizon = {
        "timezone": _ZONE,
        "interval_minutes": 15,
        "start": forecast_start.isoformat(),
        "end": (forecast_start + timedelta(days=31)).isoformat(),
    }
    with pytest.raises(EnergyError, match="overflow"):
        _forecast(huge_history, huge_horizon)
