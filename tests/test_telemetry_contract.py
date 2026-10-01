"""Verify observation freshness and counter semantics at the provider boundary."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from energy_agent_tools.app import build_agent
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.onboarding import LocalProfile

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age", "expected"), [(5, None), (601, "stale_telemetry"), (None, "freshness_unavailable")]
)
async def test_connection_to_current_power_requires_observation_age(tmp_path, age, expected):
    async def handler(request):
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running"})
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.power",
                "state": "1750",
                "attributes": {"unit_of_measurement": "W", "state_class": "measurement"},
                "last_updated": (NOW - timedelta(seconds=age)).isoformat()
                if age is not None
                else None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LocalProfile(tmp_path, http=client) as profile:
            profile.create_site("School", "America/New_York", site_id="school")
            connection = await profile.connect(
                "home_assistant",
                credential="project-test-only",
                site_id="school",
                metadata={
                    "base_url": "https://ha.fixture",
                    "entity_id": "sensor.power",
                    "telemetry_role": "current_power",
                    "measurement_kind": "metered",
                    "quantity_shape": "instantaneous",
                    "unit": "W",
                },
            )
            assert connection["ok"]
        agent = build_agent(tmp_path)
        await agent.http.aclose()
        agent.http = client
        agent._owns_http = False
        agent.calendar_clock = lambda: NOW
        try:
            result = await agent.resolver.execute(
                agent.session("local", "school"), CapabilityRequest(capability="get_current_power")
            )
            if expected:
                assert result["error"]["code"] == expected, result
            else:
                assert result["ok"], result
                assert result["result"]["quantity_shape"] == "instantaneous"
                assert result["result"]["data"]["state"] == "1750"
                assert any(
                    entry.get("observation_age_seconds") == 5
                    for entry in result["result"]["provenance"]
                )
        finally:
            await agent.close()


async def test_sensor_changed_to_counter_cannot_pass_interval_binding(tmp_path):
    async def handler(request):
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running"})
        payload = {
            "entity_id": "sensor.energy",
            "state": "240",
            "attributes": {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
            "last_updated": NOW.isoformat(),
        }
        return httpx.Response(200, json=[[payload]] if "/history/" in request.url.path else payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with LocalProfile(tmp_path, http=client) as profile:
            profile.create_site("Home", "UTC", site_id="home")
            profile.configure_connection(
                "home_assistant",
                credential="project-test-only",
                site_id="home",
                metadata={
                    "base_url": "https://ha.fixture",
                    "entity_id": "sensor.energy",
                    "telemetry_role": "consumption_interval",
                    "measurement_kind": "metered",
                    "quantity_shape": "interval",
                    "unit": "kWh",
                    "state_class": "measurement",
                },
            )
        agent = build_agent(tmp_path)
        await agent.http.aclose()
        agent.http, agent._owns_http = client, False
        try:
            output = await agent.resolver.execute(
                agent.session("local", "home"),
                CapabilityRequest(
                    capability="get_energy_consumption",
                    arguments={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
                ),
            )
            assert output["error"]["code"] == "binding_semantics_changed", output
        finally:
            await agent.close()
