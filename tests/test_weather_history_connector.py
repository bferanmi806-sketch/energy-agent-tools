from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from energy_agent_tools.connectors import http as http_connector
from energy_agent_tools.connectors import weather_history
from energy_agent_tools.models import Action, EnergyError, ExecutionContext, Session
from energy_agent_tools.registry import Registry


def _registry() -> Registry:
    registry = Registry()
    http_connector.register(registry)
    weather_history.register(registry)
    return registry


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _context(
    payload: Any,
    *,
    status_code: int = 200,
    requests: list[httpx.Request] | None = None,
) -> ExecutionContext:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            status_code,
            content=json.dumps(payload, allow_nan=True).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    return ExecutionContext(
        session=Session(id="session-1", user_id="user-1"),
        account=None,
        credential=None,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        workbench=None,
        site_id="site-1",
        asset_id="weather-asset-1",
    )


def _payload(times: list[Any], temperatures: list[Any], unit: str = "°C") -> dict[str, Any]:
    return {
        "hourly": {"time": times, "temperature_2m": temperatures},
        "hourly_units": {"temperature_2m": unit},
    }


@pytest.mark.asyncio
async def test_historical_temperature_uses_archive_best_match_and_filters_half_open_window():
    requested: list[httpx.Request] = []
    payload = _payload(
        [
            _epoch("2025-12-31T23:00:00Z"),
            _epoch("2026-01-01T00:00:00Z"),
            _epoch("2026-01-01T01:00:00Z"),
            _epoch("2026-01-02T00:00:00Z"),
        ],
        [2.0, 3.5, 4.0, 5.0],
    )
    ctx = _context(payload, requests=requested)
    registry = _registry()
    try:
        result = await registry.handlers["open_meteo.get_historical_temperature"](
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "start": "2026-01-01T01:00:00+01:00",
                "end": "2026-01-02T00:00:00Z",
            },
            ctx,
        )
    finally:
        await ctx.http.aclose()

    assert len(requested) == 1
    request = requested[0]
    assert request.url.host == "archive-api.open-meteo.com"
    assert request.url.path == "/v1/archive"
    assert request.url.params["latitude"] == "51.5"
    assert request.url.params["longitude"] == "-0.1"
    assert request.url.params["hourly"] == "temperature_2m"
    assert request.url.params["timezone"] == "UTC"
    assert request.url.params["temperature_unit"] == "celsius"
    assert request.url.params["timeformat"] == "unixtime"
    assert request.url.params["start_date"] == "2026-01-01"
    assert request.url.params["end_date"] == "2026-01-01"
    assert result.kind.value == "estimated"
    assert result.provider == "open-meteo"
    assert result.source == "open-meteo"
    assert result.site_id == "site-1"
    assert result.asset_id == "weather-asset-1"
    assert result.unit == "degC"
    assert result.field_units == {"temperature": "degC"}
    assert result.resolution == "1h"
    assert result.quantity_shape == "instantaneous"
    assert result.data == [
        {"timestamp": "2026-01-01T00:00:00Z", "temperature": 3.5},
        {"timestamp": "2026-01-01T01:00:00Z", "temperature": 4.0},
    ]
    assert result.provenance[0]["model_selection"] == "Best Match (provider default)"
    assert any("not a physical thermometer" in warning for warning in result.warnings)
    tool = registry.get("open_meteo.get_historical_temperature")
    assert tool.capabilities == ["get_historical_weather"]
    assert tool.actions == {Action.READ}


@pytest.mark.asyncio
async def test_null_temperatures_are_preserved_with_a_warning():
    ctx = _context(_payload([_epoch("2026-01-01T00:00:00Z")], [None]))
    try:
        result = await _registry().handlers["open_meteo.get_historical_temperature"](
            {
                "latitude": 0,
                "longitude": 0,
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T01:00:00Z",
            },
            ctx,
        )
    finally:
        await ctx.http.aclose()

    assert result.data == [{"timestamp": "2026-01-01T00:00:00Z", "temperature": None}]
    assert any("null values" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (_payload([_epoch("2026-01-01T00:00:00Z")], [1.0], unit="°F"), "unit_mismatch"),
        (_payload([_epoch("2026-01-01T00:00:00Z")], [1.0, 2.0]), "malformed_response"),
        (
            _payload(
                [_epoch("2026-01-01T00:00:00Z"), _epoch("2026-01-01T00:00:00Z")],
                [1.0, 2.0],
            ),
            "malformed_response",
        ),
        (_payload([True], [1.0]), "malformed_response"),
        (_payload([_epoch("2026-01-01T00:00:00Z")], [True]), "malformed_response"),
        (_payload([float("inf")], [1.0]), "malformed_response"),
        (_payload([_epoch("2026-01-01T00:00:00Z")], [float("nan")]), "malformed_response"),
    ],
)
async def test_rejects_malformed_history_payloads(payload: Any, error_code: str):
    ctx = _context(payload)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["open_meteo.get_historical_temperature"](
                {
                    "latitude": 0,
                    "longitude": 0,
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-01T02:00:00Z",
                },
                ctx,
            )
    finally:
        await ctx.http.aclose()

    assert caught.value.code == error_code


@pytest.mark.asyncio
async def test_archive_rate_limit_uses_shared_http_error_behavior():
    ctx = _context({}, status_code=429)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["open_meteo.get_historical_temperature"](
                {
                    "latitude": 0,
                    "longitude": 0,
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-01T01:00:00Z",
                },
                ctx,
            )
    finally:
        await ctx.http.aclose()

    assert caught.value.code == "rate_limited"
    assert caught.value.retryable


@pytest.mark.asyncio
async def test_requires_offset_aware_ranges_and_limits_history_to_366_days():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_payload([], []), request=request)

    ctx = ExecutionContext(
        session=Session(id="session-1", user_id="user-1"),
        account=None,
        credential=None,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        workbench=None,
    )
    call = _registry().handlers["open_meteo.get_historical_temperature"]
    try:
        with pytest.raises(EnergyError) as naive:
            await call(
                {
                    "latitude": 0,
                    "longitude": 0,
                    "start": "2026-01-01T00:00:00",
                    "end": "2026-01-01T01:00:00Z",
                },
                ctx,
            )
        assert naive.value.code == "naive_time"
        with pytest.raises(EnergyError) as too_long:
            await call(
                {
                    "latitude": 0,
                    "longitude": 0,
                    "start": "2025-01-01T00:00:00Z",
                    "end": "2026-01-03T00:00:00Z",
                },
                ctx,
            )
        assert too_long.value.code == "range_too_large"
        assert not called
    finally:
        await ctx.http.aclose()
