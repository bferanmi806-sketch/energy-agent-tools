from __future__ import annotations

import pytest

from energy_agent_tools.grid_analysis import analyse
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult


def _result(
    rows,
    unit,
    *,
    kind=DataKind.METERED,
    quantity_shape=None,
    source="fixture",
    field_units=None,
):
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source=source,
        resolution="30min",
        quantity_shape=quantity_shape,
        field_units=field_units or {},
    )


def _starts(values, column="value", timestamp_column="timestamp"):
    return [
        {timestamp_column: f"2026-01-01T0{index}:00:00+00:00", column: value}
        for index, value in enumerate(values)
    ]


def test_analysis_aligns_exact_starts_and_summarizes_supplied_horizon():
    generation = _result(
        _starts(["31000", "32500", "31800"]),
        "MW",
        kind=DataKind.FORECAST,
        quantity_shape="instantaneous",
        source="national_generation_forecast",
    )
    carbon = _result(
        _starts(["250", "100", "40"], timestamp_column="from"),
        "gCO2/kWh",
        kind=DataKind.METERED,
        source="grid_intensity",
    )

    result = analyse(("generation-7", generation), ("carbon-9", carbon), {})

    assert result.kind == DataKind.CALCULATED
    assert result.unit == "mixed"
    assert result.field_units == {
        "generation_mw": "MW",
        "carbon_intensity_g_per_kwh": "gCO2/kWh",
    }
    assert len(result.data["intervals"]) == 3
    summary = result.data["summary"]
    assert summary["generation_mw"] == {
        "min": 31000.0,
        "max": 32500.0,
        "mean": pytest.approx(31766.666666666668),
    }
    assert summary["carbon_intensity_g_per_kwh"] == {
        "min": 40.0,
        "max": 250.0,
        "mean": 130.0,
    }
    assert summary["peak_generation_interval"]["timestamp"].startswith("2026-01-01T01:00")
    assert summary["lowest_carbon_interval"]["timestamp"].startswith("2026-01-01T02:00")
    assert [stamp[11:16] for stamp in summary["lower_carbon_intervals"]] == [
        "02:00",
        "01:00",
        "00:00",
    ]
    assert result.data["coverage"]["horizon_claimed"] is False
    assert result.time_end is None
    assert all("end" not in row for row in result.data["intervals"])
    assert not any("emissions" in key.lower() for row in result.data["intervals"] for key in row)
    assert result.provenance[0]["inputs"][0]["artifact_id"] == "generation-7"
    assert result.provenance[0]["inputs"][0]["kind"] == "forecast"
    assert result.provenance[0]["inputs"][1]["source"] == "grid_intensity"


def test_fuel_rows_are_grouped_and_share_requires_explicit_classification():
    generation_rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": "60000", "fuelType": "gas"},
        {"timestamp": "2026-01-01T00:00:00Z", "value": "40000", "fuelType": "wind"},
        {"timestamp": "2026-01-01T01:00:00Z", "value": "50000", "fuelType": "gas"},
        {"timestamp": "2026-01-01T01:00:00Z", "value": "50000", "fuelType": "wind"},
    ]
    generation = _result(generation_rows, "kW", quantity_shape="instantaneous")
    carbon = _result(
        _starts([100, 80], timestamp_column="from"),
        "kgCO2e/MWh",
    )

    classified = analyse(
        ("g", generation),
        ("c", carbon),
        {"fuel_column": "fuelType", "low_carbon_fuels": ["wind"]},
    )

    intervals = classified.data["intervals"]
    assert intervals[0]["generation_mw"] == 100.0
    assert intervals[0]["generation_by_fuel_mw"] == {"gas": 60.0, "wind": 40.0}
    assert intervals[0]["low_carbon_generation_fraction"] == pytest.approx(0.4)
    assert intervals[1]["low_carbon_generation_fraction"] == pytest.approx(0.5)
    assert intervals[0]["carbon_intensity_g_per_kwh"] == 100.0
    assert classified.field_units["carbon_intensity_g_per_kwh"] == "gCO2e/kWh"

    without_classification = analyse(("g", generation), ("c", carbon), {"fuel_column": "fuelType"})
    assert "low_carbon_generation_fraction" not in without_classification.data["intervals"][0]
    with pytest.raises(EnergyError, match="requires an explicit fuel_column"):
        analyse(("g", generation), ("c", carbon), {"low_carbon_fuels": ["wind"]})
    with pytest.raises(EnergyError, match="nonempty list"):
        analyse(
            ("g", generation),
            ("c", carbon),
            {"fuel_column": "fuelType", "low_carbon_fuels": None},
        )


def test_matching_explicit_endpoints_are_reported_without_integration():
    generation_rows = [
        {
            "timestamp": f"2026-01-01T0{index}:00:00Z",
            "end": f"2026-01-01T0{index + 1}:00:00Z",
            "value": value,
        }
        for index, value in enumerate([10, 20])
    ]
    carbon_rows = [
        {
            "from": f"2026-01-01T0{index}:00:00Z",
            "to": f"2026-01-01T0{index + 1}:00:00Z",
            "value": value,
        }
        for index, value in enumerate([200, 100])
    ]
    result = analyse(
        ("g", _result(generation_rows, "GW", quantity_shape="instantaneous")),
        ("c", _result(carbon_rows, "gCO2e/kWh")),
        {},
    )

    assert result.data["coverage"]["horizon_claimed"] is True
    assert result.data["intervals"][0]["end"] == "2026-01-01T01:00:00+00:00"
    assert result.data["intervals"][0]["generation_mw"] == 10000.0
    assert result.time_end.isoformat() == "2026-01-01T02:00:00+00:00"
    assert "emissions" not in result.data["intervals"][0]


@pytest.mark.parametrize(
    ("generation_rows", "carbon_rows", "generation_unit", "carbon_unit", "shape", "message"),
    [
        (
            _starts([1, 2]),
            [
                {"from": "2026-01-01T00:00:00Z", "value": 1},
                {"from": "2026-01-01T02:00:00Z", "value": 2},
            ],
            "MW",
            "gCO2/kWh",
            None,
            "exactly the same UTC start timestamps",
        ),
        (
            [
                {"timestamp": "2026-01-01T00:00:00Z", "value": 1},
                {"timestamp": "2026-01-01T00:00:00+00:00", "value": 1},
            ],
            _starts([1, 1], timestamp_column="from"),
            "MW",
            "gCO2/kWh",
            None,
            "Duplicate generation timestamp",
        ),
        (
            _starts([1, 1]),
            [
                {"from": "2026-01-01T00:00:00Z", "value": 1},
                {"from": "2026-01-01T00:00:00+00:00", "value": 2},
            ],
            "MW",
            "gCO2/kWh",
            None,
            "Duplicate carbon UTC timestamps",
        ),
        (
            _starts([1, 2]),
            _starts([1, 2], timestamp_column="from"),
            "kWh",
            "gCO2/kWh",
            None,
            "must use MW, kW, or GW",
        ),
        (
            _starts([1, 2]),
            _starts([1, 2], timestamp_column="from"),
            "MW",
            "gCO2/kWh",
            "counter",
            "(?i)cumulative generation counters",
        ),
        (
            _starts([1, 2]),
            _starts([1, 2], timestamp_column="from"),
            "MW",
            "gCO2/kWh",
            "interval",
            "instantaneous or have unknown quantity shape",
        ),
    ],
)
def test_rejects_unaligned_duplicate_or_semantically_incompatible_inputs(
    generation_rows, carbon_rows, generation_unit, carbon_unit, shape, message
):
    with pytest.raises(EnergyError, match=message):
        analyse(
            ("g", _result(generation_rows, generation_unit, quantity_shape=shape)),
            ("c", _result(carbon_rows, carbon_unit)),
            {},
        )


def test_rejects_endpoint_mismatch_incomplete_fuel_sets_and_nonfinite_values():
    generation_with_end = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "value": 5}],
        "MW",
        quantity_shape="instantaneous",
    )
    carbon_without_end = _result([{"from": "2026-01-01T00:00:00Z", "value": 100}], "gCO2/kWh")
    with pytest.raises(EnergyError, match="require matching explicit carbon endpoints"):
        analyse(("g", generation_with_end), ("c", carbon_without_end), {})

    carbon_with_different_end = _result(
        [
            {
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-01-01T00:30:00Z",
                "value": 100,
            }
        ],
        "gCO2/kWh",
    )
    with pytest.raises(EnergyError, match="must match exactly in UTC"):
        analyse(("g", generation_with_end), ("c", carbon_with_different_end), {})

    fuel_rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": 5, "fuel": "gas"},
        {"timestamp": "2026-01-01T01:00:00Z", "value": 5, "fuel": "wind"},
    ]
    with pytest.raises(EnergyError, match="same explicit fuel set"):
        analyse(
            ("g", _result(fuel_rows, "MW")),
            ("c", _result(_starts([100, 100], timestamp_column="from"), "gCO2/kWh")),
            {"fuel_column": "fuel"},
        )

    repeated_fuel = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": 5, "fuel": "gas"},
        {"timestamp": "2026-01-01T00:00:00+00:00", "value": 6, "fuel": "GAS"},
    ]
    with pytest.raises(EnergyError, match="Duplicate generation timestamp/fuel"):
        analyse(
            ("g", _result(repeated_fuel, "MW")),
            ("c", _result(_starts([100], timestamp_column="from"), "gCO2/kWh")),
            {"fuel_column": "fuel"},
        )

    nonfinite_generation = _result(_starts([float("inf")]), "MW")
    with pytest.raises(EnergyError, match="finite"):
        analyse(
            ("g", nonfinite_generation),
            ("c", _result(_starts([100], timestamp_column="from"), "gCO2/kWh")),
            {},
        )


def test_carbon_threshold_uses_canonical_species_unit_and_forecast_lineage():
    generation = _result(_starts([1, 2]), "GW", kind=DataKind.FORECAST)
    carbon = _result(
        _starts(["0.3", "0.1"], timestamp_column="from"),
        "kgCO2e/kWh",
        kind=DataKind.FORECAST,
    )
    result = analyse(
        ("g", generation),
        ("c", carbon),
        {"carbon_threshold_g_per_kwh": "150"},
    )

    assert result.data["summary"]["intervals_at_or_below_carbon_threshold"] == [
        "2026-01-01T01:00:00+00:00"
    ]
    assert result.data["summary"]["carbon_intensity_g_per_kwh"]["mean"] == 200.0
    assert result.field_units["carbon_threshold_g_per_kwh"] == "gCO2e/kWh"
    assert result.provenance[0]["inputs"][0]["kind"] == "forecast"
