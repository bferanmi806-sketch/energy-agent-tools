from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from energy_agent_tools.forecast_billing import estimate
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult


def _forecast(rows, *, unit="kWh", kind=DataKind.FORECAST, shape="interval", timezone="UTC"):
    return EnergyResult(
        data={
            "intervals": rows,
            "summary": {"interval_count": len(rows)},
            "model": {"name": "fixture model", "historical_lineage": {"artifact_id": "history-1"}},
        },
        kind=kind,
        unit=unit,
        source="fixture forecast",
        timezone=timezone,
        quantity_shape=shape,
        resolution="30min",
        provenance=[{"operation": "forecast", "inputs": [{"artifact_id": "history-1"}]}],
    )


def _tariff(rows, *, unit="GBP/kWh", kind=DataKind.FORECAST, timezone="UTC"):
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="fixture tariff",
        timezone=timezone,
        quantity_shape="interval",
        provenance=[{"operation": "tariff_import", "source": "fixture"}],
    )


def _row(start, end, value=1, lower=None, upper=None):
    return {
        "timestamp": start.isoformat(),
        "end": end.isoformat(),
        "value": value,
        "lower": value if lower is None else lower,
        "upper": value if upper is None else upper,
    }


def _interval(start, end, value):
    return {"from": start.isoformat(), "to": end.isoformat(), "value": value}


def test_estimates_eight_days_of_half_hour_forecast_with_bill_and_scenarios():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        _row(
            start + timedelta(minutes=30 * index),
            start + timedelta(minutes=30 * (index + 1)),
            0.5,
            0.4,
            0.6,
        )
        for index in range(384)
    ]
    finish = start + timedelta(days=8)
    result = estimate(
        ("forecast-8-days", _forecast(rows)),
        ("wide-tariff", _tariff([_interval(start, finish, 20)], unit="GBP_pence/kWh")),
        {
            "billing": {
                "standing_charge": {
                    "amount_per_day": 0.30,
                    "currency": "GBP",
                    "taxable": False,
                },
                "tax": {"rate": 0.05, "energy_taxable": True},
                "source": "fixture standing charge and tax",
            }
        },
    )

    assert result.kind == DataKind.CALCULATED
    assert result.unit == "GBP"
    assert result.quantity_shape == "interval"
    assert result.data["calculation_basis"] == "forecast_consumption"
    assert result.data["estimate"]["energy_cost"] == Decimal("38.400")
    assert result.data["estimate"]["standing_charge"] == Decimal("2.40")
    assert result.data["estimate"]["tax"] == Decimal("1.92000")
    assert result.data["estimate"]["total"] == Decimal("42.72000")
    assert result.data["estimate"]["chargeable_days"] == 8
    assert result.data["uncertainty"]["lower"]["energy_cost"] == Decimal("30.720")
    assert result.data["uncertainty"]["lower"]["total"] == Decimal("34.65600")
    assert result.data["uncertainty"]["upper"]["energy_cost"] == Decimal("46.080")
    assert result.data["uncertainty"]["upper"]["total"] == Decimal("50.78400")
    assert len(result.data["intervals"]) == 384
    lineage = result.provenance[0]
    assert [item["artifact_id"] for item in lineage["inputs"]] == [
        "forecast-8-days",
        "wide-tariff",
    ]
    assert lineage["forecast_metadata"]["model"]["historical_lineage"] == {
        "artifact_id": "history-1"
    }
    assert "Calculated from forecast consumption; not a measured bill." in result.assumptions


def test_applies_daily_tariffs_over_multiple_forecast_intervals():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(days=1)
    finish = middle + timedelta(days=1)
    forecast = _forecast(
        [
            _row(start, middle, 10),
            _row(middle, finish, 10),
        ]
    )
    tariff = _tariff(
        [
            _interval(start, middle, 0.2),
            _interval(middle, finish, 0.3),
        ]
    )

    result = estimate(("f", forecast), ("t", tariff), {})

    assert result.data["estimate"] == {
        "energy_cost": Decimal("5.0"),
        "currency": "GBP",
        "complete_bill": False,
        "missing_components": ["standing_charge", "tax"],
    }
    assert result.data["intervals"][0]["rate"] == Decimal("0.2")
    assert result.data["intervals"][1]["rate"] == Decimal("0.3")
    assert "standing charge and tax" in result.warnings[-1].lower()


def test_splits_a_forecast_interval_when_tariff_changes_inside_it():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(minutes=30)
    finish = start + timedelta(hours=1)
    result = estimate(
        (
            "f",
            _forecast([_row(start, finish, 4, 2, 6)]),
        ),
        (
            "t",
            _tariff([_interval(start, middle, 0.1), _interval(middle, finish, 0.3)]),
        ),
        {},
    )

    interval = result.data["intervals"][0]
    assert interval["rate"] == Decimal("0.2")
    assert interval["energy_cost"] == Decimal("0.8")
    assert interval["lower_energy_cost"] == Decimal("0.4")
    assert interval["upper_energy_cost"] == Decimal("1.2")
    assert [segment["forecast_kwh"] for segment in interval["segments"]] == [
        Decimal("2.0"),
        Decimal("2.0"),
    ]
    assert [segment["energy_cost"] for segment in interval["segments"]] == [
        Decimal("0.20"),
        Decimal("0.60"),
    ]


def test_negative_tariff_reverses_lower_and_upper_cost_scenarios():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    finish = start + timedelta(hours=1)
    result = estimate(
        ("f", _forecast([_row(start, finish, 10, 5, 15)])),
        ("t", _tariff([_interval(start, finish, -0.2)])),
        {},
    )

    interval = result.data["intervals"][0]
    assert interval["energy_cost"] == Decimal("-2.0")
    assert interval["lower_energy_cost"] == Decimal("-3.0")
    assert interval["upper_energy_cost"] == Decimal("-1.0")
    assert result.data["uncertainty"]["lower"]["energy_cost"] == Decimal("-3.0")
    assert result.data["uncertainty"]["upper"]["energy_cost"] == Decimal("-1.0")


def test_billing_uses_local_calendar_days_across_dst():
    timezone = ZoneInfo("Europe/London")
    start = datetime(2026, 3, 25, tzinfo=timezone)
    boundaries = [start + timedelta(days=index) for index in range(9)]
    forecast = _forecast(
        [_row(boundaries[index], boundaries[index + 1], 1) for index in range(8)],
        timezone="Europe/London",
    )
    tariff = _tariff([_interval(boundaries[0], boundaries[-1], 0.2)])
    result = estimate(
        ("f", forecast),
        ("t", tariff),
        {
            "billing": {
                "standing_charge": {
                    "amount_per_day": 0.3,
                    "currency": "GBP",
                    "taxable": False,
                },
                "tax": {"rate": 0, "energy_taxable": False},
                "source": "fixture schedule",
            }
        },
    )

    assert result.data["estimate"]["chargeable_days"] == 8
    assert result.data["estimate"]["standing_charge"] == Decimal("2.4")
    assert result.data["estimate"]["energy_cost"] == Decimal("1.6")
    assert result.data["estimate"]["total"] == Decimal("4.0")


def test_custom_tariff_columns_and_unknown_or_missing_values():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    finish = start + timedelta(hours=1)
    forecast = _forecast([_row(start, finish, 2)])
    custom_tariff = _tariff([{"begin": start.isoformat(), "finish": finish.isoformat(), "price": 0.5}])
    result = estimate(
        ("f", forecast),
        ("t", custom_tariff),
        {"tariff_timestamp": "begin", "tariff_end": "finish", "tariff_column": "price"},
    )
    assert result.data["estimate"]["energy_cost"] == Decimal("1.0")

    with pytest.raises(EnergyError, match="nonempty string"):
        estimate(("f", forecast), ("t", custom_tariff), {"tariff_column": " "})
    with pytest.raises(EnergyError, match="is missing"):
        estimate(
            ("f", forecast),
            ("t", _tariff([_interval(start, finish, None)])),
            {},
        )
    with pytest.raises(EnergyError):
        estimate(("f", forecast), ("t", _tariff([_interval(start, finish, 1)], unit="CAD/kWh")), {})


def test_rejects_missing_tariff_coverage_and_open_ended_intervals():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(minutes=30)
    finish = start + timedelta(hours=1)
    forecast = _forecast([_row(start, finish, 1)])
    with pytest.raises(EnergyError, match="do not cover the forecast"):
        estimate(("f", forecast), ("t", _tariff([_interval(middle, finish, 0.2)])), {})

    with pytest.raises(EnergyError, match="explicit end timestamp"):
        estimate(
            ("f", forecast),
            ("t", _tariff([{"from": start.isoformat(), "to": None, "value": 0.2}])),
            {},
        )


@pytest.mark.parametrize(
    "rows",
    [
        lambda start, middle, finish: [
            _row(start, middle, 1),
            _row(middle + timedelta(minutes=1), finish, 1),
        ],
        lambda start, middle, finish: [
            _row(start, finish, 1),
            _row(middle, finish, 1),
        ],
    ],
    ids=["forecast-gap", "forecast-overlap"],
)
def test_rejects_gapped_or_overlapping_forecast_intervals(rows):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(minutes=30)
    finish = start + timedelta(hours=1)
    forecast = _forecast(rows(start, middle, finish))
    tariff = _tariff([_interval(start, finish, 0.2)])
    with pytest.raises(EnergyError):
        estimate(("f", forecast), ("t", tariff), {})


def test_rejects_overlapping_tariff_intervals_and_invalid_forecast_bounds():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(minutes=30)
    finish = start + timedelta(hours=1)
    forecast = _forecast([_row(start, finish, 1)])
    overlapping = _tariff(
        [_interval(start, finish, 0.2), _interval(middle, finish, 0.3)]
    )
    with pytest.raises(EnergyError, match="must not overlap"):
        estimate(("f", forecast), ("t", overlapping), {})

    bad_forecast = _forecast([_row(start, finish, 1, 2, 0)])
    with pytest.raises(EnergyError, match="0 <= lower"):
        estimate(("f", bad_forecast), ("t", _tariff([_interval(start, finish, 0.2)])), {})


@pytest.mark.parametrize(
    "result",
    [
        _forecast([], kind=DataKind.METERED),
        _forecast([], shape="counter"),
        _forecast([], unit="kW"),
    ],
    ids=["kind", "shape", "power-unit"],
)
def test_rejects_wrong_forecast_kind_shape_or_power_unit(result):
    with pytest.raises(EnergyError):
        estimate(("f", result), ("t", _tariff([])), {})
