from __future__ import annotations

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.timeseries import operate


def _result(rows, unit="kWh", *, kind=DataKind.METERED, timezone="UTC", resolution="30min"):
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="fixture",
        timezone=timezone,
        resolution=resolution,
    )


def _rows(values, start="2026-01-01T00:00:00+00:00", step_minutes=30):
    import pandas as pd

    begin = pd.Timestamp(start)
    return [
        {
            "timestamp": (begin + pd.Timedelta(minutes=index * step_minutes)).isoformat(),
            "value": value,
        }
        for index, value in enumerate(values)
    ]


def test_cost_and_carbon_use_exact_interval_starts_and_keep_lineage():
    energy = _result(_rows([1, 2, 0.5]))
    price = _result(_rows([10, 20, 30]), "GBP_pence/kWh", kind=DataKind.FORECAST)
    intensity = _result(_rows([100, 200, 300]), "gCO2e/kWh", kind=DataKind.FORECAST)

    cost = operate("cost", [("energy", energy), ("price", price)], {})
    carbon = operate("carbon", [("energy", energy), ("intensity", intensity)], {})

    assert [row["cost"] for row in cost.data] == [0.1, 0.4, 0.15]
    assert cost.unit == "GBP"
    assert [row["carbon"] for row in carbon.data] == [100.0, 400.0, 150.0]
    assert carbon.unit == "gCO2e"
    assert cost.provenance[0]["operation"] == "cost"
    assert cost.provenance[0]["inputs"][1]["unit"] == "GBP_pence/kWh"


def test_cost_infers_equal_implicit_resolution_without_zip_shape_error():
    energy = _result(_rows([1, 1, 1]), resolution=None)
    price = _result(_rows([10, 20, 30]), "GBP_pence/kWh", resolution=None)

    output = operate("cost", [("energy", energy), ("price", price)], {})

    assert output.resolution == "30min"
    assert [row["cost"] for row in output.data] == [0.1, 0.2, 0.3]


def test_power_integration_converts_kw_to_kwh_and_rejects_non_power():
    power = _result(_rows([2, 2, 4]), "kW")
    integrated = operate("integrate_power", [("power", power)], {"method": "left"})

    assert integrated.unit == "kWh"
    assert [row["energy"] for row in integrated.data] == [1.0, 1.0]
    with pytest.raises(EnergyError, match="Power integration"):
        operate("integrate_power", [("energy", _result(_rows([2, 2])))], {})


def test_power_gap_is_null_and_explicit_intervals_cannot_overlap():
    rows = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "value": 2},
        {"timestamp": "2026-01-01T01:00:00+00:00", "value": 2},
    ]
    result = _result(rows, "kW", resolution="30min")
    output = operate("integrate_power", [("power", result)], {"method": "left"})
    assert output.data[0]["energy"] is None
    assert output.data[0]["status"] == "gap"

    overlap = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "value": 2,
            },
            {
                "timestamp": "2026-01-01T00:30:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "value": 2,
            },
        ],
        "kW",
        resolution=None,
    )
    with pytest.raises(EnergyError, match="overlap"):
        operate("integrate_power", [("power", overlap)], {"method": "left"})


def test_counter_reset_and_missing_values_never_invent_consumption():
    result = _result(_rows([100, 102, None, 10, 12]), "kWh")
    output = operate("counter", [("meter", result)], {})

    assert [row["value"] for row in output.data] == [None, 2.0, None, None, 2.0]
    assert [row["status"] for row in output.data] == ["initial", "ok", "missing", "missing", "ok"]
    assert any("reset" in warning for warning in output.warnings)


def test_counter_gap_is_null_and_requires_expected_resolution():
    rows = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "value": 100},
        {"timestamp": "2026-01-01T00:30:00+00:00", "value": 102},
        {"timestamp": "2026-01-01T01:30:00+00:00", "value": 106},
        {"timestamp": "2026-01-01T02:00:00+00:00", "value": 108},
    ]
    result = _result(rows, resolution="30min")
    output = operate("counter", [("meter", result)], {})

    assert [row["value"] for row in output.data] == [None, 2.0, None, 2.0]
    assert any("gap" in warning for warning in output.warnings)
    with pytest.raises(EnergyError, match="declared resolution"):
        operate("counter", [("meter", _result(rows, resolution=None))], {})


def test_missing_detects_gap_and_preserves_dst_folds():
    rows = [
        {"timestamp": "2026-10-25T00:30:00+01:00", "value": 1},
        {"timestamp": "2026-10-25T01:30:00+00:00", "value": 2},
        {"timestamp": "2026-10-25T02:00:00+00:00", "value": 3},
    ]
    result = _result(rows, timezone="Europe/London")
    output = operate("missing", [("meter", result)], {"frequency": "30min"})

    assert len(output.data) == 6
    assert sum(row["missing"] for row in output.data) == 3
    assert output.data[0]["timestamp"].endswith("+01:00")
    assert output.data[3]["timestamp"].endswith("+00:00")


def test_missing_uses_physical_dst_day_lengths():
    spring = _result(
        [
            {"timestamp": "2026-03-29T00:00:00+00:00", "value": 1},
            {"timestamp": "2026-03-30T00:00:00+01:00", "value": 1},
        ],
        timezone="Europe/London",
    )
    autumn = _result(
        [
            {"timestamp": "2026-10-25T00:00:00+01:00", "value": 1},
            {"timestamp": "2026-10-26T00:00:00+00:00", "value": 1},
        ],
        timezone="Europe/London",
    )

    spring_output = operate("missing", [("spring", spring)], {"frequency": "30min"})
    autumn_output = operate("missing", [("autumn", autumn)], {"frequency": "30min"})

    assert len(spring_output.data) == 47
    assert len(autumn_output.data) == 51


def test_missing_keeps_custom_timestamp_and_value_columns():
    result = _result(
        [
            {"when": "2026-01-01T00:00:00+00:00", "reading": 1},
            {"when": "2026-01-01T01:00:00+00:00", "reading": 3},
        ]
    )
    output = operate(
        "missing",
        [("meter", result)],
        {"timestamp": "when", "column": "reading", "frequency": "30min"},
    )

    assert output.data[1]["when"] == "2026-01-01T00:30:00+00:00"
    assert output.data[1]["reading"] is None


def test_missing_rejects_off_grid_and_huge_ranges_before_expansion():
    off_grid = _result(
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 1},
            {"timestamp": "2026-01-01T00:45:00+00:00", "value": 1},
        ]
    )
    with pytest.raises(EnergyError, match="interval grid"):
        operate("missing", [("meter", off_grid)], {"frequency": "30min"})

    huge = _result(
        [
            {"timestamp": "1900-01-01T00:00:00+00:00", "value": 1},
            {"timestamp": "2100-01-01T00:00:00+00:00", "value": 1},
        ]
    )
    with pytest.raises(EnergyError, match="row limit"):
        operate("missing", [("meter", huge)], {"frequency": "30min"})


def test_filter_normalize_and_baseline_are_bounded_and_lineaged():
    result = _result(_rows([1, 2, 3, 4]), "Wh")
    filtered = operate(
        "filter",
        [("meter", result)],
        {"start": "2026-01-01T00:30:00+00:00", "end": "2026-01-01T01:00:00+00:00"},
    )
    normalized = operate("normalize", [("meter", result)], {"unit": "kWh"})
    baseline = operate("baseline", [("meter", result)], {"window": 2})

    assert [row["value"] for row in filtered.data] == [2.0, 3.0]
    assert [row["value"] for row in normalized.data] == [0.001, 0.002, 0.003, 0.004]
    assert [row["baseline"] for row in baseline.data] == [None, None, 1.5, 2.5]
    assert normalized.provenance[0]["inputs"][0]["unit"] == "Wh"

    ranged = operate("filter", [("meter", result)], {"minimum": 2, "maximum": 3})
    assert [row["value"] for row in ranged.data] == [2.0, 3.0]


def test_alignment_rejects_missing_coverage_duplicate_instants_and_unknown_units():
    left = _result(_rows([1, 2]))
    right = _result(_rows([10]), "GBP_pence/kWh")
    with pytest.raises(EnergyError, match="exact same UTC timestamps"):
        operate("align", [("left", left), ("right", right)], {})

    duplicate = _result(
        [
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 1},
            {"timestamp": "2026-01-01T01:00:00+01:00", "value": 2},
        ]
    )
    with pytest.raises(EnergyError, match="Duplicate UTC"):
        operate("normalize", [("duplicate", duplicate)], {"unit": "kWh"})
    with pytest.raises(EnergyError, match="supported energy unit"):
        operate("normalize", [("left", left)], {"unit": "bananas"})

    with pytest.raises(EnergyError, match="NaT"):
        operate(
            "normalize",
            [("bad", _result([{"timestamp": "NaT", "value": 1}]))],
            {"unit": "kWh"},
        )
    with pytest.raises(EnergyError, match="Boolean"):
        operate(
            "normalize",
            [
                (
                    "bad",
                    _result([{"timestamp": "2026-01-01T00:00:00+00:00", "value": True}]),
                )
            ],
            {"unit": "kWh"},
        )


def test_cost_rejects_mixed_resolution_and_rate_gaps_without_fill():
    energy = _result(_rows([1, 1, 1]), resolution="30min")
    price = _result(_rows([10, 20, 30]), "GBP_pence/kWh", resolution="1h")
    with pytest.raises(EnergyError, match="incompatible resolutions"):
        operate("cost", [("energy", energy), ("price", price)], {})

    price = _result(_rows([10, None, 30]), "GBP_pence/kWh", resolution="30min")
    with pytest.raises(EnergyError, match="missing value"):
        operate("cost", [("energy", energy), ("price", price)], {})


def test_explicit_rate_durations_must_match_and_metadata_is_preserved():
    energy = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:30:00+00:00",
                "value": 1,
            }
        ]
    )
    price = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "value": 10,
            }
        ],
        "GBP_pence/kWh",
    )
    with pytest.raises(EnergyError, match="durations"):
        operate("cost", [("energy", energy), ("price", price)], {})

    energy.provider = "meter"
    energy.site_id = "home"
    energy.asset_id = "main"
    energy.original_unit = "Wh"
    energy.field_units = {"value": "Wh", "temperature": "C"}
    normalized = operate("normalize", [("energy", energy)], {"unit": "kWh"})
    assert normalized.provider == "meter"
    assert normalized.site_id == "home"
    assert normalized.asset_id == "main"
    assert normalized.original_unit == "Wh"
    assert normalized.field_units["value"] == "kWh"
    assert normalized.field_units["temperature"] == "C"


def test_calendar_compare_uses_local_periods_and_null_prior_periods():
    rows = [
        {"timestamp": "2026-03-28T23:30:00+00:00", "value": 1},
        {"timestamp": "2026-03-29T00:30:00+00:00", "value": 2},
        {"timestamp": "2026-03-30T00:30:00+01:00", "value": 3},
    ]
    result = _result(rows, timezone="Europe/London")
    output = operate("compare", [("meter", result)], {"frequency": "daily"})

    assert output.data[0]["period"] == "2026-03-28"
    assert output.data[0]["previous"] is None
    assert output.data[1]["period"] == "2026-03-29"
    assert output.data[1]["previous"] == 1
    assert output.data[2]["period"] == "2026-03-30"
    assert output.data[2]["previous"] == 2
