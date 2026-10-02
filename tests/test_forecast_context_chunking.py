from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from energy_agent_tools import EnergyAgentTools

HISTORY_START = datetime(2025, 4, 2, tzinfo=UTC)
HISTORY_END = datetime(2026, 10, 2, tzinfo=UTC)
FROZEN_NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
FORECAST_START = datetime(2026, 10, 3, tzinfo=UTC)
FORECAST_END = datetime(2026, 10, 11, tzinfo=UTC)
INTERVAL = timedelta(minutes=30)


def _write_history(path):
    with path.open("w") as stream:
        stream.write("timestamp,end,value\n")
        point = HISTORY_START
        while point < HISTORY_END:
            edge = point + INTERVAL
            stream.write(f"{point.isoformat()},{edge.isoformat()},0.5\n")
            point = edge


def _configuration():
    return {
        "sites": [
            {
                "id": "site",
                "user_id": "owner",
                "name": "Synthetic site",
                "timezone": "UTC",
                "latitude": 52.52,
                "longitude": 13.41,
            }
        ],
        "bindings": [
            {
                "capability": "get_energy_consumption",
                "tool": "CSV_READ_TIMESERIES",
                "reviewed": True,
                "kind": "metered",
                "unit": "kWh",
                "quantity_shape": "interval",
                "fixed_arguments": {
                    "file": "history.csv",
                    "kind": "metered",
                    "unit": "kWh",
                    "timezone": "UTC",
                    "quantity_shape": "interval",
                    "resolution": "30min",
                },
            }
        ],
    }


def _weather_payload(request):
    query = request.url.params
    first = datetime.fromisoformat(query["start_date"]).replace(tzinfo=UTC)
    last = datetime.fromisoformat(query["end_date"]).replace(tzinfo=UTC) + timedelta(days=1)
    points = []
    while first < last:
        points.append(first)
        first += timedelta(hours=1)
    archive = request.url.path == "/v1/archive"
    variables = query["hourly"].split(",")
    hourly = {
        "time": [
            int(point.timestamp()) if archive else point.strftime("%Y-%m-%dT%H:%M")
            for point in points
        ]
    }
    units = {
        "temperature_2m": "°C",
        "cloud_cover": "%",
        "wind_speed_10m": "km/h",
        "shortwave_radiation": "W/m²",
    }
    for variable in variables:
        hourly[variable] = [10 if variable == "temperature_2m" else 1 for _ in points]
    return {"hourly": hourly, "hourly_units": {name: units[name] for name in variables}}


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _run(session, interface, parameters):
    if interface == "sdk":
        return session.skill("consumption-forecast", parameters)
    return session.dispatch(
        "ENERGY_RUN_SKILL",
        {"skill_id": "consumption-forecast", "parameters": parameters},
    )


def _context_resolution(response):
    if "context_resolution" in response:
        return response["context_resolution"]
    return next(
        item["context_resolution"] for item in response["evidence"] if "context_resolution" in item
    )


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_long_history_weather_context_is_chunked_and_complete(tmp_path, interface):
    data = tmp_path / "data"
    data.mkdir()
    _write_history(data / "history.csv")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["latitude"] == "52.52"
        assert request.url.params["longitude"] == "13.41"
        return httpx.Response(200, json=_weather_payload(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", _configuration(), data_root=data) as energy:
            energy.agent.calendar_clock = lambda: FROZEN_NOW
            energy.agent.http = transport
            session = energy.session("owner", "site")
            response = await _run(
                session,
                interface,
                {
                    "start": FORECAST_START.isoformat(),
                    "end": FORECAST_END.isoformat(),
                    "history_months": 18,
                    "context_mode": "required",
                },
            )

            assert response["ok"], response
            assert response["window"]["history_start"] == HISTORY_START.isoformat()
            assert response["window"]["history_end"] == HISTORY_END.isoformat()
            assert _context_resolution(response)["status"] == "available"
            assert response["model"]["context"]["historical_kind"] == "estimated"
            assert response["model"]["context"]["future_kind"] == "forecast"
            assert response["forecast_summary"]["total_kwh"] == pytest.approx(192)

            history_context = energy.agent.workbench.read(
                session.context, response["model"]["context"]["historical_artifact_id"]
            )
            expected_timestamps = []
            point = HISTORY_START
            while point < HISTORY_END:
                expected_timestamps.append(point.isoformat().replace("+00:00", "Z"))
                point += INTERVAL
            assert [row["timestamp"] for row in history_context.data] == expected_timestamps

            archive_requests = [
                request for request in requests if request.url.path == "/v1/archive"
            ]
            forecast_requests = [
                request for request in requests if request.url.path == "/v1/forecast"
            ]
            assert len(archive_requests) > 1
            assert len(forecast_requests) == 1
            source_chunks = [
                item for item in history_context.provenance if item.get("endpoint") == "/v1/archive"
            ]
            assert len(source_chunks) == len(archive_requests)
            chunk_windows = [
                (_instant(item["requested_start"]), _instant(item["requested_end"]))
                for item in source_chunks
            ]
            assert chunk_windows[0][0] == HISTORY_START
            assert chunk_windows[-1][1] == HISTORY_END
            assert all(
                timedelta(0) < end - start <= timedelta(days=30, hours=1)
                for start, end in chunk_windows
            )
            assert all(
                left[1] == right[0]
                for left, right in zip(chunk_windows, chunk_windows[1:], strict=False)
            )
            combined = next(
                item
                for item in history_context.provenance
                if item.get("operation") == "combine_temperature_context"
            )
            assert combined["chunk_count"] == len(archive_requests)
            assert len(combined["inputs"]) == len(archive_requests)


@pytest.mark.parametrize("mode", ["auto", "required"])
async def test_middle_archive_chunk_failure_keeps_partial_context_evidence(tmp_path, mode):
    data = tmp_path / "data"
    data.mkdir()
    _write_history(data / "history.csv")
    requests = []
    failed_chunk = 9

    def handler(request):
        requests.append(request)
        assert request.url.params["latitude"] == "52.52"
        assert request.url.params["longitude"] == "13.41"
        if request.url.path == "/v1/archive":
            archive_index = sum(item.url.path == "/v1/archive" for item in requests) - 1
            if archive_index == failed_chunk:
                return httpx.Response(429)
        return httpx.Response(200, json=_weather_payload(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", _configuration(), data_root=data) as energy:
            energy.agent.calendar_clock = lambda: FROZEN_NOW
            energy.agent.http = transport
            session = energy.session("owner", "site")
            response = await _run(
                session,
                "sdk",
                {
                    "start": FORECAST_START.isoformat(),
                    "end": FORECAST_END.isoformat(),
                    "history_months": 18,
                    "context_mode": mode,
                },
            )

            resolution = _context_resolution(response)
            assert resolution["status"] == "unavailable"
            assert resolution["error"]["code"] == "rate_limited"
            successful_chunks = [
                attempt
                for attempt in resolution["attempts"]
                if attempt.get("role") == "historical_context"
                and attempt.get("retrieval", {}).get("ok")
            ]
            assert len(successful_chunks) == failed_chunk
            assert (
                len([request for request in requests if request.url.path == "/v1/archive"])
                == failed_chunk + 1
            )
            assert not any(request.url.path == "/v1/forecast" for request in requests)

            if mode == "auto":
                assert response["ok"], response
                assert not response["model"]["context_used"]
                assert response["model"]["context"]["evaluated"] is False
            else:
                assert not response["ok"]
                assert response["error"]["code"] == "context_unavailable"


async def test_weather_chunk_boundaries_preserve_fractional_offset_grid(tmp_path):
    from zoneinfo import ZoneInfo

    from energy_agent_tools.forecast_context import fetch_forecast_context

    zone = ZoneInfo("Asia/Kathmandu")
    left = datetime(2025, 4, 2, tzinfo=zone)
    cutoff = datetime(2026, 10, 2, tzinfo=zone)
    future = cutoff + timedelta(days=1)
    config = _configuration()
    config["sites"][0]["timezone"] = "Asia/Kathmandu"
    config.pop("bindings")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_weather_payload(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", config) as energy:
            energy.agent.http = transport
            session = energy.session("owner", "site")
            references, evidence = await fetch_forecast_context(
                energy.agent,
                session.context,
                history_start=left.isoformat(),
                history_end=cutoff.isoformat(),
                forecast_start=future.isoformat(),
                forecast_end=(future + timedelta(days=8)).isoformat(),
                interval_minutes=30,
                arguments={},
                account_ids={},
                tools={},
            )
            assert evidence["status"] == "available", evidence
            history = energy.agent.workbench.read(session.context, references["historical_context"])
            points = [_instant(row["timestamp"]) for row in history.data]
            assert points[0] == left.astimezone(UTC)
            assert points[-1] + INTERVAL == cutoff.astimezone(UTC)
            assert all(b - a == INTERVAL for a, b in zip(points, points[1:], strict=False))
            assert len(points) == int((cutoff - left) / INTERVAL)
            source_chunks = [
                item for item in history.provenance if item.get("endpoint") == "/v1/archive"
            ]
            assert len(source_chunks) == 19
            assert all(
                _instant(item["requested_end"]) - _instant(item["requested_start"])
                <= timedelta(days=30, hours=1)
                for item in source_chunks
            )
            assert _instant(source_chunks[0]["requested_start"]) == left.astimezone(UTC).replace(
                minute=0
            )
            assert history.kind.value == "estimated"
