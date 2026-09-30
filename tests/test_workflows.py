"""A deterministic reference agent executes six workflows over one MCP connection.

Private/provider data is supplied by HTTP contract fixtures. Numerical solvers
are real installed libraries. This is not an autonomous LLM benchmark.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from energy_agent_tools.connectors import engineering, http, local
from energy_agent_tools.models import AuthConfig, ConnectedAccount, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent
from energy_agent_tools.server import create_server


async def test_six_workflows_one_mcp_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_OCTOPUS_KEY", "fixture-secret-never-returned")
    day = datetime(2026, 9, 29, tzinfo=UTC)
    times = [day + timedelta(minutes=30 * i) for i in range(48)]
    consumption = [
        {
            "interval_start": t.isoformat(),
            "interval_end": (t + timedelta(minutes=30)).isoformat(),
            "consumption": 5.0 if i == 24 else 0.25,
        }
        for i, t in enumerate(times)
    ]
    prices = [
        {
            "valid_from": t.isoformat(),
            "valid_to": (t + timedelta(minutes=30)).isoformat(),
            "value_inc_vat": 5.0 if 4 <= i < 8 else 25.0,
        }
        for i, t in enumerate(times)
    ]
    requests = []

    def provider(request):
        requests.append(request)
        path = request.url.path
        if "consumption" in path:
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"results": consumption, "next": None})
        if "standard-unit-rates" in path:
            return httpx.Response(200, json={"results": prices, "next": None})
        if request.url.host == "api.carbonintensity.org.uk":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "from": t.isoformat(),
                            "to": (t + timedelta(minutes=30)).isoformat(),
                            "intensity": {
                                "actual": None,
                                "forecast": 60 if 4 <= i < 8 else 180,
                                "index": "moderate",
                            },
                        }
                        for i, t in enumerate(times)
                    ]
                },
            )
        if request.url.host == "api.open-meteo.com":
            weather_day = datetime.fromisoformat(
                request.url.params.get("start_date", "2026-09-29")
            ).replace(tzinfo=UTC)
            hourly = {
                "time": [
                    (weather_day + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(24)
                ],
                "temperature_2m": [15.0] * 24,
                "shortwave_radiation": [500.0 if 8 <= i < 17 else 0.0 for i in range(24)],
            }
            return httpx.Response(
                200,
                json={
                    "hourly": hourly,
                    "hourly_units": {"temperature_2m": "°C", "shortwave_radiation": "W/m²"},
                },
            )
        raise AssertionError(f"Unexpected fixture endpoint: {request.url.host}{path}")

    registry = Registry()
    http.register(registry)
    engineering.register(registry)
    local.register(registry)
    site = Site(
        id="building", user_id="reference-agent", name="Test building", timezone="Europe/London"
    )
    account = ConnectedAccount(
        id="meter",
        toolkit="octopus-energy-account",
        user_id="reference-agent",
        site_id=site.id,
        auth=AuthConfig(scheme="basic", credential_env="FIXTURE_OCTOPUS_KEY"),
        settings={"mpan": "TEST123", "serial_number": "TEST456"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
        agent = EnergyAgent(registry, tmp_path, sites=[site], accounts=[account], http=transport)
        session = agent.session("reference-agent", "building")
        async with create_connected_server_and_client_session(
            create_server(agent, session)
        ) as client:

            async def execute(tool, arguments, *, persist=False, inputs=None):
                response = await client.call_tool(
                    "ENERGY_MULTI_EXECUTE_TOOL",
                    {
                        "calls": [
                            {
                                "tool": tool,
                                "arguments": arguments,
                                "persist": persist,
                                "input_artifacts": inputs or [],
                            }
                        ]
                    },
                )
                out = response.structuredContent["results"][0]
                assert out["ok"], out
                return out["result"]

            for query, expected in [
                ("smart meter consumption yesterday", "octopus_energy.get_consumption"),
                ("building consumption spike anomaly", "WORKBENCH_ANOMALY"),
                ("battery charging cheapest tariff", "engineering.schedule_battery_charging"),
                ("estimate solar generation", "engineering.estimate_solar_generation"),
                ("run power flow network", "engineering.run_power_flow"),
                ("compare consumption weather tariff", "WORKBENCH_JOIN"),
            ]:
                response = await client.call_tool(
                    "ENERGY_SEARCH_TOOLS", {"query": query, "limit": 5}
                )
                tools = response.structuredContent["tools"]
                assert len(tools) <= 5
                assert expected in {t["name"] for t in tools}, (query, [t["name"] for t in tools])

            # 1. Yesterday consumption. Meter result remains metered, total becomes calculated.
            meter = await execute(
                "octopus_energy.get_consumption",
                {"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()},
                persist=True,
            )
            meter_id = meter["data"]["artifact_id"]
            assert meter["kind"] == "metered"
            total = await execute(
                "WORKBENCH_SUMMARIZE", {"artifact_id": meter_id, "column": "value"}
            )
            assert total["data"]["sum"] == pytest.approx(16.75)
            assert total["provenance"][0]["input_kind"] == "metered"

            # 2. Building spike screening. It identifies the large interval without claiming cause.
            spike = await execute("WORKBENCH_ANOMALY", {"artifact_id": meter_id, "column": "value"})
            assert spike["data"]["anomaly_count"] == 1
            assert spike["data"]["preview"][0]["value"] == 5
            assert any("cause" in a for a in spike["assumptions"])

            # 3. Public tariff and forecast carbon drive advisory battery scheduling.
            tariff = await execute(
                "octopus_energy.get_tariffs",
                {"product_code": "TEST", "tariff_code": "TEST"},
                persist=True,
            )
            carbon = await execute("carbon_intensity_gb.get_intensity", {}, persist=True)
            carbon_rows = agent.workbench.read(session, carbon["data"]["artifact_id"]).data
            assert carbon["kind"] == "forecast"
            intervals = [
                {
                    "timestamp": times[i].isoformat(),
                    "duration_hours": 0.5,
                    "load_kw": 0,
                    "pv_kw": 0,
                    "price_per_kwh": prices[i]["value_inc_vat"] / 100,
                    "carbon_intensity_g_per_kwh": carbon_rows[i]["value"],
                }
                for i in range(8)
            ]
            battery_args = {
                "intervals": intervals,
                "battery": {
                    "capacity_kwh": 4,
                    "initial_soc_kwh": 0,
                    "max_charge_kw": 2,
                    "max_discharge_kw": 2,
                    "charge_efficiency": 1,
                    "discharge_efficiency": 1,
                    "target_final_soc_kwh": 4,
                },
                "objective": "cost",
            }
            inputs = [tariff["data"]["artifact_id"], carbon["data"]["artifact_id"]]
            charge = await execute(
                "engineering.schedule_battery_charging", battery_args, inputs=inputs
            )
            assert charge["data"]["summary"]["final_soc_kwh"] == pytest.approx(4)
            assert charge["data"]["summary"]["total_cost"] == pytest.approx(0.2)
            assert "forecast" in {p.get("input_kind") for p in charge["provenance"]}
            battery_args["objective"] = "carbon"
            clean = await execute(
                "engineering.schedule_battery_charging", battery_args, inputs=inputs
            )
            assert clean["data"]["summary"]["total_carbon_g"] == pytest.approx(240)

            # 4. Forecast weather -> pivot -> real pvlib estimate with input lineage.
            weather = await execute(
                "open_meteo.get_forecast",
                {
                    "start": "2026-10-01T00:00:00Z",
                    "end": "2026-10-02T00:00:00Z",
                    "latitude": 51.5,
                    "longitude": -0.12,
                    "variables": ["temperature_2m", "shortwave_radiation"],
                },
                persist=True,
            )
            weather_id = weather["data"]["artifact_id"]
            pivot = await execute(
                "WORKBENCH_PIVOT",
                {
                    "artifact_id": weather_id,
                    "timestamp": "timestamp",
                    "variable": "variable",
                    "value": "value",
                },
                persist=True,
            )
            weather_rows = agent.workbench.read(session, pivot["data"]["artifact_id"]).data
            assert all(r["timestamp"].startswith("2026-10-01") for r in weather_rows)
            rows = [
                {
                    "timestamp": r["timestamp"],
                    "ghi_w_m2": r["shortwave_radiation"],
                    "temp_air_c": r["temperature_2m"],
                    "duration_hours": 1,
                }
                for r in weather_rows
            ]
            solar = await execute(
                "engineering.estimate_solar_generation",
                {
                    "latitude": 51.5,
                    "longitude": -0.12,
                    "timezone": "Europe/London",
                    "dc_capacity_kw": 4,
                    "weather_rows": rows,
                },
                inputs=[weather_id],
            )
            assert solar["kind"] in {"estimated", "simulated", "forecast"}
            assert solar["kind"] != "metered"
            assert solar["source"] == "pvlib"
            assert any(p.get("input_kind") == "forecast" for p in solar["provenance"])

            # 5. A caller-supplied network goes through real pandapower AC Newton-Raphson.
            network = {
                "buses": [{"id": "slack", "vn_kv": 20}, {"id": "load", "vn_kv": 20}],
                "lines": [
                    {
                        "id": "line",
                        "from_bus": "slack",
                        "to_bus": "load",
                        "length_km": 1,
                        "r_ohm_per_km": 0.2,
                        "x_ohm_per_km": 0.1,
                        "max_i_ka": 0.4,
                    }
                ],
                "loads": [{"id": "load", "bus": "load", "p_mw": 1, "q_mvar": 0.2}],
                "ext_grid": [{"bus": "slack", "vm_pu": 1}],
            }
            power = await execute("engineering.run_power_flow", {"network": network})
            assert power["kind"] == "simulated"
            assert power["data"]["converged"]
            assert abs(power["data"]["totals"]["balance_error_mw"]) < 1e-6

            # Comparison uses a separate weather window matching the meter date.
            comparison_weather = await execute(
                "open_meteo.get_forecast",
                {
                    "latitude": 51.5,
                    "longitude": -0.12,
                    "start": day.isoformat(),
                    "end": (day + timedelta(days=1)).isoformat(),
                    "variables": ["temperature_2m", "shortwave_radiation"],
                },
                persist=True,
            )
            comparison_pivot = await execute(
                "WORKBENCH_PIVOT",
                {
                    "artifact_id": comparison_weather["data"]["artifact_id"],
                    "timestamp": "timestamp",
                    "variable": "variable",
                    "value": "value",
                },
                persist=True,
            )
            # 6. Align and compare consumption, tariff and weather while retaining each input unit/kind.
            metered_hour = await execute(
                "WORKBENCH_RESAMPLE",
                {
                    "artifact_id": meter_id,
                    "timestamp": "from",
                    "column": "value",
                    "frequency": "1h",
                    "aggregation": "sum",
                },
                persist=True,
            )
            tariff_hour = await execute(
                "WORKBENCH_RESAMPLE",
                {
                    "artifact_id": tariff["data"]["artifact_id"],
                    "timestamp": "from",
                    "column": "value",
                    "frequency": "1h",
                    "aggregation": "mean",
                },
                persist=True,
            )
            aligned = await execute(
                "WORKBENCH_JOIN",
                {
                    "left": metered_hour["data"]["artifact_id"],
                    "right": tariff_hour["data"]["artifact_id"],
                    "timestamp": "timestamp",
                },
                persist=True,
            )
            compared = await execute(
                "WORKBENCH_JOIN",
                {
                    "left": aligned["data"]["artifact_id"],
                    "right": comparison_pivot["data"]["artifact_id"],
                    "timestamp": "timestamp",
                },
            )
            assert len(compared["data"]) == 24
            assert compared["unit"] == "mixed"
            assert "temperature_2m" in compared["data"][0]
            assert len(compared["provenance"]) >= 3
            assert all(
                "fixture-secret-never-returned" not in str(r) for r in [meter, total, charge]
            )
        assert len(requests) >= 4
