from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from energy_agent_tools.connectors import enphase_energy
from energy_agent_tools.models import (
    Action,
    ConnectedAccount,
    EnergyError,
    ExecutionContext,
    Session,
)
from energy_agent_tools.registry import Registry

SYSTEM_ID = 698910067
APP_KEY = "enphase-app-key-secret-for-test"
ACCESS_TOKEN = "enphase-access-token-secret-for-test"
CONFIGURATION_ID = "enphase-prod"


def _account(resource_id: Any = SYSTEM_ID) -> ConnectedAccount:
    return ConnectedAccount(
        id="enphase-account-1",
        user_id="alice",
        toolkit="enphase-energy",
        site_id="home",
        auth={"scheme": "oauth"},
        settings={
            "resource_id": resource_id,
            "managed_oauth_configuration_id": CONFIGURATION_ID,
        },
    )


def _registry(*, api_keys: dict[str, str] | None = None) -> Registry:
    registry = Registry()
    enphase_energy.register(
        registry,
        api_keys=api_keys if api_keys is not None else {CONFIGURATION_ID: APP_KEY},
    )
    return registry


def _context(
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    account: ConnectedAccount | None = None,
    credential: str | None = ACCESS_TOKEN,
) -> ExecutionContext:
    return ExecutionContext(
        session=Session(id="session-1", user_id="alice"),
        account=account or _account(),
        credential=credential,
        http=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        workbench=None,
    )


def _official_summary(system_id: int = SYSTEM_ID) -> dict[str, Any]:
    return {
        "system_id": system_id,
        "current_power": 4200,
        "energy_lifetime": 15872000,
        "energy_today": 19200,
        "last_interval_end_at": 1791460500,
        "last_report_at": 1791460530,
        "modules": 30,
        "operational_at": 1557400231,
        "size_w": 12500,
        "nmi": "meter-identifier",
        "source": "meter",
        "status": "normal",
        "summary_date": "2026-10-08",
        "battery_charge_w": 1280,
        "battery_discharge_w": 1280,
        "battery_capacity_wh": 3360,
        "evse_power": [{"21458935": {"evse_max_charge_w": 440, "evse_min_charge_w": 2230}}],
        "stage": 4,
    }


@pytest.mark.asyncio
async def test_summary_uses_the_account_system_and_keeps_energy_units_distinct():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_official_summary(), request=request)

    registry = _registry()
    ctx = _context(respond)
    try:
        result = await registry.handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.enphaseenergy.com"
    assert request.url.path == f"/api/v4/systems/{SYSTEM_ID}/summary"
    assert dict(request.url.params) == {"key": APP_KEY}
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Accept-Encoding"] == "identity"
    assert result.data["system_id"] == SYSTEM_ID
    assert result.data["energy_today"] == 19200
    assert result.data["current_power"] == 4200
    assert "nmi" not in result.data
    assert "evse_power" not in result.data
    assert result.field_units["energy_today"] == "Wh"
    assert result.field_units["energy_lifetime"] == "Wh"
    assert result.field_units["current_power"] == "W"
    assert result.field_units["size_w"] == "W"
    assert result.field_units["last_report_at"] == "unix s"
    assert result.unit == "mixed"
    assert result.site_id == "home"
    assert result.provider == "enphase"
    assert result.provenance == [
        {
            "provider": "enphase",
            "endpoint": "/api/v4/systems/{system_id}/summary",
            "documentation": enphase_energy.SUMMARY_DOCS,
        }
    ]
    tool = registry.get("enphase_energy.get_summary")
    assert tool.toolkit == "enphase-energy"
    assert tool.resource_scope == "account"
    assert tool.input_schema["properties"] == {}
    assert tool.actions == {Action.READ, Action.EXTERNAL}


@pytest.mark.asyncio
async def test_summary_drops_unknown_provider_fields_including_secret_shaped_values():
    payload = _official_summary()
    payload.update({"api_key": APP_KEY, "access_token": ACCESS_TOKEN, "private_extra": "hidden"})
    ctx = _context(lambda request: httpx.Response(200, json=payload, request=request))
    try:
        result = await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    serialized = result.model_dump_json()
    assert APP_KEY not in serialized
    assert ACCESS_TOKEN not in serialized
    assert "private_extra" not in serialized
    assert APP_KEY not in json.dumps(result.provenance)


@pytest.mark.asyncio
async def test_probe_verifies_the_system_id_returned_by_the_selected_summary():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"system_id": SYSTEM_ID}, request=request)

    account = _account(str(SYSTEM_ID))
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        verified = await enphase_energy.probe(http, account, ACCESS_TOKEN, api_key=APP_KEY)
    finally:
        await http.aclose()

    assert verified is True
    assert len(requests) == 1
    assert requests[0].url.path == f"/api/v4/systems/{SYSTEM_ID}/summary"
    assert dict(requests[0].url.params) == {"key": APP_KEY}


@pytest.mark.asyncio
async def test_probe_returns_false_if_the_summary_does_not_match_the_selected_system():
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"system_id": SYSTEM_ID + 1}, request=request)
        )
    )
    try:
        verified = await enphase_energy.probe(http, _account(), ACCESS_TOKEN, api_key=APP_KEY)
    finally:
        await http.aclose()

    assert verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_id", "expected_code"),
    [
        (None, "invalid_resource_id"),
        (True, "invalid_resource_id"),
        (0, "invalid_resource_id"),
        (1.5, "invalid_resource_id"),
        ("../698910067", "invalid_resource_id"),
        ("698910067?x=1", "invalid_resource_id"),
    ],
)
async def test_account_resource_id_is_a_bounded_numeric_path_segment(resource_id, expected_code):
    called = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_official_summary(), request=request)

    ctx = _context(respond, account=_account(resource_id))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code == expected_code
    assert not called


@pytest.mark.asyncio
async def test_summary_rejects_arguments_that_could_retarget_the_account():
    called = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_official_summary(), request=request)

    ctx = _context(respond)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"](
                {"system_id": SYSTEM_ID + 1}, ctx
            )
    finally:
        await ctx.http.aclose()

    assert caught.value.code == "invalid_arguments"
    assert not called


@pytest.mark.asyncio
async def test_asset_context_cannot_retarget_or_relabel_the_account_system():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_official_summary(), request=request)

    ctx = _context(respond)
    ctx.site_id = "other-site"
    ctx.asset_id = "asset-for-another-system"
    try:
        result = await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert requests[0].url.path == f"/api/v4/systems/{SYSTEM_ID}/summary"
    assert result.site_id == "home"
    assert result.asset_id is None


@pytest.mark.asyncio
async def test_toolkit_and_oauth_configuration_must_match_the_registered_account():
    called = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_official_summary(), request=request)

    wrong_toolkit = _account()
    wrong_toolkit.toolkit = "other-provider"
    ctx = _context(respond, account=wrong_toolkit)
    try:
        with pytest.raises(EnergyError) as mismatch:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()
    assert mismatch.value.code == "account_mismatch"
    assert not called

    no_configuration = _account()
    no_configuration.settings.pop("managed_oauth_configuration_id")
    ctx = _context(respond, account=no_configuration)
    try:
        with pytest.raises(EnergyError) as missing:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()
    assert missing.value.code == "oauth_configuration_unavailable"
    assert not called


@pytest.mark.asyncio
async def test_managed_oauth_configuration_selects_the_operator_application_key():
    configured_key = "key-for-selected-configuration"
    account = _account()
    account.settings["managed_oauth_configuration_id"] = "enphase-other"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == configured_key
        return httpx.Response(200, json=_official_summary(), request=request)

    ctx = _context(respond, account=account)
    try:
        result = await _registry(api_keys={"enphase-other": configured_key}).handlers[
            "enphase_energy.get_summary"
        ]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert result.data["system_id"] == SYSTEM_ID
    assert APP_KEY not in result.model_dump_json()
    assert configured_key not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (301, "unexpected_redirect"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (404, "provider_not_found"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
async def test_provider_errors_are_sanitized_and_never_follow_redirects(status, expected_code):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json={"message": APP_KEY, "details": ACCESS_TOKEN},
            headers={"location": "https://elsewhere.example/collect"},
            request=request,
        )

    ctx = _context(respond)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code == expected_code
    assert APP_KEY not in str(caught.value)
    assert ACCESS_TOKEN not in str(caught.value)
    assert len(requests) == 1
    assert requests[0].url.host == "api.enphaseenergy.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"system_id": str(SYSTEM_ID)},
        {"system_id": SYSTEM_ID + 1},
        {"system_id": SYSTEM_ID, "current_power": True},
        {"system_id": SYSTEM_ID, "current_power": float("nan")},
        {"system_id": SYSTEM_ID, "energy_today": float("inf")},
        {"system_id": SYSTEM_ID, "last_report_at": 10**1000},
        {"system_id": SYSTEM_ID, "summary_date": "not-a-date"},
    ],
)
async def test_summary_rejects_malformed_or_nonfinite_values(payload):
    body = json.dumps(payload, allow_nan=True).encode()
    ctx = _context(lambda request: httpx.Response(200, content=body, request=request))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code in {"malformed_response", "resource_mismatch"}
    assert APP_KEY not in str(caught.value)
    assert ACCESS_TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_nonstandard_json_constants_are_rejected():
    body = f'{{"system_id": {SYSTEM_ID}, "current_power": NaN}}'.encode()
    ctx = _context(lambda request: httpx.Response(200, content=body, request=request))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code == "malformed_response"


@pytest.mark.asyncio
async def test_response_byte_limit_is_enforced_while_streaming():
    body = b"{" + (b" " * enphase_energy.MAX_RESPONSE_BYTES)
    ctx = _context(lambda request: httpx.Response(200, content=body, request=request))
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code == "result_too_large"


@pytest.mark.asyncio
async def test_network_failures_do_not_copy_request_urls_or_secret_values_into_errors():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    ctx = _context(fail)
    try:
        with pytest.raises(EnergyError) as caught:
            await _registry().handlers["enphase_energy.get_summary"]({}, ctx)
    finally:
        await ctx.http.aclose()

    assert caught.value.code == "provider_unavailable"
    assert APP_KEY not in str(caught.value)
    assert ACCESS_TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_credentials_and_missing_application_keys_fail_before_network_io():
    called = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_official_summary(), request=request)

    for context, registry, expected in [
        (_context(respond, credential=None), _registry(), "credential_required"),
        (_context(respond), _registry(api_keys={}), "credential_required"),
    ]:
        try:
            with pytest.raises(EnergyError) as caught:
                await registry.handlers["enphase_energy.get_summary"]({}, context)
        finally:
            await context.http.aclose()
        assert caught.value.code == expected
        assert APP_KEY not in str(caught.value)
        assert ACCESS_TOKEN not in str(caught.value)

    assert not called
