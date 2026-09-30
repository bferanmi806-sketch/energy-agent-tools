from __future__ import annotations

import pytest

from energy_agent_tools.connectors.engineering import (
    calculate_heat_loss,
    estimate_solar_generation,
    register,
    run_power_flow,
    schedule_battery_charging,
)
from energy_agent_tools.models import EnergyError
from energy_agent_tools.registry import Registry


@pytest.mark.asyncio
async def test_registers_all_engineering_tools_with_explicit_status() -> None:
    registry = Registry()
    register(registry)

    assert registry.toolkits["engineering"].status == "experimental"
    assert set(registry.tools) == {
        "engineering.estimate_solar_generation",
        "engineering.run_power_flow",
        "engineering.calculate_heat_loss",
        "engineering.schedule_battery_charging",
    }


@pytest.mark.asyncio
async def test_pvlib_estimate_is_zero_at_night_and_positive_with_daylight() -> None:
    result = await estimate_solar_generation(
        {
            "latitude": 35.0,
            "longitude": -110.0,
            "timezone": "UTC",
            "dc_capacity_kw": 5.0,
            "surface_tilt_deg": 30.0,
            "surface_azimuth_deg": 180.0,
            "weather_rows": [
                {
                    "timestamp": "2025-06-21T05:00:00+00:00",
                    "ghi_w_m2": 0.0,
                    "dni_w_m2": 0.0,
                    "dhi_w_m2": 0.0,
                },
                {
                    "timestamp": "2025-06-21T19:00:00+00:00",
                    "ghi_w_m2": 900.0,
                    "dni_w_m2": 800.0,
                    "dhi_w_m2": 100.0,
                    "temp_air_c": 25.0,
                    "wind_speed_m_s": 2.0,
                },
            ],
        },
        None,  # type: ignore[arg-type]
    )

    intervals = result.data["intervals"]
    assert intervals[0]["ac_power_kw"] == pytest.approx(0.0, abs=1e-10)
    assert intervals[1]["ac_power_kw"] > 0.0
    assert result.kind.value == "estimated"
    assert result.source == "pvlib"
    assert any("PVWatts" in assumption for assumption in result.assumptions)


@pytest.mark.asyncio
async def test_pvlib_rejects_naive_timestamps() -> None:
    with pytest.raises(EnergyError, match="explicit timezone") as exc:
        await estimate_solar_generation(
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "timezone": "Europe/London",
                "dc_capacity_kw": 1.0,
                "weather_rows": [{"timestamp": "2025-06-21T12:00:00", "ghi_w_m2": 500}],
            },
            None,  # type: ignore[arg-type]
        )
    assert exc.value.code == "invalid_timestamp"


@pytest.mark.asyncio
async def test_pandapower_voltage_drop_and_active_power_conservation() -> None:
    common = {
        "buses": [{"id": "grid", "vn_kv": 11.0}, {"id": "load", "vn_kv": 11.0}],
        "lines": [
            {
                "id": "feeder",
                "from_bus": "grid",
                "to_bus": "load",
                "length_km": 2.0,
                "r_ohm_per_km": 0.2,
                "x_ohm_per_km": 0.4,
                "c_nf_per_km": 0.0,
                "max_i_ka": 1.0,
            }
        ],
        "ext_grid": [{"id": "utility", "bus": "grid", "vm_pu": 1.0}],
    }
    light = {**common, "loads": [{"id": "house", "bus": "load", "p_mw": 0.2, "q_mvar": 0.04}]}
    heavy = {**common, "loads": [{"id": "house", "bus": "load", "p_mw": 2.0, "q_mvar": 0.4}]}

    light_result = await run_power_flow({"network": light}, None)  # type: ignore[arg-type]
    heavy_result = await run_power_flow({"network": heavy}, None)  # type: ignore[arg-type]
    light_bus = next(bus for bus in light_result.data["buses"] if bus["id"] == "load")
    heavy_bus = next(bus for bus in heavy_result.data["buses"] if bus["id"] == "load")

    assert heavy_bus["vm_pu"] < light_bus["vm_pu"]
    totals = heavy_result.data["totals"]
    assert abs(totals["balance_error_mw"]) < 1e-6
    assert totals["generation_mw"] > totals["load_mw"]
    assert totals["line_loss_mw"] > 0.0


@pytest.mark.asyncio
async def test_pandapower_rejects_unknown_bus_and_missing_slack() -> None:
    with pytest.raises(EnergyError) as unknown_bus:
        await run_power_flow(
            {
                "network": {
                    "buses": [{"id": "a", "vn_kv": 11.0}],
                    "loads": [{"id": "bad", "bus": "missing", "p_mw": 0.1}],
                }
            },
            None,  # type: ignore[arg-type]
        )
    assert unknown_bus.value.code == "invalid_network"

    with pytest.raises(EnergyError) as no_slack:
        await run_power_flow(
            {"network": {"buses": [{"id": "a", "vn_kv": 11.0}]}},
            None,  # type: ignore[arg-type]
        )
    assert no_slack.value.code == "invalid_network"


@pytest.mark.asyncio
async def test_heat_loss_calculation_preserves_units_and_gains() -> None:
    result = await calculate_heat_loss(
        {
            "components": [
                {"name": "wall", "area_m2": 100.0, "u_value_w_m2k": 0.3},
                {"name": "windows", "area_m2": 20.0, "u_value_w_m2k": 1.5},
            ],
            "indoor_temp_c": 21.0,
            "outdoor_temp_c": 1.0,
            "volume_m3": 250.0,
            "air_changes_per_hour": 0.5,
            "internal_gains_kw": 0.2,
            "duration_hours": 2.0,
        },
        None,  # type: ignore[arg-type]
    )

    assert result.data["transmission_ua_w_per_k"] == pytest.approx(60.0)
    assert result.data["gross_heat_loss_kw"] == pytest.approx(2.0383333333)
    assert result.data["net_heating_demand_kw"] == pytest.approx(1.8383333333)
    assert result.data["energy_required_kwh"] == pytest.approx(3.6766666666)
    assert result.unit == "kW_th and kWh_th"


@pytest.mark.asyncio
async def test_battery_schedule_is_feasible_and_uses_low_price_to_charge() -> None:
    result = await schedule_battery_charging(
        {
            "intervals": [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "duration_hours": 1.0,
                    "load_kw": 1.0,
                    "pv_kw": 0.0,
                    "price_per_kwh": 0.10,
                },
                {
                    "timestamp": "2025-01-01T01:00:00+00:00",
                    "duration_hours": 1.0,
                    "load_kw": 1.0,
                    "pv_kw": 0.0,
                    "price_per_kwh": 0.50,
                },
            ],
            "battery": {
                "capacity_kwh": 2.0,
                "initial_soc_kwh": 0.0,
                "max_charge_kw": 2.0,
                "max_discharge_kw": 2.0,
            },
        },
        None,  # type: ignore[arg-type]
    )

    schedule = result.data["schedule"]
    assert schedule[0]["charge_kw"] > 0.0
    assert schedule[1]["discharge_kw"] > 0.0
    assert schedule[0]["soc_kwh"] >= schedule[1]["soc_kwh"]
    assert 0.0 <= schedule[0]["soc_kwh"] <= 2.0
    assert schedule[1]["soc_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert result.data["summary"]["cost_savings"] > 0.0


@pytest.mark.asyncio
async def test_battery_schedule_rejects_infeasible_final_soc() -> None:
    with pytest.raises(EnergyError) as exc:
        await schedule_battery_charging(
            {
                "intervals": [
                    {
                        "timestamp": "2025-01-01T00:00:00+00:00",
                        "duration_hours": 1.0,
                        "load_kw": 0.0,
                        "pv_kw": 0.0,
                        "price_per_kwh": 0.2,
                    }
                ],
                "battery": {
                    "capacity_kwh": 2.0,
                    "initial_soc_kwh": 0.0,
                    "max_charge_kw": 0.5,
                    "max_discharge_kw": 1.0,
                    "target_final_soc_kwh": 2.0,
                },
            },
            None,  # type: ignore[arg-type]
        )
    assert exc.value.code == "battery_infeasible"
