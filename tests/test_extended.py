from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

import energy_agent_tools.connectors.extended as extended
from energy_agent_tools.connectors.extended import register, register_energyplus
from energy_agent_tools.models import ConnectedAccount, EnergyError, ExecutionContext, Session
from energy_agent_tools.registry import Registry


def _ctx(
    transport: httpx.AsyncBaseTransport,
    *,
    toolkit: str | None = None,
    credential: str | None = None,
) -> ExecutionContext:
    account = (
        ConnectedAccount(id="a1", user_id="u1", toolkit=toolkit or "test")
        if toolkit is not None
        else None
    )
    return ExecutionContext(
        session=Session(id="s1", user_id="u1"),
        account=account,
        credential=credential,
        http=httpx.AsyncClient(transport=transport),
        workbench=None,
    )


def _registry(*, data_root: Path | None = None) -> Registry:
    registry = Registry()
    register(registry, data_root=data_root)
    return registry


@pytest.mark.asyncio
async def test_registers_bounded_tool_contracts_without_secret_fields() -> None:
    registry = _registry()

    assert {
        "neso",
        "electricitymaps",
        "entsoe",
        "windpowerlib",
        "sqlite",
    } <= set(registry.toolkits)
    assert {
        "neso.search_datasets",
        "neso.query_dataset",
        "electricitymaps.get_signal",
        "entsoe.get_timeseries",
        "windpowerlib.estimate_generation",
        "sqlite.read_timeseries",
    } <= set(registry.tools)
    for tool in registry.tools.values():
        serialized = str(tool.input_schema).lower()
        assert "credential" not in serialized
        assert "securitytoken" not in serialized
        assert "sql" not in serialized or tool.name == "sqlite.read_timeseries"
        assert tool.version == "1.0.0"
    assert registry.tools["neso.query_dataset"].result_kind is None
    assert registry.tools["electricitymaps.get_signal"].result_kind is None
    assert registry.tools["entsoe.get_timeseries"].result_kind is None
    assert registry.tools["sqlite.read_timeseries"].result_kind is None
    assert "get_carbon_intensity" in registry.tools["electricitymaps.get_signal"].capabilities
    assert "get_grid_load" in registry.tools["entsoe.get_timeseries"].capabilities
    assert (
        "estimate_wind_generation"
        in registry.tools["windpowerlib.estimate_generation"].capabilities
    )


@pytest.mark.asyncio
async def test_neso_uses_package_and_datastore_actions_without_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extended, "_NESO_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(extended, "_NESO_DATASTORE_INTERVAL_SECONDS", 0.0)
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path.endswith("package_show"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "title": "Embedded wind forecast",
                        "resources": [{"id": "resource-1", "datastore_active": True}],
                    },
                },
                request=request,
            )
        assert request.url.path.endswith("datastore_search")
        assert "sql" not in request.url.params
        assert request.url.params["filters"] == '{"SETTLEMENT_PERIOD":1}'
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "fields": [{"id": "value", "info": {"unit": "MW"}}],
                    "records": [{"DATE_GMT": "2026-01-01", "TIME_GMT": "00:00", "value": 12}],
                },
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler))
    result = await _registry().handlers["neso.query_dataset"](
        {
            "dataset": "embedded-wind-and-solar-forecasts",
            "filters": {"SETTLEMENT_PERIOD": 1},
        },
        ctx,
    )
    assert len(seen) == 2
    assert result.unit == "MW"
    assert result.kind.value == "forecast"
    assert result.data["resource_id"] == "resource-1"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_neso_time_scope_drops_rows_without_parseable_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extended, "_NESO_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(extended, "_NESO_DATASTORE_INTERVAL_SECONDS", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("package_show"):
            payload = {
                "success": True,
                "result": {"resources": [{"id": "r", "datastore_active": True}]},
            }
        else:
            payload = {
                "success": True,
                "result": {
                    "fields": [],
                    "records": [
                        {"timestamp": "2026-01-01T00:30:00Z", "value": 1},
                        {"timestamp": "not-a-time", "value": 2},
                        {"timestamp": "2026-01-01T03:00:00Z", "value": 3},
                    ],
                },
            }
        return httpx.Response(200, json=payload, request=request)

    ctx = _ctx(httpx.MockTransport(handler))
    result = await _registry().handlers["neso.query_dataset"](
        {
            "dataset": "forecast",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
        },
        ctx,
    )
    assert result.data["rows"] == [{"timestamp": "2026-01-01T00:30:00Z", "value": 1}]
    assert any("no parseable timestamp" in warning for warning in result.warnings)
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_electricity_maps_sends_fixed_v4_auth_header_and_flattens_mix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.electricitymaps.com"
        assert request.url.path == "/v4/electricity-mix/latest"
        assert request.headers["auth-token"] == "secret-token"
        return httpx.Response(
            200,
            json={
                "zone": "GB",
                "unit": "MW",
                "data": [
                    {
                        "datetime": "2026-01-01T00:00:00Z",
                        "mix": {"wind": 20, "battery storage": {"charge": 2, "discharge": 1}},
                        "isEstimated": False,
                    }
                ],
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), toolkit="electricitymaps", credential="secret-token")
    result = await _registry().handlers["electricitymaps.get_signal"](
        {"zone": "GB", "signal": "electricity-mix"}, ctx
    )
    assert [row["variable"] for row in result.data["rows"]] == [
        "wind",
        "battery storage.charge",
        "battery storage.discharge",
    ]
    assert result.unit == "MW"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_electricity_maps_past_range_uses_documented_time_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/carbon-intensity/past-range"
        assert request.url.params["start"] == "2026-01-01T00:00:00Z"
        assert request.url.params["end"] == "2026-01-01T02:00:00Z"
        assert request.url.params["flowTraced"] == "false"
        return httpx.Response(
            200,
            json={
                "zone": "GB",
                "data": [{"datetime": "2026-01-01T00:00:00Z", "carbonIntensity": 100}],
            },
            request=request,
        )

    ctx = _ctx(httpx.MockTransport(handler), toolkit="electricitymaps", credential="secret-token")
    result = await _registry().handlers["electricitymaps.get_signal"](
        {
            "zone": "GB",
            "mode": "past-range",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T02:00:00Z",
            "flow_traced": False,
        },
        ctx,
    )
    assert result.data["rows"][0]["value"] == 100
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_electricity_maps_auth_failure_does_not_echo_credential() -> None:
    secret = "do-not-log-this"
    transport = httpx.MockTransport(lambda request: httpx.Response(401, request=request))
    ctx = _ctx(transport, toolkit="electricitymaps", credential=secret)
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["electricitymaps.get_signal"]({"zone": "GB"}, ctx)
    assert caught.value.code == "authentication_failed"
    assert secret not in caught.value.message
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_entsoe_xml_positions_are_preserved_and_missing_is_null() -> None:
    body = b"""
    <Publication_MarketDocument xmlns="urn:entsoe">
      <TimeSeries>
        <mRID>series-1</mRID>
        <inBiddingZone_Domain.mRID>10YGB-TEST</inBiddingZone_Domain.mRID>
        <Period>
          <timeInterval><start>2026-01-01T00:00:00Z</start><end>2026-01-01T00:45:00Z</end></timeInterval>
          <resolution>PT15M</resolution>
          <Point><position>1</position><quantity>10</quantity></Point>
          <Point><position>3</position><quantity>12</quantity></Point>
        </Period>
      </TimeSeries>
    </Publication_MarketDocument>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "web-api.tp.entsoe.eu"
        assert request.url.params["securityToken"] == "secret-token"
        assert request.url.params["outBiddingZone_Domain"] == "10YGB-TEST"
        return httpx.Response(200, content=body, request=request)

    ctx = _ctx(httpx.MockTransport(handler), toolkit="entsoe", credential="secret-token")
    result = await _registry().handlers["entsoe.get_timeseries"](
        {
            "domain": "10YGB-TEST",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
        },
        ctx,
    )
    rows = result.data["rows"]
    assert [row["position"] for row in rows] == [1, 2, 3]
    assert rows[1]["value"] is None
    assert result.kind.value == "metered"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_entsoe_rejects_dtd_and_entity_declarations() -> None:
    body = b'<!DOCTYPE x [<!ENTITY secret "bad">]><Publication_MarketDocument />'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, request=request)
    )
    ctx = _ctx(transport, toolkit="entsoe", credential="secret-token")
    with pytest.raises(EnergyError) as caught:
        await _registry().handlers["entsoe.get_timeseries"](
            {
                "domain": "10YGB-TEST",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T01:00:00Z",
            },
            ctx,
        )
    assert caught.value.code == "unsafe_xml"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_sqlite_reads_inside_operator_root_and_rejects_escape(tmp_path: Path) -> None:
    database = tmp_path / "readings.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE readings (ts TEXT, value REAL)")
    connection.executemany(
        "INSERT INTO readings VALUES (?, ?)",
        [("2026-01-01T00:00:00Z", 1.0), ("2026-01-01T01:00:00Z", 2.0)],
    )
    connection.commit()
    connection.close()
    ctx = _ctx(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    result = await _registry(data_root=tmp_path).handlers["sqlite.read_timeseries"](
        {
            "path": "readings.db",
            "table": "readings",
            "timestamp_column": "ts",
            "value_column": "value",
            "unit": "kWh",
        },
        ctx,
    )
    assert [row["value"] for row in result.data] == [1.0, 2.0]
    with pytest.raises(EnergyError) as caught:
        await _registry(data_root=tmp_path).handlers["sqlite.read_timeseries"](
            {
                "path": "../readings.db",
                "table": "readings",
                "timestamp_column": "ts",
                "value_column": "value",
            },
            ctx,
        )
    assert caught.value.code == "path_forbidden"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_sqlite_applies_declared_timezone_to_naive_timestamps(tmp_path: Path) -> None:
    database = tmp_path / "local.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE readings (ts TEXT, value REAL)")
    connection.execute("INSERT INTO readings VALUES ('2026-01-01T00:00:00', 3)")
    connection.commit()
    connection.close()
    ctx = _ctx(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    result = await _registry(data_root=tmp_path).handlers["sqlite.read_timeseries"](
        {
            "path": "local.db",
            "table": "readings",
            "timestamp_column": "ts",
            "value_column": "value",
            "timezone": "Europe/London",
        },
        ctx,
    )
    assert result.data[0]["timestamp"] == "2026-01-01T00:00:00Z"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_sqlite_time_scope_normalizes_offset_timestamps_before_filtering(
    tmp_path: Path,
) -> None:
    database = tmp_path / "offsets.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE readings (ts TEXT, value REAL)")
    connection.executemany(
        "INSERT INTO readings VALUES (?, ?)",
        [
            ("2026-01-01T01:30:00+01:00", 1.0),
            ("2026-01-01T01:00:00Z", 2.0),
        ],
    )
    connection.commit()
    connection.close()
    ctx = _ctx(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    result = await _registry(data_root=tmp_path).handlers["sqlite.read_timeseries"](
        {
            "path": "offsets.db",
            "table": "readings",
            "timestamp_column": "ts",
            "value_column": "value",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
        },
        ctx,
    )
    assert [row["value"] for row in result.data] == [1.0]
    assert result.data[0]["timestamp"] == "2026-01-01T00:30:00Z"
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_windpowerlib_runs_explicit_offline_power_curve_when_installed() -> None:
    pytest.importorskip("windpowerlib")
    registry = _registry()
    ctx = _ctx(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    result = await registry.handlers["windpowerlib.estimate_generation"](
        {
            "hub_height_m": 80,
            "power_curve": [
                {"wind_speed_m_s": 0, "power_kw": 0},
                {"wind_speed_m_s": 4, "power_kw": 0},
                {"wind_speed_m_s": 8, "power_kw": 500},
                {"wind_speed_m_s": 12, "power_kw": 1000},
                {"wind_speed_m_s": 25, "power_kw": 0},
            ],
            "weather_rows": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "wind_speed_m_s": 8,
                    "measurement_height_m": 10,
                },
                {
                    "timestamp": "2026-01-01T01:00:00Z",
                    "wind_speed_m_s": 10,
                    "measurement_height_m": 10,
                },
            ],
        },
        ctx,
    )
    assert result.kind.value == "estimated"
    assert result.data["intervals"][0]["power_kw"] > 0
    await ctx.http.aclose()


def test_energyplus_requires_operator_paths(tmp_path: Path) -> None:
    registry = Registry()
    with pytest.raises(ValueError):
        register_energyplus(registry, tmp_path / "missing-energyplus", tmp_path)
