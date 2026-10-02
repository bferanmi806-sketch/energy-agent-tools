from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult
from energy_agent_tools.temperature_alignment import align_temperature_context


def _result(
    rows: list[dict[str, object]],
    *,
    kind: DataKind = DataKind.ESTIMATED,
    resolution: str | None = "15min",
    unit: str = "degC",
    field_units: dict[str, str] | None = None,
    **metadata: object,
) -> EnergyResult:
    return EnergyResult(
        data=rows,
        kind=kind,
        unit=unit,
        source="weather-fixture",
        resolution=resolution,
        field_units=field_units or {},
        **metadata,
    )


def _align(
    result: EnergyResult,
    *,
    start: str = "2026-01-01T00:00:00Z",
    end: str = "2026-01-01T01:00:00Z",
    interval_minutes: int = 30,
    column: str = "temperature",
) -> EnergyResult:
    return align_temperature_context(
        ("temperature-artifact", result),
        start=start,
        end=end,
        interval_minutes=interval_minutes,
        column=column,
    )


def test_aligns_wide_celsius_rows_to_target_interval_starts() -> None:
    result = _result(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "temperature": 10},
            {"timestamp": "2026-01-01T00:15:00Z", "temperature": 12.5},
            {"timestamp": "2026-01-01T00:30:00Z", "temperature": 14},
            {"timestamp": "2026-01-01T00:45:00Z", "temperature": 15},
        ],
        resolution="15min",
        field_units={"temperature": "°C"},
    )

    aligned = _align(result)

    assert aligned.data == [
        {"timestamp": "2026-01-01T00:00:00Z", "temperature": 10.0},
        {"timestamp": "2026-01-01T00:30:00Z", "temperature": 14.0},
    ]
    assert aligned.unit == "degC"
    assert aligned.field_units == {"temperature": "degC"}
    assert aligned.resolution == "30min"
    assert aligned.quantity_shape == "instantaneous"
    assert aligned.time_start == datetime(2026, 1, 1, tzinfo=UTC)
    assert aligned.time_end == datetime(2026, 1, 1, 1, tzinfo=UTC)


def test_aligns_only_requested_variable_from_mixed_long_form_rows() -> None:
    result = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "variable": "temperature_2m",
                "value": 8.0,
                "unit": "°C",
            },
            {
                "timestamp": "not a timestamp",
                "variable": "relative_humidity_2m",
                "value": 50,
                "unit": "%",
            },
            {
                "timestamp": "2026-01-01T01:00:00Z",
                "variable": "temperature_2m",
                "value": 11.0,
                "unit": "Celsius",
            },
        ],
        resolution="1h",
        unit="mixed",
        field_units={"temperature_2m": "degC"},
    )

    aligned = align_temperature_context(
        ("open-meteo", result),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T02:00:00Z",
        interval_minutes=30,
    )

    assert aligned.data == [
        {"timestamp": "2026-01-01T00:00:00Z", "temperature": 8.0},
        {"timestamp": "2026-01-01T00:30:00Z", "temperature": 8.0},
        {"timestamp": "2026-01-01T01:00:00Z", "temperature": 11.0},
        {"timestamp": "2026-01-01T01:30:00Z", "temperature": 11.0},
    ]
    assert aligned.provenance[-1]["variable"] == "temperature_2m"
    assert aligned.provenance[-1]["coverage"]["aligned_intervals"] == 4  # type: ignore[index]


def test_does_not_borrow_a_future_reading_for_window_start() -> None:
    result = _result(
        [{"timestamp": "2026-01-01T00:15:00Z", "temperature": 10}],
        resolution="30min",
    )

    with pytest.raises(EnergyError) as error:
        _align(result, end="2026-01-01T00:30:00Z")

    assert error.value.code == "incomplete_coverage"


def test_refuses_stale_values_across_a_source_gap() -> None:
    result = _result(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "temperature": 10},
            {"timestamp": "2026-01-01T00:45:00Z", "temperature": 11},
        ],
        resolution="30min",
    )

    with pytest.raises(EnergyError) as error:
        _align(result, end="2026-01-01T01:00:00Z", interval_minutes=15)

    assert error.value.code == "stale_source_value"


@pytest.mark.parametrize(
    "times",
    [
        ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        ["2026-01-01T00:15:00Z", "2026-01-01T00:00:00Z"],
    ],
)
def test_rejects_duplicate_or_non_increasing_relevant_timestamps(
    times: list[str],
) -> None:
    result = _result(
        [
            {"timestamp": times[0], "temperature": 10},
            {"timestamp": times[1], "temperature": 11},
        ],
        resolution="15min",
    )

    with pytest.raises(EnergyError) as error:
        _align(result, end="2026-01-01T00:30:00Z")

    assert error.value.code == "invalid_timestamp_order"


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), True])
def test_rejects_missing_nonfinite_and_boolean_temperatures(bad_value: object) -> None:
    result = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "temperature": bad_value}],
        resolution="15min",
    )

    with pytest.raises(EnergyError):
        _align(result, end="2026-01-01T00:30:00Z")


@pytest.mark.parametrize(
    ("resolution", "unit", "field_units", "expected_code"),
    [
        (None, "degC", {}, "invalid_resolution"),
        ("5min", "degC", {}, "invalid_resolution"),
        ("15min", "degF", {}, "unit_mismatch"),
        ("15min", "degC", {"temperature": "degF"}, "unit_mismatch"),
    ],
)
def test_rejects_unknown_cadence_or_non_celsius_units(
    resolution: str | None,
    unit: str,
    field_units: dict[str, str],
    expected_code: str,
) -> None:
    result = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}],
        resolution=resolution,
        unit=unit,
        field_units=field_units,
    )

    with pytest.raises(EnergyError) as error:
        _align(result, end="2026-01-01T00:30:00Z")

    assert error.value.code == expected_code


def test_rejects_non_celsius_units_on_selected_long_rows() -> None:
    result = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "variable": "temperature_2m",
                "value": 50,
                "unit": "°F",
            }
        ],
        resolution="1h",
        unit="mixed",
    )

    with pytest.raises(EnergyError) as error:
        align_temperature_context(
            ("weather", result),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T01:00:00Z",
        )

    assert error.value.code == "unit_mismatch"


def test_rejects_conflicting_long_form_field_unit_declaration() -> None:
    result = _result(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "variable": "temperature_2m",
                "value": 10,
                "unit": "°C",
            }
        ],
        resolution="1h",
        unit="mixed",
        field_units={"temperature_2m": "degF"},
    )

    with pytest.raises(EnergyError) as error:
        align_temperature_context(
            ("weather", result),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T01:00:00Z",
        )

    assert error.value.code == "unit_mismatch"


def test_requires_explicit_offsets_and_supported_input_kinds() -> None:
    naive = _result([{"timestamp": "2026-01-01T00:00:00", "temperature": 10}])
    with pytest.raises(EnergyError) as timestamp_error:
        _align(naive, end="2026-01-01T00:30:00Z")
    assert timestamp_error.value.code == "naive_timestamp"

    calculated = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}],
        kind=DataKind.CALCULATED,
    )
    with pytest.raises(EnergyError) as kind_error:
        _align(calculated)
    assert kind_error.value.code == "invalid_kind"


@pytest.mark.parametrize("interval_minutes", [True, 10, 90])
def test_rejects_unsupported_target_cadence(interval_minutes: int) -> None:
    result = _result([{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}])

    with pytest.raises(EnergyError) as error:
        _align(result, interval_minutes=interval_minutes)

    assert error.value.code == "invalid_frequency"


def test_rejects_invalid_bounds_and_output_over_limit() -> None:
    result = _result([{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}])
    with pytest.raises(EnergyError) as invalid_bounds:
        _align(result, end="2026-01-01T00:45:00Z")
    assert invalid_bounds.value.code == "invalid_range"

    too_many_minutes = 15 * 100_001
    too_many_end = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=too_many_minutes)
    with pytest.raises(EnergyError) as too_large:
        _align(
            result,
            start="2026-01-01T00:00:00Z",
            end=too_many_end.isoformat(),
            interval_minutes=15,
        )
    assert too_large.value.code == "output_too_large"


def test_preserves_kind_scope_warnings_and_input_provenance() -> None:
    source = _result(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "temperature": 10},
            {"timestamp": "2026-01-01T00:15:00Z", "temperature": 11},
        ],
        kind=DataKind.METERED,
        resolution="15min",
        provider="weather-station",
        site_id="site-1",
        asset_id="sensor-1",
        warnings=["source warning"],
        provenance=[{"provider": "fixture", "observation_id": "obs-1"}],
    )

    aligned = _align(source, end="2026-01-01T00:30:00Z")

    assert aligned.kind is DataKind.METERED
    assert aligned.source == "weather-fixture"
    assert aligned.provider == "weather-station"
    assert aligned.site_id == "site-1"
    assert aligned.asset_id == "sensor-1"
    assert aligned.warnings == ["source warning"]
    assert aligned.provenance[0] == {"provider": "fixture", "observation_id": "obs-1"}
    alignment = aligned.provenance[-1]
    assert alignment["artifact_id"] == "temperature-artifact"
    assert alignment["input_kind"] == "metered"
    assert alignment["input_provenance"] == source.provenance
    assert "zero-order hold" in alignment["assumption"]


def test_metered_forward_hold_is_relabelled_estimated_with_warning() -> None:
    source = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}],
        kind=DataKind.METERED,
        resolution="1h",
    )

    aligned = _align(source, end="2026-01-01T01:00:00Z", interval_minutes=30)

    assert aligned.kind is DataKind.ESTIMATED
    assert any("labeled estimated" in warning for warning in aligned.warnings)


def test_forecast_kind_is_preserved_with_alignment_assumption() -> None:
    source = _result(
        [{"timestamp": "2026-01-01T00:00:00Z", "temperature": 10}],
        kind=DataKind.FORECAST,
        resolution="60min",
    )

    aligned = _align(source, end="2026-01-01T01:00:00Z", interval_minutes=30)

    assert aligned.kind is DataKind.FORECAST
    assert any("zero-order hold" in assumption for assumption in aligned.assumptions)


def test_current_celsius_units_allow_recorded_fahrenheit_origin():
    source = _result(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "temperature": 10},
            {"timestamp": "2026-01-01T00:30:00Z", "temperature": 11},
        ],
        resolution="30min",
        original_unit="degF",
        provenance=[{"operation": "convert_fahrenheit_to_celsius"}],
    )
    aligned = _align(source)
    assert aligned.unit == "degC" and aligned.original_unit == "degF"
    assert [row["temperature"] for row in aligned.data] == [10, 11]
    assert "convert_fahrenheit_to_celsius" in str(aligned.provenance)
