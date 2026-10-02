from datetime import UTC, datetime, timedelta

import httpx
import pytest

from energy_agent_tools import EnergyAgentTools

LEFT = datetime(2026, 1, 1, tzinfo=UTC)
START = datetime(2026, 4, 1, tzinfo=UTC)
END = START + timedelta(days=8)


def temperature(point):
    return 10 + (point.date() - LEFT.date()).days * 0.15


def configure(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with (data / "history.csv").open("w") as stream:
        stream.write("timestamp,end,value\n")
        point = LEFT
        while point < START:
            edge = point + timedelta(minutes=30)
            stream.write(f"{point.isoformat()},{edge.isoformat()},{1 + temperature(point) * 0.1}\n")
            point = edge
    (data / "tariff.csv").write_text(
        "timestamp,end,value\n2026-03-01T00:00:00Z,2026-05-01T00:00:00Z,20\n"
    )
    return data, {
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
            },
            {
                "capability": "get_tariff",
                "tool": "CSV_READ_TIMESERIES",
                "reviewed": True,
                "kind": "forecast",
                "unit": "p/kWh",
                "quantity_shape": "interval",
                "fixed_arguments": {
                    "file": "tariff.csv",
                    "kind": "forecast",
                    "unit": "p/kWh",
                    "timezone": "UTC",
                    "quantity_shape": "interval",
                    "window_mode": "overlap",
                },
            },
        ],
    }


def weather_payload(request):
    query = request.url.params
    first = datetime.fromisoformat(query["start_date"]).replace(tzinfo=UTC)
    last = datetime.fromisoformat(query["end_date"]).replace(tzinfo=UTC) + timedelta(days=1)
    points = []
    while first < last:
        points.append(first)
        first += timedelta(hours=1)
    variables = query["hourly"].split(",")
    archive = request.url.path == "/v1/archive"
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
        hourly[variable] = [
            temperature(point) if variable == "temperature_2m" else 1 for point in points
        ]
    return {"hourly": hourly, "hourly_units": {variable: units[variable] for variable in variables}}


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize("gap_days", [0, 1])
async def test_forecast_bill_autonomously_fetches_and_preserves_weather_kinds(
    tmp_path, interface, gap_days
):
    data, config = configure(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["latitude"] == "52.52"
        assert request.url.params["longitude"] == "13.41"
        return httpx.Response(200, json=weather_payload(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
            energy.agent.http = transport
            session = energy.session("owner", "site")
            parameters = {
                "start": (START + timedelta(days=gap_days)).isoformat(),
                "end": (END + timedelta(days=gap_days)).isoformat(),
                "history_end": START.isoformat(),
                "context_mode": "required",
            }
            if interface == "sdk":
                response = await session.skill("forecast-bill", parameters)
            else:
                response = await session.dispatch(
                    "ENERGY_RUN_SKILL", {"skill_id": "forecast-bill", "parameters": parameters}
                )
            assert response["ok"], response
            assert response["context_resolution"]["status"] == "available"
            assert response["model"]["context_used"]
            assert response["model"]["context"]["historical_kind"] == "estimated"
            assert response["model"]["context"]["future_kind"] == "forecast"
            forecast = energy.agent.workbench.read(session.context, response["forecast_artifact"])
            assert forecast.kind.value == "forecast"
            assert "temperature_alignment" in str(forecast.provenance)
            assert any("zero-order hold" in assumption for assumption in forecast.assumptions)
            assert any("gridded" in warning for warning in forecast.warnings)
            assert any(
                "future weather forecast accuracy" in warning for warning in forecast.warnings
            )
            analysis = response["evidence"][-1]["analysis"]
            bill = energy.agent.workbench.read(
                session.context, analysis["result"]["data"]["artifact_id"]
            )
            assert (
                bill.kind.value == "calculated"
                and bill.data["calculation_basis"] == "forecast_consumption"
            )
            assert "metered" in str(bill.provenance) and "estimated" in str(bill.provenance)
    assert [request.url.path for request in requests] == ["/v1/archive"] * 3 + ["/v1/forecast"]
    assert requests[-2].url.params["end_date"] == "2026-03-31"
    assert (
        requests[-1].url.params["start_date"]
        == (START + timedelta(days=gap_days)).date().isoformat()
    )


@pytest.mark.parametrize("mode", ["auto", "required", "explicit"])
async def test_missing_weather_has_visible_fallback_or_required_refusal(tmp_path, mode):
    data, config = configure(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
            energy.agent.http = transport
            response = await energy.session("owner", "site").skill(
                "consumption-forecast",
                {
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                    "context_mode": mode,
                },
            )
            if mode == "required":
                assert not response["ok"] and response["error"]["code"] == "context_unavailable"
                assert (
                    response["evidence"][-1]["context_resolution"]["error"]["code"]
                    == "rate_limited"
                )
            else:
                assert response["ok"], response
                assert not response["model"]["context_used"]
                if mode == "auto":
                    assert response["context_resolution"]["error"]["code"] == "rate_limited"
                else:
                    assert response["context_resolution"]["status"] == "disabled"
    assert bool(requests) == (mode != "explicit")
    assert all(request.url.path == "/v1/archive" for request in requests)


async def test_forecast_context_refuses_other_site_coordinates_before_http(tmp_path):
    data, config = configure(tmp_path)
    config["bindings"].append(
        {
            "capability": "get_historical_weather",
            "tool": "open_meteo.get_historical_temperature",
            "reviewed": True,
            "kind": "estimated",
            "unit": "degC",
            "preference": 100,
            "fixed_arguments": {"latitude": 30.0},
        }
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=weather_payload(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
            energy.agent.http = transport
            response = await energy.session("owner", "site").skill(
                "consumption-forecast",
                {
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                },
            )
            assert response["ok"], response
            assert response["context_resolution"]["error"]["code"] == "context_location_mismatch"
            assert not response["model"]["context_used"]
    assert requests == []


async def test_auto_context_can_substitute_reviewed_file_providers(tmp_path):
    data, config = configure(tmp_path)
    for capability, kind, filename, left, right in (
        ("get_historical_weather", "estimated", "past.csv", LEFT, START),
        ("get_weather", "forecast", "future.csv", START, END),
    ):
        with (data / filename).open("w") as stream:
            stream.write("timestamp,temperature\n")
            point = left
            while point < right:
                stream.write(f"{point.isoformat()},{temperature(point)}\n")
                point += timedelta(hours=1)
        config["bindings"].append(
            {
                "capability": capability,
                "tool": "CSV_READ_TIMESERIES",
                "reviewed": True,
                "preference": 100,
                "kind": kind,
                "unit": "degC",
                "fixed_arguments": {
                    "file": filename,
                    "kind": kind,
                    "unit": "degC",
                    "timezone": "UTC",
                    "resolution": "1h",
                    "quantity_shape": "instantaneous",
                },
            }
        )
    async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
        session = energy.session("owner", "site")
        response = await session.skill(
            "consumption-forecast",
            {
                "start": START.isoformat(),
                "end": END.isoformat(),
                "context_mode": "required",
            },
        )
        assert response["ok"], response
        assert response["context_resolution"]["status"] == "available"
        assert response["model"]["context_used"]
        artifact = energy.agent.workbench.read(session.context, response["forecast_artifact"])
        assert "local-csv" in str(artifact.provenance)
