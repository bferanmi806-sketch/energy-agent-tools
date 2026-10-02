from __future__ import annotations

from decimal import Decimal

import pytest

from energy_agent_tools.billing import calculate_bill
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.timeseries import operate


def _cost_result(rows, *, unit="GBP", kind=DataKind.CALCULATED, timezone="UTC", provenance=None):
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="timeseries",
        timezone=timezone,
        resolution="1D",
        provenance=provenance or [{"operation": "cost", "inputs": [{"source": "caller"}]}],
    )


def _rows(*costs, starts=None, ends=None):
    if starts is None:
        starts = [f"2026-01-{day:02}T00:00:00+00:00" for day in range(1, len(costs) + 1)]
    if ends is None:
        ends = [f"2026-01-{day:02}T00:00:00+00:00" for day in range(2, len(costs) + 2)]
    return [
        {"timestamp": start, "end": end, "cost": cost}
        for start, end, cost in zip(starts, ends, costs, strict=True)
    ]


def _parameters(
    *, start="2026-01-01T00:00:00+00:00", finish="2026-01-03T00:00:00+00:00", **overrides
):
    return {
        "start": start,
        "finish": finish,
        "timezone": "UTC",
        "standing_charge": {"amount_per_day": 0.5, "currency": "GBP", "taxable": True},
        "tax": {"rate": 0.05, "energy_taxable": True},
        "source": "fixture tariff schedule",
        **overrides,
    }


def _bill(rows=None, *, result=None, parameters=None, artifact_id="cost-artifact"):
    input_result = result or _cost_result(rows if rows is not None else _rows(1, 1))
    return calculate_bill(
        [(artifact_id, input_result)],
        parameters if parameters is not None else _parameters(),
    )


def test_calculates_exact_energy_standing_charge_and_tax_components():
    energy = EnergyResult(
        data=[
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 5},
            {"timestamp": "2026-01-02T00:00:00+00:00", "value": 5},
        ],
        kind=DataKind.METERED,
        unit="kWh",
        source="meter",
        timezone="UTC",
        resolution="1D",
    )
    price = EnergyResult(
        data=[
            {"timestamp": "2026-01-01T00:00:00+00:00", "value": 0.2},
            {"timestamp": "2026-01-02T00:00:00+00:00", "value": 0.2},
        ],
        kind=DataKind.FORECAST,
        unit="GBP/kWh",
        source="caller tariff",
        timezone="UTC",
        resolution="1D",
    )
    cost = operate("cost", [("meter", energy), ("tariff", price)], {})
    for row, interval_end in zip(
        cost.data,
        ["2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"],
        strict=True,
    ):
        row["end"] = interval_end
    result = calculate_bill([("cost-artifact", cost)], _parameters())

    assert result.data == {
        "energy_cost": Decimal("2"),
        "standing_charge": Decimal("1.0"),
        "chargeable_days": 2,
        "taxable_subtotal": Decimal("3.0"),
        "tax": Decimal("0.150"),
        "total": Decimal("3.150"),
        "currency": "GBP",
        "window": {
            "start": "2026-01-01T00:00:00+00:00",
            "finish": "2026-01-03T00:00:00+00:00",
            "timezone": "UTC",
        },
    }
    assert result.kind == DataKind.CALCULATED
    assert result.unit == "GBP"
    assert result.provenance[0]["inputs"][0]["artifact_id"] == "cost-artifact"
    assert result.provenance[0]["inputs"][0]["source_kind"] == "calculated"
    assert result.provenance[0]["inputs"][0]["provenance"] == cost.provenance
    assert result.provenance[0]["tariff_schedule"]["source"] == "fixture tariff schedule"
    assert "No statutory tariff or tax rate was inferred." in result.assumptions
    assert result.model_dump(mode="json")["data"]["total"] == "3.150"


def test_dst_calendar_day_counts_once_across_a_23_hour_window():
    rows = _rows(
        2,
        starts=["2026-03-29T00:00:00+00:00"],
        ends=["2026-03-30T00:00:00+01:00"],
    )
    result = _bill(
        rows,
        result=_cost_result(rows, timezone="Europe/London"),
        parameters=_parameters(
            start="2026-03-29T00:00:00+00:00",
            finish="2026-03-30T00:00:00+01:00",
            timezone="Europe/London",
        ),
    )

    assert result.data["chargeable_days"] == 1
    assert result.data["standing_charge"] == Decimal("0.5")
    assert result.timezone == "Europe/London"
    assert result.data["window"]["timezone"] == "Europe/London"


def test_rejects_currency_mismatch_and_non_cost_currencies():
    rows = _rows(1, 1)
    with pytest.raises(EnergyError, match="same currency"):
        _bill(
            rows,
            parameters=_parameters(
                standing_charge={"amount_per_day": 0.5, "currency": "USD", "taxable": True}
            ),
        )
    with pytest.raises(EnergyError, match="GBP, USD or EUR"):
        _bill(rows, result=_cost_result(rows, unit="CAD"))


@pytest.mark.parametrize(
    "rows",
    [
        _rows(1, 1)[1:],
        _rows(1, 1)[:1],
        [*_rows(1, 1), _rows(1, 1)[0]],
        [dict(_rows(1, 1)[0], cost=None), _rows(1, 1)[1]],
        _rows(
            1,
            1,
            starts=["2025-12-31T23:30:00+00:00", "2026-01-02T00:00:00+00:00"],
            ends=["2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00"],
        ),
    ],
    ids=["gap-at-start", "gap-at-finish", "duplicate-overlap", "null-cost", "outside-window"],
)
def test_rejects_incomplete_overlapping_null_or_outside_intervals(rows):
    with pytest.raises(EnergyError):
        _bill(rows)


def test_rejects_partial_day_and_bounds_that_are_not_aware():
    rows = _rows(1)
    with pytest.raises(EnergyError, match="local midnight"):
        _bill(
            rows,
            parameters=_parameters(
                start="2026-01-01T00:30:00+00:00",
                finish="2026-01-02T00:30:00+00:00",
            ),
        )
    with pytest.raises(EnergyError, match="explicit UTC offset"):
        _bill(rows, parameters=_parameters(start="2026-01-01T00:00:00"))


@pytest.mark.parametrize("kind", [DataKind.METERED, DataKind.ESTIMATED, DataKind.SIMULATED])
def test_rejects_non_calculated_cost_input_kinds(kind):
    rows = _rows(1, 1)
    with pytest.raises(EnergyError, match="calculated interval-cost"):
        _bill(rows, result=_cost_result(rows, kind=kind))


def test_rejects_input_without_cost_operation_provenance():
    rows = _rows(1, 1)
    with pytest.raises(EnergyError, match="timeseries cost operation"):
        _bill(rows, result=_cost_result(rows, provenance=[{"operation": "normalize"}]))


def test_negative_energy_cost_is_preserved_without_a_zero_floor():
    rows = _rows(-2, 0)
    result = _bill(
        rows,
        parameters=_parameters(
            standing_charge={"amount_per_day": 0, "currency": "GBP", "taxable": False},
            tax={"rate": 0, "energy_taxable": False},
        ),
    )

    assert result.data["energy_cost"] == Decimal("-2")
    assert result.data["standing_charge"] == Decimal("0")
    assert result.data["tax"] == Decimal("0")
    assert result.data["total"] == Decimal("-2")


@pytest.mark.parametrize(
    "override",
    [
        {"unknown": "value"},
        {
            "standing_charge": {
                "amount_per_day": 0.5,
                "currency": "GBP",
                "taxable": True,
                "extra": 1,
            }
        },
        {"tax": {"rate": 0.05, "energy_taxable": True, "extra": 1}},
        {"source": "  "},
        {"timezone": "Mars/Olympus"},
    ],
    ids=[
        "unknown-param",
        "unknown-standing-field",
        "unknown-tax-field",
        "blank-source",
        "bad-timezone",
    ],
)
def test_rejects_open_or_malformed_parameter_objects(override):
    with pytest.raises(EnergyError):
        _bill(_rows(1, 1), parameters=_parameters(**override))


@pytest.mark.parametrize("amount", [-0.01, True, "0.50", float("inf"), float("nan")])
def test_rejects_invalid_standing_charge_numbers(amount):
    with pytest.raises(EnergyError):
        _bill(
            _rows(1, 1),
            parameters=_parameters(
                standing_charge={"amount_per_day": amount, "currency": "GBP", "taxable": True}
            ),
        )


@pytest.mark.parametrize("rate", [-0.01, 1.01, True, "0.05", float("nan")])
def test_rejects_invalid_tax_rates(rate):
    with pytest.raises(EnergyError):
        _bill(_rows(1, 1), parameters=_parameters(tax={"rate": rate, "energy_taxable": True}))


def test_rejects_boolean_costs_and_non_boolean_taxability_flags():
    rows = _rows(1, 1)
    rows[0]["cost"] = True
    with pytest.raises(EnergyError, match="boolean"):
        _bill(rows)
    with pytest.raises(EnergyError, match="must be a boolean"):
        _bill(
            _rows(1, 1),
            parameters=_parameters(
                tax={"rate": 0.05, "energy_taxable": 1},
            ),
        )


def test_rejects_missing_interval_end_and_duplicate_timestamps():
    rows = _rows(1, 1)
    del rows[0]["end"]
    with pytest.raises(EnergyError, match="interval-end"):
        _bill(rows)

    rows = _rows(1, 1)
    rows[1]["timestamp"] = rows[0]["timestamp"]
    with pytest.raises(EnergyError, match="overlap or duplicate"):
        _bill(rows)


def test_supports_custom_interval_columns():
    rows = [
        {"from": "2026-01-01T00:00:00+00:00", "until": "2026-01-03T00:00:00+00:00", "charge": 2}
    ]
    result = _bill(
        rows,
        result=_cost_result(rows),
        parameters=_parameters(column="charge", timestamp="from", end="until"),
    )

    assert result.data["energy_cost"] == Decimal("2")


def test_requires_explicit_zero_components_instead_of_inferred_defaults():
    parameters = _parameters()
    del parameters["tax"]
    with pytest.raises(EnergyError, match="are required"):
        _bill(_rows(1, 1), parameters=parameters)
