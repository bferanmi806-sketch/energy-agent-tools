from __future__ import annotations

import json

import httpx
import pytest

from energy_agent_tools.connectors.tesla_energy import (
    TESLA_ENERGY_DOCS,
    probe,
    register,
)
from energy_agent_tools.models import (
    Action,
    ConnectedAccount,
    DataKind,
    EnergyError,
    ExecutionContext,
    Session,
)
from energy_agent_tools.registry import Registry

_SITE_ID = "1234567-00-R--EY132456789F4N"
_TOKEN = "test-tesla-oauth-token"
_ORIGIN = "https://fleet-api.prd.eu.vn.cloud.tesla.com"


def _account(*, region: str = "eu", resource_id: str | int = _SITE_ID) -> ConnectedAccount:
    return ConnectedAccount(
        id="tesla-account",
        user_id="user-1",
        toolkit="tesla-energy",
        site_id="gateway-site-1",
        settings={"region": region, "resource_id": resource_id},
    )


def _context(
    transport: httpx.MockTransport,
    *,
    account: ConnectedAccount | None = None,
    credential: str | None = _TOKEN,
) -> ExecutionContext:
    return ExecutionContext(
        session=Session(id="session-1", user_id="user-1"),
        account=account or _account(),
        credential=credential,
        http=httpx.AsyncClient(transport=transport),
        workbench=None,
        asset_id="caller-selected-asset",
        site_id="caller-selected-site",
    )


def _registry() -> Registry:
    registry = Registry()
    register(registry)
    return registry


def _site_info(site_id: str = _SITE_ID) -> dict:
    return {
        "response": {
            "id": site_id,
            "site_name": "Home Power",
            "installation_time_zone": "Europe/London",
            "nameplate_power": 10000,
            "nameplate_energy": 27000,
            "backup_reserve_percent": 20,
            "components": {
                "solar": True,
                "battery": True,
                "grid": True,
                "backup": True,
                "storm_mode_capable": True,
            },
        }
    }


def _live_status() -> dict:
    return {
        "response": {
            "solar_power": 1550,
            "energy_left": 18000,
            "total_pack_energy": 27000,
            "percentage_charged": 66.7,
            "battery_power": -250,
            "load_power": 1300,
            "grid_status": "Active",
            "grid_services_active": False,
            "grid_power": 0,
            "grid_services_power": 0,
            "generator_power": 0,
            "island_status": "on_grid",
            "storm_mode_active": False,
            "timestamp": "2026-09-30T17:11:27Z",
            "wall_connectors": None,
        }
    }


@pytest.mark.asyncio
async def test_registers_two_read_only_account_tools_without_caller_site_fields():
    registry = _registry()
    assert "tesla-energy" in registry.toolkits
    assert registry.toolkits["tesla-energy"].docs_url == TESLA_ENERGY_DOCS
    assert set(registry.handlers) == {
        "tesla_energy.get_site_info",
        "tesla_energy.get_live_status",
    }
    for name in registry.handlers:
        tool = registry.tools[name]
        assert tool.resource_scope == "account"
        assert tool.actions == {Action.READ, Action.EXTERNAL}
        assert tool.input_schema == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        assert "credential" not in json.dumps(tool.input_schema).lower()


@pytest.mark.asyncio
async def test_site_info_uses_account_site_and_eu_origin_and_reports_units_and_provenance():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "fleet-api.prd.eu.vn.cloud.tesla.com"
        assert request.url.path == f"/api/1/energy_sites/{_SITE_ID}/site_info"
        assert request.headers["authorization"] == f"Bearer {_TOKEN}"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, json=_site_info(), request=request)

    ctx = _context(httpx.MockTransport(handler))
    try:
        result = await _registry().handlers["tesla_energy.get_site_info"]({}, ctx)
        assert len(seen) == 1
        assert result.data["components"]["battery"] is True
        assert result.kind is DataKind.METERED
        assert result.unit == "mixed"
        assert result.site_id == "gateway-site-1"
        assert result.data["id"] == _SITE_ID
        assert result.field_units == {
            "nameplate_power": "W",
            "nameplate_energy": "Wh",
            "backup_reserve_percent": "%",
        }
        assert result.provenance == [
            {
                "provider": "Tesla Fleet API",
                "endpoint": f"/api/1/energy_sites/{_SITE_ID}/site_info",
                "documentation": TESLA_ENERGY_DOCS,
            }
        ]
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_live_status_preserves_provider_values_and_labels_watts_wh_and_percent():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fleet-api.prd.eu.vn.cloud.tesla.com"
        assert request.url.path == f"/api/1/energy_sites/{_SITE_ID}/live_status"
        return httpx.Response(200, json=_live_status(), request=request)

    ctx = _context(httpx.MockTransport(handler))
    try:
        result = await _registry().handlers["tesla_energy.get_live_status"]({}, ctx)
        assert result.data["solar_power"] == 1550
        assert result.data["percentage_charged"] == 66.7
        assert result.data["grid_status"] == "Active"
        assert result.kind is DataKind.METERED
        assert result.unit == "mixed"
        assert result.site_id == "gateway-site-1"
        assert result.quantity_shape == "instantaneous"
        assert result.time_start.isoformat() == "2026-09-30T17:11:27+00:00"
        assert result.time_end == result.time_start
        assert result.field_units == {
            "solar_power": "W",
            "energy_left": "Wh",
            "total_pack_energy": "Wh",
            "percentage_charged": "%",
            "battery_power": "W",
            "load_power": "W",
            "grid_power": "W",
            "grid_services_power": "W",
            "generator_power": "W",
        }
        assert result.provenance[0]["endpoint"].endswith("/live_status")
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "location", "code"),
    [
        (401, None, "authentication_failed"),
        (403, None, "authentication_failed"),
        (429, None, "rate_limited"),
        (302, "https://attacker.example/collect", "unexpected_redirect"),
        (503, None, "provider_unavailable"),
    ],
)
async def test_http_errors_are_safe_and_redirects_are_not_followed(status, location, code):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"Location": location} if location else None
        return httpx.Response(status, headers=headers, text=_TOKEN, request=request)

    ctx = _context(httpx.MockTransport(handler))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_site_info"]({}, ctx)
        assert caught.value.code == code
        assert _TOKEN not in caught.value.message
        assert len(seen) == 1
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b'{"response": []}',
        b'{"response":{"solar_power":NaN}}',
        b'{"response":{"solar_power":1e9999}}',
    ],
)
async def test_malformed_json_and_non_finite_measurements_are_rejected(content):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=content, request=request)
    )
    ctx = _context(transport)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_live_status"]({}, ctx)
        assert caught.value.code == "malformed_response"
        assert _TOKEN not in caught.value.message
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_site_id_mismatch_is_rejected_and_caller_asset_cannot_override_account_site():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_site_info("someone-elses-site"), request=request)

    ctx = _context(httpx.MockTransport(handler))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_site_info"]({}, ctx)
        assert caught.value.code == "site_scope_mismatch"
        assert seen == [f"/api/1/energy_sites/{_SITE_ID}/site_info"]
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region", "resource_id"),
    [
        ("https://attacker.example", _SITE_ID),
        ("na;evil", _SITE_ID),
        ("eu", "../../other-site"),
    ],
)
async def test_untrusted_region_or_site_setting_fails_before_network(region, resource_id):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500, request=request)

    ctx = _context(
        httpx.MockTransport(handler),
        account=_account(region=region, resource_id=resource_id),
    )
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_live_status"]({}, ctx)
        assert caught.value.code == "invalid_settings"
        assert not called
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_caller_cannot_supply_site_or_asset_override():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_live_status(), request=request)

    ctx = _context(httpx.MockTransport(handler))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_live_status"](
                {"energy_site_id": "other-site", "asset_id": "other-asset"}, ctx
            )
        assert caught.value.code == "invalid_input"
        assert not called
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_probe_accepts_matching_site_info_and_raises_on_provider_failure():
    good = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_site_info(), request=request)
        )
    )
    wrong = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_site_info("wrong-site-id"), request=request)
        )
    )
    denied = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text=_TOKEN, request=request)
        )
    )
    try:
        assert await probe(good, _account(), _TOKEN)
        for invalid_http, invalid_account in (
            (wrong, _account()),
            (denied, _account()),
            (good, _account(region="bogus")),
        ):
            with pytest.raises(EnergyError) as caught:
                await probe(invalid_http, invalid_account, _TOKEN)
            assert caught.value.code == "provider_verification_failed"
            assert _TOKEN not in caught.value.message
    finally:
        await good.aclose()
        await wrong.aclose()
        await denied.aclose()


@pytest.mark.asyncio
async def test_probe_raises_sanitized_failure_for_malformed_site_info():
    malformed = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"response": {"site_name": "no id"}}, request=request
            )
        )
    )
    try:
        with pytest.raises(EnergyError) as caught:
            await probe(malformed, _account(), _TOKEN)
        assert caught.value.code == "provider_verification_failed"
        assert _TOKEN not in caught.value.message
    finally:
        await malformed.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", ["12345", 12345])
async def test_integer_provider_site_id_matches_selected_site_and_stays_provider_data(resource_id):
    account = _account(resource_id=resource_id)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1/energy_sites/12345/site_info"
        return httpx.Response(200, json={"response": {"id": 12345}}, request=request)

    ctx = _context(httpx.MockTransport(handler), account=account)
    try:
        result = await _registry().handlers["tesla_energy.get_site_info"]({}, ctx)
        assert result.site_id == "gateway-site-1"
        assert result.data["id"] == 12345
        assert result.provenance[0]["endpoint"] == "/api/1/energy_sites/12345/site_info"
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [True, 12345.0])
async def test_non_integer_numeric_provider_site_ids_are_malformed(response_id):
    account = _account(resource_id="12345")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": {"id": response_id}}, request=request)
    )
    ctx = _context(transport, account=account)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["tesla_energy.get_site_info"]({}, ctx)
        assert caught.value.code == "malformed_response"
    finally:
        await ctx.http.aclose()
