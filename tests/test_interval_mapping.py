from datetime import UTC, datetime

import httpx
import pytest

from energy_agent_tools.connectors import http
from energy_agent_tools.models import AuthConfig, ConnectedAccount
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


@pytest.mark.parametrize(
    "settings,expected",
    [
        (
            {"quantity_shape": "interval", "interval_position": "start", "interval_seconds": 1800},
            True,
        ),
        ({"quantity_shape": "interval"}, False),
        ({"quantity_shape": "counter"}, False),
    ],
)
async def test_emon_interval_bounds_require_explicit_reviewed_mapping(
    tmp_path, monkeypatch, settings, expected
):
    monkeypatch.setenv("TEST_INTERVAL_FEED_KEY", "synthetic-secret")
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[[timestamp.timestamp() * 1000, 0.5]])
        )
    ) as transport:
        registry = Registry()
        http.register(registry)
        account = ConnectedAccount(
            id="feed",
            toolkit="openenergymonitor",
            user_id="owner",
            auth=AuthConfig(scheme="api-key", credential_env="TEST_INTERVAL_FEED_KEY"),
            settings={"base_url": "https://emon.example", "feed_id": 1, "unit": "kWh", **settings},
        )
        agent = EnergyAgent(registry, tmp_path, accounts=[account], http=transport)
        result = await agent.execute(
            agent.session("owner"),
            "openenergymonitor.get_feed",
            {
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:30:00Z",
                "interval": 1800,
            },
            account_id="feed",
        )
        assert result["ok"], result
        row = result["result"]["data"][0]
        assert ("end" in row) is expected
        if expected:
            assert row["end"] == "2026-01-01T00:30:00Z"
            assert result["result"]["time_end"] == "2026-01-01T00:30:00Z"
        assert result["result"]["quantity_shape"] == settings["quantity_shape"]
        await agent.close()


async def test_emon_reviewed_interval_cannot_change_request_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_INTERVAL_FEED_KEY", "synthetic-secret")
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=[])
        )
    ) as transport:
        registry = Registry()
        http.register(registry)
        account = ConnectedAccount(
            id="feed",
            toolkit="openenergymonitor",
            user_id="owner",
            auth=AuthConfig(scheme="api-key", credential_env="TEST_INTERVAL_FEED_KEY"),
            settings={
                "base_url": "https://emon.example",
                "feed_id": 1,
                "unit": "kWh",
                "quantity_shape": "interval",
                "interval_position": "start",
                "interval_seconds": 1800,
            },
        )
        agent = EnergyAgent(registry, tmp_path, accounts=[account], http=transport)
        result = await agent.execute(
            agent.session("owner"),
            "openenergymonitor.get_feed",
            {
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:30:00Z",
                "interval": 900,
            },
            account_id="feed",
        )
        assert not result["ok"] and result["error"]["code"] == "interval_mapping_incompatible"
        assert not calls
        await agent.close()
