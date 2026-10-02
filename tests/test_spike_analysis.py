from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.spike_analysis import explain


def _rows(values, *, start=datetime(2026, 1, 1, tzinfo=UTC), step=timedelta(hours=1)):
    return [
        {
            "timestamp": (start + step * index).isoformat(),
            "end": (start + step * (index + 1)).isoformat(),
            "value": value,
        }
        for index, value in enumerate(values)
    ]


def _result(
    rows,
    unit="kWh",
    *,
    kind=DataKind.METERED,
    shape="interval",
    site_id="site-a",
    asset_id=None,
    resolution="1h",
):
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="fixture",
        site_id=site_id,
        asset_id=asset_id,
        quantity_shape=shape,
        resolution=resolution,
    )


def _weather(values, *, start=datetime(2026, 1, 1, tzinfo=UTC), kind=DataKind.METERED):
    return EnergyResult(
        data=[
            {"timestamp": (start + timedelta(hours=index)).isoformat(), "value": value}
            for index, value in enumerate(values)
        ],
        kind=kind,
        unit="°C",
        source="fixture-weather",
        site_id="site-a",
    )


def test_reports_coincident_equipment_increase_as_bounded_noncausal_evidence():
    consumption = _result(_rows([1, 1, 1, 1, 5]))
    equipment = _result(_rows([0.2, 0.2, 0.2, 0.2, 4.2]), asset_id="oven-1")

    result = explain(
        ("site-meter", consumption),
        {},
        equipment=[("oven-meter", equipment)],
    )

    spike = result.data["spikes"][0]
    evidence = spike["supported_explanations"][0]
    assert spike["load_kwh"] == 5
    assert spike["baseline_kwh"] == 1
    assert spike["excess_kwh"] == 4
    assert evidence["asset_id"] == "oven-1"
    assert evidence["increase_kwh"] == pytest.approx(4)
    assert evidence["overlap_with_site_excess_kwh"] == pytest.approx(4)
    assert evidence["share_of_site_excess_upper_bound"] == pytest.approx(1)
    assert "does not establish cause" in evidence["interpretation"]
    assert result.provenance[0]["inputs"][1]["artifact_id"] == "oven-meter"
    assert result.data["intervals"][-1]["is_spike"] is True


@pytest.mark.parametrize(
    "temperatures", [[12] * 13, [10, 14, 12, 16, 11, 15, 13, 10, 16, 12, 14, 11, 30]]
)
def test_constant_or_unrelated_temperature_has_no_weather_explanation(temperatures):
    load_values = [1 + index * 0.03 for index in range(12)] + [5]
    result = explain(
        ("site-meter", _result(_rows(load_values))),
        {},
        weather=("weather", _weather(temperatures)),
    )

    spike = result.data["spikes"][0]
    assert all(
        item["type"] != "observed_weather_association" for item in spike["supported_explanations"]
    )
    assert any(
        "correlation" in reason or "vary enough" in reason for reason in spike["missing_evidence"]
    )


def test_weather_association_needs_history_and_is_reported_without_causal_claim():
    temperatures = list(range(10, 22)) + [30]
    load_values = [1 + index * 0.1 for index in range(12)] + [5]
    result = explain(
        ("site-meter", _result(_rows(load_values))),
        {},
        weather=("weather", _weather(temperatures)),
    )

    spike = result.data["spikes"][0]
    evidence = next(
        item
        for item in spike["supported_explanations"]
        if item["type"] == "observed_weather_association"
    )
    assert evidence["non_spike_observations"] == 8
    assert evidence["temperature_load_correlation"] == pytest.approx(1)
    assert "does not establish cause" in evidence["interpretation"]


def test_missing_context_is_visible_and_forecast_is_not_observed_evidence():
    result = explain(("site-meter", _result(_rows([1, 1, 1, 1, 5]))), {})
    spike = result.data["spikes"][0]
    assert spike["supported_explanations"] == []
    assert any("No equipment" in item for item in spike["missing_evidence"])
    assert any("No weather" in item for item in spike["missing_evidence"])

    forecast = explain(
        ("site-meter", _result(_rows([1, 1, 1, 1, 5]))),
        {},
        weather=("forecast", _weather([10, 10, 10, 10, 30], kind=DataKind.FORECAST)),
    )
    forecast_spike = forecast.data["spikes"][0]
    assert forecast_spike["supported_explanations"] == []
    assert any("forecast" in item for item in forecast_spike["missing_evidence"])


def test_converts_load_units_and_matches_equipment_on_exact_utc_intervals():
    load = _result(_rows([1000, 1000, 1000, 1000, 5000]), "Wh")
    equipment = _result(_rows([200, 200, 200, 200, 4200]), "Wh", asset_id="heater")

    result = explain(("load", load), {}, equipment=[("heater", equipment)])

    assert result.data["spikes"][0]["excess_kwh"] == pytest.approx(4)
    assert result.data["spikes"][0]["supported_explanations"][0]["increase_kwh"] == pytest.approx(4)

    utc_equivalent = _result(
        _rows(
            [0.2, 0.2, 0.2, 0.2, 4.2],
            start=datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        ),
        asset_id="heater",
    )
    assert explain(
        ("load", _result(_rows([1, 1, 1, 1, 5]))), {}, equipment=[("heater", utc_equivalent)]
    )


@pytest.mark.parametrize(
    ("result", "parameters", "message"),
    [
        (_result(_rows([1, 1, 1, 1, 5]), kind=DataKind.FORECAST), {}, "metered or calculated"),
        (_result(_rows([1, 1, 1, 1, 5]), shape="counter"), {}, "interval energy"),
        (_result(_rows([1, 1, 1, 1, 5]), "kW"), {}, "Wh, kWh or MWh"),
        (_result(_rows([1, 1, 1, 1, 5])), {"window": 0}, "Window must be an integer"),
        (_result(_rows([1, 1, 1, 1, 5])), {"spike_ratio": 1}, "spike_ratio must be greater than"),
    ],
)
def test_rejects_incompatible_kinds_shapes_units_and_thresholds(result, parameters, message):
    with pytest.raises(EnergyError, match=message):
        explain(("load", result), parameters)


def test_requires_explicit_finite_load_intervals_and_matching_equipment_horizon():
    without_ends = _result(
        [{"timestamp": row["timestamp"], "value": row["value"]} for row in _rows([1, 1, 1, 1, 5])]
    )
    with pytest.raises(EnergyError, match="explicit end"):
        explain(("load", without_ends), {})

    with pytest.raises(EnergyError, match="finite numbers or null"):
        explain(("load", _result(_rows([1, 1, 1, 1, float("inf")]))), {})

    load = _result(_rows([1, 1, 1, 1, 5]))
    equipment_rows = _rows([0.2, 0.2, 0.2, 0.2, 4.2])
    equipment_rows[-1]["end"] = "2026-01-01T05:30:00+00:00"
    with pytest.raises(EnergyError, match="exact consumption starts, ends and horizon"):
        explain(("load", load), {}, equipment=[("equipment", _result(equipment_rows))])


def test_rejects_overlapping_equipment_totals_above_site_load():
    load = _result(_rows([1, 1, 1, 1, 5]))
    first = _result(_rows([0.7, 0.7, 0.7, 0.7, 3]))
    second = _result(_rows([0.7, 0.7, 0.7, 0.7, 3]))

    with pytest.raises(EnergyError, match="overlapping submeters"):
        explain(("load", load), {}, equipment=[("first", first), ("second", second)])


def test_weather_uses_exact_instants_across_offset_spellings_and_no_nearest_match():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    load_values = [1 + index * 0.1 for index in range(12)] + [5]
    temperatures = list(range(10, 22)) + [30]
    weather = _weather(temperatures, start=start)
    weather.data[0]["timestamp"] = "2026-01-01T01:00:00+01:00"
    result = explain(("load", _result(_rows(load_values))), {}, weather=("weather", weather))
    assert result.data["summary"]["weather_non_spike_observations"] == 8

    weather.data[-1]["timestamp"] = "2026-01-01T12:59:00+00:00"
    unmatched = explain(("load", _result(_rows(load_values))), {}, weather=("weather", weather))
    assert not any(
        item["type"] == "observed_weather_association"
        for item in unmatched.data["spikes"][0]["supported_explanations"]
    )


def test_exact_ratio_threshold_survives_decimal_float_rounding():
    result = explain(("meter", _result(_rows([1.2, 0.8, 1.6, 2.4]))), {"window": 2})
    assert result.data["summary"]["spike_count"] == 1
    assert result.data["spikes"][0]["load_to_baseline_ratio"] == pytest.approx(2)


def test_equipment_can_declare_an_independent_endpoint_column():
    load = _result(_rows([1, 1, 1, 1, 5]))
    for row in load.data:
        row["to"] = row.pop("end")
    equipment = _result(_rows([0.2, 0.2, 0.2, 0.2, 4.2]), asset_id="oven")
    result = explain(
        ("meter", load),
        {"end_column": "to", "equipment_end_column": "end"},
        equipment=[("oven", equipment)],
    )
    assert result.data["summary"]["spike_count"] == 1
    assert result.data["spikes"][0]["supported_explanations"][0]["asset_id"] == "oven"
