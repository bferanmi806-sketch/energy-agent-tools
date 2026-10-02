from __future__ import annotations

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.solar_balance import reconcile


def _result(
    values,
    unit="kWh",
    *,
    kind=DataKind.METERED,
    shape="interval",
    starts=None,
    ends=None,
):
    rows = []
    for index, value in enumerate(values):
        hour = index if starts is None else starts[index]
        end_hour = hour + 1 if ends is None else ends[index]
        rows.append(
            {
                "timestamp": f"2026-01-01T{hour:02d}:00:00+00:00",
                "end": f"2026-01-01T{end_hour:02d}:00:00+00:00",
                "value": value,
            }
        )
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="fixture",
        resolution="1h",
        quantity_shape=shape,
    )


def _inputs(load, generation):
    return [("load-artifact", load), ("solar-artifact", generation)]


def _params(**overrides):
    return {"consumption_basis": "total_load", "storage_mode": "none", **overrides}


def test_reconciles_interval_energy_and_preserves_lineage_and_assumptions():
    load = _result([0.5, 0.75, 0.4])
    generation = _result([0.1, 0.35, 0.5], kind=DataKind.FORECAST)

    result = reconcile(_inputs(load, generation), _params())

    assert result.kind == DataKind.CALCULATED
    assert result.unit == "kWh"
    assert result.quantity_shape == "interval"
    expected_intervals = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T01:00:00+00:00",
            "load_kwh": 0.5,
            "generation_kwh": 0.1,
            "self_consumption_kwh": 0.1,
            "estimated_import_kwh": 0.4,
            "estimated_export_kwh": 0.0,
        },
        {
            "timestamp": "2026-01-01T01:00:00+00:00",
            "end": "2026-01-01T02:00:00+00:00",
            "load_kwh": 0.75,
            "generation_kwh": 0.35,
            "self_consumption_kwh": 0.35,
            "estimated_import_kwh": 0.4,
            "estimated_export_kwh": 0.0,
        },
        {
            "timestamp": "2026-01-01T02:00:00+00:00",
            "end": "2026-01-01T03:00:00+00:00",
            "load_kwh": 0.4,
            "generation_kwh": 0.5,
            "self_consumption_kwh": 0.4,
            "estimated_import_kwh": 0.0,
            "estimated_export_kwh": 0.1,
        },
    ]
    assert [
        (row["timestamp"], row["end"]) for row in result.data["intervals"]
    ] == [(row["timestamp"], row["end"]) for row in expected_intervals]
    for actual, expected in zip(
        result.data["intervals"], expected_intervals, strict=True
    ):
        for field in (
            "load_kwh",
            "generation_kwh",
            "self_consumption_kwh",
            "estimated_import_kwh",
            "estimated_export_kwh",
        ):
            assert actual[field] == pytest.approx(expected[field])
    assert result.data["summary"] == pytest.approx(
        {
            "load_kwh": 1.65,
            "generation_kwh": 0.95,
            "self_consumption_kwh": 0.85,
            "estimated_import_kwh": 0.8,
            "estimated_export_kwh": 0.1,
            "self_consumption_fraction": 0.85 / 0.95,
            "solar_load_fraction": 0.85 / 1.65,
        }
    )
    lineage_inputs = result.provenance[0]["inputs"]
    assert [item["artifact_id"] for item in lineage_inputs] == [
        "load-artifact",
        "solar-artifact",
    ]
    assert [item["kind"] for item in lineage_inputs] == ["metered", "forecast"]
    assert [item["unit"] for item in lineage_inputs] == ["kWh", "kWh"]
    assert result.field_units["estimated_import_kwh"] == "kWh"
    assert result.field_units["self_consumption_fraction"] == "1"
    assert any("no battery" in assumption for assumption in result.assumptions)
    assert any("losses are modeled" in assumption for assumption in result.assumptions)
    assert any("cannot recover opposing import and export flows" in warning for warning in result.warnings)
    assert any("not grid-meter readings" in warning for warning in result.warnings)


def test_converts_energy_units_to_kwh_before_netting():
    load = _result([0.001], "MWh")
    generation = _result([500], "Wh")

    summary = reconcile(_inputs(load, generation), _params()).data["summary"]

    assert summary["load_kwh"] == pytest.approx(1)
    assert summary["generation_kwh"] == pytest.approx(0.5)
    assert summary["self_consumption_kwh"] == pytest.approx(0.5)
    assert summary["estimated_import_kwh"] == pytest.approx(0.5)


def test_zero_totals_have_undefined_fractions():
    result = reconcile(_inputs(_result([0]), _result([0])), _params()).data["summary"]

    assert result["self_consumption_fraction"] is None
    assert result["solar_load_fraction"] is None


def test_requires_explicit_total_load_and_no_storage_declarations():
    inputs = _inputs(_result([1]), _result([1]))
    with pytest.raises(EnergyError, match="Declare consumption_basis"):
        reconcile(inputs, {"storage_mode": "none"})
    with pytest.raises(EnergyError, match="grid import is not total load"):
        reconcile(inputs, _params(consumption_basis="grid_import"))
    with pytest.raises(EnergyError, match="Declare storage_mode"):
        reconcile(inputs, {"consumption_basis": "total_load"})
    with pytest.raises(EnergyError, match="requires storage_mode='none'"):
        reconcile(inputs, _params(storage_mode="battery"))


def test_requires_explicit_ends_and_matching_nonoverlapping_intervals():
    no_ends = EnergyResult(
        data=[{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1}],
        kind=DataKind.METERED,
        unit="kWh",
        source="fixture",
        quantity_shape="interval",
    )
    with pytest.raises(EnergyError, match="explicit end"):
        reconcile(_inputs(no_ends, no_ends), _params())

    left_overlap = _result([1, 1], ends=[2, 2])
    right_overlap = _result([1, 1], ends=[2, 2])
    with pytest.raises(EnergyError, match="overlap"):
        reconcile(_inputs(left_overlap, right_overlap), _params())

    left = _result([1], ends=[1])
    right = _result([1], ends=[2])
    with pytest.raises(EnergyError, match="matching explicit interval durations"):
        reconcile(_inputs(left, right), _params())


def test_requested_horizon_requires_full_contiguous_coverage():
    load = _result([1, 1], ends=[1, 2])
    generation = _result([1, 1], ends=[1, 2])
    params = _params(start="2026-01-01T00:00:00+00:00", finish="2026-01-01T03:00:00+00:00")
    with pytest.raises(EnergyError, match="cover the requested horizon"):
        reconcile(_inputs(load, generation), params)

    load = _result([1, 1], starts=[0, 2], ends=[1, 3])
    generation = _result([1, 1], starts=[0, 2], ends=[1, 3])
    params["finish"] = "2026-01-01T03:00:00+00:00"
    with pytest.raises(EnergyError, match="contiguous"):
        reconcile(_inputs(load, generation), params)


@pytest.mark.parametrize("shape", [None, "counter", "instantaneous"])
def test_rejects_unknown_counter_and_instantaneous_quantity_shapes(shape):
    load = _result([1], shape=shape)
    with pytest.raises(EnergyError, match="interval energy"):
        reconcile(_inputs(load, _result([1])), _params())


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (-1, "nonnegative"),
        ("not numeric", "not numeric"),
        (None, "missing"),
        (True, "Boolean"),
    ],
)
def test_rejects_negative_nonnumeric_and_missing_values(value, message):
    with pytest.raises(EnergyError, match=message):
        reconcile(_inputs(_result([value]), _result([1])), _params())


def test_rejects_power_units():
    with pytest.raises(EnergyError, match="energy values"):
        reconcile(_inputs(_result([1], "kW"), _result([1])), _params())


def test_rejects_totals_that_overflow_even_when_rows_are_finite():
    with pytest.raises(EnergyError, match="totals must remain finite"):
        reconcile(_inputs(_result([1e308, 1e308]), _result([0, 0])), _params())
