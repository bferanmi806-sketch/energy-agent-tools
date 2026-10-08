"""Read-only Enphase Enlighten Systems API v4 summary connector."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ..models import (
    Action,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    Toolkit,
    schema,
)
from ..registry import Registry

TOOLKIT_ID = "enphase-energy"
API_BASE = "https://api.enphaseenergy.com/api/v4"
SUMMARY_DOCS = "https://developer-v4.enphase.com/docs/monitoring_api"
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
_SYSTEM_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")
_CONFIGURATION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_SUMMARY_NUMBERS = {
    "current_power",
    "energy_lifetime",
    "energy_today",
    "last_interval_end_at",
    "last_report_at",
    "modules",
    "operational_at",
    "size_w",
    "stage",
    "battery_charge_w",
    "battery_discharge_w",
    "battery_capacity_wh",
}
_SUMMARY_TIMESTAMPS = {"last_interval_end_at", "last_report_at", "operational_at"}
_SUMMARY_TEXT = {"source", "status", "summary_date"}
_FIELD_UNITS = {
    "current_power": "W",
    "energy_lifetime": "Wh",
    "energy_today": "Wh",
    "last_interval_end_at": "unix s",
    "last_report_at": "unix s",
    "modules": "count",
    "operational_at": "unix s",
    "size_w": "W",
    "stage": "stage",
    "battery_charge_w": "W",
    "battery_discharge_w": "W",
    "battery_capacity_wh": "Wh",
}


def _error(code: str, message: str, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _system_id(account: ConnectedAccount | None) -> int:
    if account is None:
        raise _error("account_required", "A connected Enphase account is required.")
    if account.toolkit != TOOLKIT_ID:
        raise _error("account_mismatch", "Select a connected Enphase account.")
    settings = account.settings
    if not isinstance(settings, Mapping):
        raise _error("invalid_settings", "The Enphase account settings are invalid.")
    value = settings.get("resource_id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _error("invalid_resource_id", "The Enphase account has no valid system ID.")
    text = str(value)
    if not _SYSTEM_ID_PATTERN.fullmatch(text):
        raise _error("invalid_resource_id", "The Enphase account has no valid system ID.")
    try:
        identifier = int(text)
    except ValueError:
        raise _error("invalid_resource_id", "The Enphase account has no valid system ID.") from None
    if identifier <= 0:
        raise _error("invalid_resource_id", "The Enphase account has no valid system ID.")
    return identifier


def _managed_configuration_id(account: ConnectedAccount | None) -> str:
    if account is None:
        raise _error("account_required", "A connected Enphase account is required.")
    if account.toolkit != TOOLKIT_ID:
        raise _error("account_mismatch", "Select a connected Enphase account.")
    value = account.settings.get("managed_oauth_configuration_id")
    if not isinstance(value, str) or not _CONFIGURATION_ID_PATTERN.fullmatch(value):
        raise _error(
            "oauth_configuration_unavailable",
            "The Enphase application configuration is unavailable.",
        )
    return value


def _secret(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise _error("credential_required", f"A valid {name} is required.")
    return value


def _parse_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def _decode_json(content: bytes) -> Any:
    try:
        return json.loads(content, parse_constant=_parse_constant)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise _error("malformed_response", "Enphase returned invalid JSON.") from None


def _status_error(status: int) -> EnergyError:
    if 300 <= status < 400:
        return _error("unexpected_redirect", "Enphase returned an unexpected redirect.")
    if status in {401, 403}:
        return _error("authentication_failed", "Enphase rejected the configured credentials.")
    if status == 404:
        return _error("provider_not_found", "The configured Enphase system was not found.")
    if status == 429:
        return _error("rate_limited", "The Enphase API rate limit was reached.", True)
    if status >= 500:
        return _error("provider_unavailable", "The Enphase API returned a server error.", True)
    return _error("provider_error", "Enphase rejected the request.")


def _same_enphase_origin(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == "api.enphaseenergy.com"
        and url.port in {None, 443}
        and not url.username
        and not url.password
    )


async def _request_summary(
    http: httpx.AsyncClient,
    system_id: int,
    credential: str,
    api_key: str,
) -> Any:
    url = f"{API_BASE}/systems/{system_id}/summary"
    try:
        async with http.stream(
            "GET",
            url,
            params={"key": api_key},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {credential}",
            },
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if not _same_enphase_origin(response.url):
                raise _error("unsafe_response", "The Enphase response used an unexpected origin.")
            if response.status_code != 200:
                raise _status_error(response.status_code)
            if response.headers.get("content-encoding", "identity").lower() not in {
                "",
                "identity",
            }:
                raise _error("malformed_response", "Enphase returned an unsupported encoding.")

            if response.is_stream_consumed:
                # Mock transports and custom in-memory transports may provide a
                # pre-buffered response; live network responses stay streamed.
                content = response.content
                if len(content) > MAX_RESPONSE_BYTES:
                    raise _error("result_too_large", "Enphase returned too much summary data.")
            else:
                bounded_content = bytearray()
                async for chunk in response.aiter_raw():
                    if len(bounded_content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _error("result_too_large", "Enphase returned too much summary data.")
                    bounded_content.extend(chunk)
                content = bytes(bounded_content)
    except httpx.RequestError:
        raise _error(
            "provider_unavailable", "The Enphase API could not be reached.", True
        ) from None

    return _decode_json(content)


def _numeric(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("malformed_response", f"Enphase returned an invalid {field} value.")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise _error("malformed_response", f"Enphase returned an invalid {field} value.")
    return value


def _timestamp(value: Any, field: str) -> int | None:
    result = _numeric(value, field)
    if result is None:
        return None
    if isinstance(result, float) and not result.is_integer():
        raise _error("malformed_response", f"Enphase returned an invalid {field} timestamp.")
    seconds = int(result)
    try:
        datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        raise _error(
            "malformed_response", f"Enphase returned an invalid {field} timestamp."
        ) from None
    return seconds


def _summary(payload: Any, requested_system_id: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _error("malformed_response", "Enphase returned an invalid system summary.")
    returned_id = payload.get("system_id")
    if isinstance(returned_id, bool) or not isinstance(returned_id, int) or returned_id <= 0:
        raise _error("malformed_response", "Enphase returned a summary without a valid system ID.")
    if returned_id != requested_system_id:
        raise _error(
            "resource_mismatch", "Enphase returned a different system than the connected account."
        )

    result: dict[str, Any] = {"system_id": returned_id}
    for field in _SUMMARY_NUMBERS - _SUMMARY_TIMESTAMPS:
        if field in payload:
            result[field] = _numeric(payload[field], field)
    for field in _SUMMARY_TIMESTAMPS:
        if field in payload:
            result[field] = _timestamp(payload[field], field)
    for field in _SUMMARY_TEXT:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise _error("malformed_response", f"Enphase returned an invalid {field} value.")
        if field == "summary_date":
            try:
                date.fromisoformat(value)
            except ValueError:
                raise _error(
                    "malformed_response", "Enphase returned an invalid summary date."
                ) from None
        result[field] = value
    return result


def _result(summary: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    return EnergyResult(
        data=summary,
        kind=DataKind.METERED,
        unit="mixed",
        source="enphase",
        provider="enphase",
        site_id=ctx.account.site_id if ctx.account is not None else None,
        original_unit="mixed",
        field_units={field: unit for field, unit in _FIELD_UNITS.items() if field in summary},
        quality="provider-reported system summary",
        provenance=[
            {
                "provider": "enphase",
                "endpoint": "/api/v4/systems/{system_id}/summary",
                "documentation": SUMMARY_DOCS,
            }
        ],
    )


async def probe(
    http: httpx.AsyncClient,
    account: ConnectedAccount,
    credential: str,
    *,
    api_key: str,
) -> bool:
    """Verify that the bearer credential can read the account's selected system."""

    system_id = _system_id(account)
    bearer = _secret(credential, "Enphase access token")
    application_key = _secret(api_key, "Enphase application key")
    payload = await _request_summary(http, system_id, bearer, application_key)
    if not isinstance(payload, dict):
        raise _error("malformed_response", "Enphase returned an invalid system summary.")
    returned_id = payload.get("system_id")
    return (
        isinstance(returned_id, int)
        and not isinstance(returned_id, bool)
        and returned_id == system_id
    )


def register(
    registry: Registry,
    *,
    api_keys: Mapping[str, str] | None = None,
) -> None:
    """Register account-scoped reads using operator-managed app keys by OAuth config ID."""

    configured_api_keys = dict(api_keys or {})
    registry.add_toolkit(
        Toolkit(
            id=TOOLKIT_ID,
            name="Enphase Energy",
            description="Read a connected Enphase system's current summary and energy values.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=SUMMARY_DOCS,
        )
    )

    async def get_summary(arguments: Json, ctx: ExecutionContext) -> EnergyResult:
        if arguments:
            raise _error("invalid_arguments", "Enphase summary does not accept target arguments.")
        system_id = _system_id(ctx.account)
        configuration_id = _managed_configuration_id(ctx.account)
        api_key = _secret(configured_api_keys.get(configuration_id), "Enphase application key")
        bearer = _secret(ctx.credential, "Enphase access token")
        payload = await _request_summary(ctx.http, system_id, bearer, api_key)
        return _result(_summary(payload, system_id), ctx)

    registry.add(
        Tool(
            name="enphase_energy.get_summary",
            toolkit=TOOLKIT_ID,
            resource_scope="account",
            description=(
                "Read current production, energy totals, system size and available battery "
                "summary values for the connected Enphase system."
            ),
            input_schema=schema({}),
            capabilities=[
                "get_generation",
                "get_current_power",
                "get_storage_state",
                "solar",
                "production",
            ],
            actions={Action.READ, Action.EXTERNAL},
            result_kind=DataKind.METERED,
            result_unit="mixed",
        ),
        get_summary,
    )
