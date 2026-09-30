from __future__ import annotations

import json

import httpx
import pytest

from energy_agent_tools.connectors.http import register
from energy_agent_tools.models import (
    ConnectedAccount,
    EnergyError,
    ExecutionContext,
    Session,
)
from energy_agent_tools.registry import Registry


def _ctx(transport: httpx.MockTransport, *, settings=None, credential="secret"):
    account = ConnectedAccount(
        id="account-1",
        user_id="user-1",
        toolkit="test",
        settings=settings or {},
    )
    return ExecutionContext(
        session=Session(id="session-1", user_id="user-1"),
        account=account,
        credential=credential,
        http=httpx.AsyncClient(transport=transport),
        workbench=None,
    )


def _registry() -> Registry:
    registry = Registry()
    register(registry)
    return registry


@pytest.mark.asyncio
async def test_registers_public_and_credentialed_http_tools_without_secret_schema_fields():
    registry = _registry()

    assert {
        "carbon-intensity-gb",
        "open-meteo",
        "octopus-energy",
        "home-assistant",
        "openenergymonitor",
        "elexon",
    } <= set(registry.toolkits)
    for tool in registry.tools.values():
        serialized = json.dumps(tool.input_schema).lower()
        assert "credential" not in serialized
        assert "apikey" not in serialized
        assert "api_key" not in serialized


@pytest.mark.asyncio
async def test_carbon_intensity_preserves_mixed_actual_and_forecast_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.carbonintensity.org.uk"
        assert request.url.path == "/intensity/2026-01-01T00:00:00Z/2026-01-01T01:00:00Z"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "from": "2026-01-01T00:00Z",
                        "to": "2026-01-01T00:30Z",
                        "intensity": {"actual": 100, "forecast": 101, "index": "moderate"},
                    },
                    {
                        "from": "2026-01-01T00:30Z",
                        "to": "2026-01-01T01:00Z",
                        "intensity": {"actual": None, "forecast": 90, "index": "low"},
                    },
                ]
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    result = await _registry().handlers["carbon_intensity_gb.get_intensity"](
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, ctx
    )
    assert [row["status"] for row in result.data] == ["actual", "forecast"]
    assert [row["kind"] for row in result.data] == ["calculated", "forecast"]
    assert result.kind.value == "calculated"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_carbon_regional_nested_response_is_flattened():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/regional/regionid/13"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "from": "2026-01-01T00:00Z",
                        "to": "2026-01-01T00:30Z",
                        "regions": [
                            {
                                "regionid": 13,
                                "shortname": "London",
                                "intensity": {"forecast": 120, "index": "moderate"},
                                "generationmix": [],
                            }
                        ],
                    }
                ]
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    result = await _registry().handlers["carbon_intensity_gb.get_intensity"]({"region_id": 13}, ctx)
    assert result.data[0]["regionid"] == 13
    assert result.data[0]["status"] == "forecast"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_carbon_intensity_empty_is_a_valid_empty_result():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": []}, request=request)
    )
    ctx = _ctx(transport, credential=None)
    result = await _registry().handlers["carbon_intensity_gb.get_intensity"]({}, ctx)
    assert result.data == []
    assert result.unit == "gCO2/kWh"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_open_meteo_hourly_radiation_and_weather_are_normalized():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["timezone"] == "UTC"
        assert "shortwave_radiation" in request.url.params["hourly"]
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-01-01T00:00", "2026-01-01T01:00"],
                    "temperature_2m": [1.5, 1.8],
                    "shortwave_radiation": [0, None],
                }
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    result = await _registry().handlers["open_meteo.get_forecast"](
        {
            "latitude": 51.5,
            "longitude": -0.1,
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T02:00:00Z",
            "variables": ["temperature_2m", "shortwave_radiation"],
        },
        ctx,
    )
    assert result.kind.value == "forecast"
    assert result.unit == "mixed"
    assert result.data[-1]["value"] is None
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_open_meteo_rejects_naive_utc_range_before_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500, request=request)

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["open_meteo.get_forecast"](
            {
                "latitude": 0,
                "longitude": 0,
                "start": "2026-01-01T00:00:00",
                "end": "2026-01-01T01:00:00Z",
            },
            ctx,
        )
    assert caught.value.code == "naive_time"
    assert not called
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_octopus_consumption_uses_account_meter_and_bounded_same_origin_pagination():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        assert request.headers.get("authorization", "").startswith("Basic ")
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "interval_start": "2026-01-01T00:30:00Z",
                            "interval_end": "2026-01-01T01:00:00Z",
                            "consumption": 0.5,
                        }
                    ],
                    "next": None,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "interval_start": "2026-01-01T00:00:00Z",
                        "interval_end": "2026-01-01T00:30:00Z",
                        "consumption": 0.4,
                    }
                ],
                "next": "https://api.octopus.energy/v1/electricity-meter-points/123/meters/ABC/consumption/?page=2",
            },
            request=request,
        )

    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"mpan": "123", "serial_number": "ABC"},
        credential="octopus-token",
    )
    result = await _registry().handlers["octopus_energy.get_consumption"](
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, ctx
    )
    assert len(result.data) == 2
    assert result.data[0]["unit"] == "kWh"
    assert len(seen) == 2
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_octopus_consumption_auth_failure_is_structured_and_secret_free():
    secret = "octopus-token"
    transport = httpx.MockTransport(lambda request: httpx.Response(401, request=request))
    ctx = _ctx(
        transport,
        settings={"mpan": "123", "serial_number": "ABC"},
        credential=secret,
    )
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["octopus_energy.get_consumption"]({}, ctx)
    assert caught.value.code == "authentication_failed"
    assert secret not in caught.value.message
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_octopus_pagination_rejects_cross_origin_next_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "next": "https://evil.example/steal",
            },
            request=request,
        )

    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"mpan": "123", "serial_number": "ABC"},
        credential="octopus-token",
    )
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["octopus_energy.get_consumption"]({}, ctx)
    assert caught.value.code == "unsafe_redirect"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_octopus_tariffs_can_read_public_product_rates_without_credential():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/products/PROD/electricity-tariffs/TARIFF/standard-unit-rates/" in request.url.path
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": "2026-01-01T00:30:00Z",
                        "value_inc_vat": 24.5,
                        "value_exc_vat": 20.4,
                    }
                ],
                "next": None,
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), settings={}, credential=None)
    result = await _registry().handlers["octopus_energy.get_tariffs"](
        {"product_code": "PROD", "tariff_code": "TARIFF"}, ctx
    )
    assert result.data[0]["value"] == 24.5
    assert result.unit == "p/kWh"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_home_assistant_state_reads_configured_origin_and_bearer_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://ha.local:8123/api/states/sensor.grid_power"
        assert request.headers["authorization"] == "Bearer ha-token"
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.grid_power",
                "state": "123.4",
                "attributes": {"unit_of_measurement": "W", "state_class": "measurement"},
                "last_changed": "2026-01-01T00:00:00+00:00",
            },
            request=request,
        )

    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"base_url": "http://ha.local:8123"},
        credential="ha-token",
    )
    result = await _registry().handlers["home_assistant.get_state"](
        {"entity_id": "sensor.grid_power"}, ctx
    )
    assert result.data["state"] == "123.4"
    assert result.unit == "W"
    assert result.kind.value == "metered"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_home_assistant_history_empty_and_auth_failure_contracts():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=[[]], request=request)
        return httpx.Response(403, request=request)

    registry = _registry()
    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"base_url": "http://ha.local:8123"},
        credential="ha-token",
    )
    result = await registry.handlers["home_assistant.get_history"](
        {
            "entity_id": "sensor.grid_power",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
        },
        ctx,
    )
    assert result.data == []
    with pytest.raises(EnergyError) as caught:
        await registry.handlers["home_assistant.get_state"]({"entity_id": "sensor.grid_power"}, ctx)
    assert caught.value.code == "authentication_failed"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_home_assistant_requires_credential_without_requesting():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500, request=request)

    ctx = _ctx(
        httpx.MockTransport(handler), settings={"base_url": "http://ha.local:8123"}, credential=None
    )
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["home_assistant.get_state"](
            {"entity_id": "sensor.grid_power"}, ctx
        )
    assert caught.value.code == "credential_required"
    assert not called
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_emoncms_feed_time_series_uses_milliseconds_and_does_not_return_api_key():
    secret = "read-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emoncms/feed/data.json"
        assert request.url.params["apikey"] == secret
        assert int(request.url.params["start"]) > 1_000_000_000_000
        return httpx.Response(
            200, json=[[1767225600000, 10.0], [1767225660000, None]], request=request
        )

    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"base_url": "http://emon.local/emoncms", "feed_id": 7, "unit": "W"},
        credential=secret,
    )
    result = await _registry().handlers["openenergymonitor.get_feed"](
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "interval": 60}, ctx
    )
    assert result.data[0]["value"] == 10.0
    assert result.data[1]["value"] is None
    assert secret not in result.model_dump_json()
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_emoncms_null_response_is_malformed_and_empty_series_is_valid():
    responses = iter([None, []])

    def handler(request: httpx.Request) -> httpx.Response:
        value = next(responses)
        return httpx.Response(200, json=value, request=request)

    ctx = _ctx(
        httpx.MockTransport(handler),
        settings={"base_url": "http://emon.local", "feed_id": 7},
        credential="read-key",
    )
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["openenergymonitor.get_feed"](
            {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, ctx
        )
    assert caught.value.code == "malformed_response"
    result = await _registry().handlers["openenergymonitor.get_feed"](
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, ctx
    )
    assert result.data == []
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_elexon_generation_and_demand_dataset_normalization():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data.elexon.co.uk"
        assert request.url.path == "/bmrs/api/v1/datasets/INDGEN"
        assert request.url.params["publishDateTimeFrom"] == "2026-01-01T00:00:00Z"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "dataset": "INDGEN",
                        "publishTime": "2026-01-01T00:00:00Z",
                        "startTime": "2026-01-01T00:30:00Z",
                        "generation": 34000,
                    }
                ]
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    result = await _registry().handlers["elexon.get_grid_data"](
        {"dataset": "INDGEN", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, ctx
    )
    assert result.kind.value == "forecast"
    assert result.data[0]["value"] == 34000.0
    assert result.data[0]["unit"] == "MW"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_elexon_null_data_is_structured_failure_and_http_error_is_retryable():
    responses = iter(
        [
            httpx.Response(200, json={"data": None}),
            httpx.Response(503),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        return httpx.Response(
            response.status_code,
            json=response.json() if response.content else None,
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["elexon.get_grid_data"]({}, ctx)
    assert caught.value.code == "malformed_response"
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["elexon.get_grid_data"]({}, ctx)
    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable
    await ctx.http.aclose()


async def test_carbon_start_only_forecast_uses_documented_horizon():
    def handler(request):
        assert request.url.path == "/intensity/2026-01-01T00:00:00Z/fw24h"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "from": "2026-01-01T00:00Z",
                        "to": "2026-01-01T00:30Z",
                        "intensity": {"actual": None, "forecast": 100},
                    }
                ]
            },
        )

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    try:
        result = await _registry().handlers["carbon_intensity_gb.get_intensity"](
            {"start": "2026-01-01T00:00:00Z"}, ctx
        )
        assert result.kind.value == "forecast"
    finally:
        await ctx.http.aclose()


async def test_tariff_default_window_is_bounded():
    def handler(request):
        assert "period_from" in request.url.params and "period_to" in request.url.params
        return httpx.Response(200, json={"results": [], "next": None})

    ctx = _ctx(httpx.MockTransport(handler), credential=None)
    try:
        result = await _registry().handlers["octopus_energy.get_tariffs"](
            {"product_code": "TEST", "tariff_code": "TEST"}, ctx
        )
        assert result.data == []
    finally:
        await ctx.http.aclose()
