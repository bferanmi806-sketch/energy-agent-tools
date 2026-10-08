"""Read-only access to a connected Tesla Energy site through the Fleet API."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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

TESLA_ENERGY_DOCS = "https://developer.tesla.com/docs/fleet-api/endpoints/energy"
_TOOLKIT_ID = "tesla-energy"
_SOURCE = "tesla-fleet-api"
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_JSON_NODES = 50_000
_MAX_JSON_DEPTH = 64
_TIMEOUT_SECONDS = 10.0
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,119}$")
_FLEET_ORIGINS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}

_FIELD_UNITS = {
    "solar_power": "W",
    "battery_power": "W",
    "load_power": "W",
    "grid_power": "W",
    "grid_services_power": "W",
    "generator_power": "W",
    "nameplate_power": "W",
    "max_site_meter_power_ac": "W",
    "min_site_meter_power_ac": "W",
    "energy_left": "Wh",
    "total_pack_energy": "Wh",
    "nameplate_energy": "Wh",
    "percentage_charged": "%",
    "backup_reserve_percent": "%",
    "vpp_backup_reserve_percent": "%",
}


def _error(code: str, message: str, *, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _selection(account: ConnectedAccount | None) -> tuple[str, str, str]:
    if account is None:
        raise _error("account_required", "A connected Tesla Energy account is required.")
    settings = account.settings
    if not isinstance(settings, Mapping):
        raise _error("invalid_settings", "Tesla Energy account settings are invalid.")

    resource_id = settings.get("resource_id")
    if isinstance(resource_id, int) and not isinstance(resource_id, bool) and resource_id >= 0:
        try:
            site_id = str(resource_id)
        except ValueError:
            site_id = ""
    elif isinstance(resource_id, str):
        site_id = resource_id
    else:
        site_id = ""
    if not _SITE_ID.fullmatch(site_id):
        raise _error("invalid_settings", "Tesla Energy account has no valid selected site.")

    region = settings.get("region")
    if not isinstance(region, str) or region not in _FLEET_ORIGINS:
        raise _error("invalid_settings", "Tesla Energy account has no supported Fleet API region.")

    return site_id, region, _FLEET_ORIGINS[region]


def _credential(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise _error("credential_required", "A Tesla Fleet API credential is required.")
    return value


def _check_finite_json(value: Any, *, credential: str, max_depth: int = _MAX_JSON_DEPTH) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > max_depth:
            raise _error(
                "malformed_response", "Tesla returned an oversized or deeply nested response."
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise _error("malformed_response", "Tesla returned a non-finite numeric value.")
        if isinstance(current, int) and not isinstance(current, bool):
            try:
                finite = math.isfinite(float(current))
            except OverflowError:
                finite = False
            if not finite:
                raise _error("malformed_response", "Tesla returned an out-of-range numeric value.")
        if isinstance(current, str) and credential in current:
            raise _error(
                "malformed_response", "Tesla returned content that cannot be safely exposed."
            )
        if isinstance(current, Mapping):
            if any(isinstance(key, str) and credential in key for key in current):
                raise _error(
                    "malformed_response", "Tesla returned content that cannot be safely exposed."
                )
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _parse_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON numeric constant")


async def _read_limited(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise _error(
                    "response_too_large", "Tesla returned a response above the size limit."
                )
        except ValueError:
            pass

    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
        if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise _error("response_too_large", "Tesla returned a response above the size limit.")
        content.extend(chunk)
    return bytes(content)


async def _request_response(
    http: httpx.AsyncClient,
    account: ConnectedAccount | None,
    credential: str | None,
    endpoint: str,
) -> tuple[dict[str, Any], str, str]:
    site_id, _region, origin = _selection(account)
    bearer = _credential(credential)
    path = f"/api/1/energy_sites/{quote(site_id, safe='-._~')}/{endpoint}"
    url = f"{origin}{path}"

    try:
        async with http.stream(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise _error("unexpected_redirect", "Tesla returned an unexpected redirect.")
            if response.status_code in {401, 403}:
                raise _error("authentication_failed", "Tesla rejected the configured credential.")
            if response.status_code == 404:
                raise _error("site_not_found", "Tesla could not find the selected energy site.")
            if response.status_code == 429:
                raise _error("rate_limited", "Tesla rate limited the request.", retryable=True)
            if response.status_code >= 500:
                raise _error(
                    "provider_unavailable",
                    "Tesla Fleet API returned a server error.",
                    retryable=True,
                )
            if response.status_code != 200:
                raise _error("provider_error", "Tesla Fleet API rejected the request.")

            try:
                response_url = response.url
                if (
                    response_url.scheme != "https"
                    or response_url.host != httpx.URL(origin).host
                    or response_url.port not in {None, 443}
                ):
                    raise _error(
                        "unsafe_response", "Tesla response came from an unexpected origin."
                    )
            except (RuntimeError, ValueError):
                raise _error(
                    "unsafe_response", "Tesla response origin could not be verified."
                ) from None

            raw = await _read_limited(response)
    except EnergyError:
        raise
    except httpx.RequestError:
        raise _error(
            "provider_unavailable", "Tesla Fleet API could not be reached.", retryable=True
        ) from None
    except Exception:
        raise _error(
            "provider_unavailable", "Tesla Fleet API request failed.", retryable=True
        ) from None

    try:
        payload = json.loads(raw, parse_constant=_parse_json_constant)
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise _error("malformed_response", "Tesla Fleet API returned invalid JSON.") from None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("response"), Mapping):
        raise _error("malformed_response", "Tesla Fleet API returned an unexpected JSON shape.")
    data = dict(payload["response"])
    _check_finite_json(data, credential=bearer)
    return data, site_id, path


def _response_site_id(value: Any, selected_site_id: str) -> bool:
    if isinstance(value, str):
        return value == selected_site_id
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value) == selected_site_id
    return False


def _check_response_site(data: Mapping[str, Any], site_id: str, *, required: bool) -> None:
    response_id = data.get("id")
    if response_id is None and required:
        raise _error(
            "malformed_response", "Tesla site information did not identify the selected site."
        )
    for identity in ("id", "energy_site_id", "site_id"):
        value = data.get(identity)
        if value is not None:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise _error("malformed_response", "Tesla returned an invalid site identifier.")
            if not _response_site_id(value, site_id):
                raise _error(
                    "site_scope_mismatch",
                    "Tesla returned information for a different energy site.",
                )


def _field_units(data: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    pending: list[Any] = [data]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                if isinstance(key, str) and key in _FIELD_UNITS:
                    found[key] = _FIELD_UNITS[key]
                if isinstance(value, (Mapping, list)):
                    pending.append(value)
        elif isinstance(current, list):
            pending.extend(item for item in current if isinstance(item, (Mapping, list)))
    return found


def _redact_response(data: Any, credential: str) -> Any:
    if isinstance(data, str):
        return data.replace(credential, "[REDACTED]")
    if isinstance(data, list):
        return [_redact_response(value, credential) for value in data]
    if isinstance(data, Mapping):
        return {key: _redact_response(value, credential) for key, value in data.items()}
    return data


def _timestamp(data: Mapping[str, Any]) -> datetime | None:
    value = data.get("timestamp")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("malformed_response", "Tesla returned an invalid observation timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _error(
            "malformed_response", "Tesla returned an invalid observation timestamp."
        ) from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise _error("malformed_response", "Tesla returned a timestamp without a UTC offset.")
    return result.astimezone(UTC)


def _result(
    data: dict[str, Any],
    *,
    gateway_site_id: str | None,
    path: str,
    credential: str,
    live: bool,
) -> EnergyResult:
    safe_data = _redact_response(data, credential)
    observed_at = _timestamp(data) if live else None
    return EnergyResult(
        data=safe_data,
        kind=DataKind.METERED,
        unit="mixed",
        source=_SOURCE,
        provider=_SOURCE,
        site_id=gateway_site_id,
        time_start=observed_at,
        time_end=observed_at,
        quantity_shape="instantaneous" if live else None,
        original_unit="mixed",
        field_units=_field_units(data),
        assumptions=[],
        warnings=[],
        quality="provider-reported",
        provenance=[
            {
                "provider": "Tesla Fleet API",
                "endpoint": path,
                "documentation": TESLA_ENERGY_DOCS,
            }
        ],
    )


async def get_site_info(args: Json, ctx: ExecutionContext) -> EnergyResult:
    if not isinstance(args, Mapping) or args:
        raise _error(
            "invalid_input", "Tesla site information accepts no caller-supplied parameters."
        )
    data, site_id, path = await _request_response(
        ctx.http, ctx.account, ctx.credential, "site_info"
    )
    _check_response_site(data, site_id, required=True)
    return _result(
        data,
        gateway_site_id=ctx.account.site_id if ctx.account else None,
        path=path,
        credential=_credential(ctx.credential),
        live=False,
    )


async def get_live_status(args: Json, ctx: ExecutionContext) -> EnergyResult:
    if not isinstance(args, Mapping) or args:
        raise _error("invalid_input", "Tesla live status accepts no caller-supplied parameters.")
    data, site_id, path = await _request_response(
        ctx.http, ctx.account, ctx.credential, "live_status"
    )
    _check_response_site(data, site_id, required=False)
    return _result(
        data,
        gateway_site_id=ctx.account.site_id if ctx.account else None,
        path=path,
        credential=_credential(ctx.credential),
        live=True,
    )


async def probe(http: httpx.AsyncClient, account: ConnectedAccount, credential: str) -> bool:
    """Return whether the selected account site responds with its own site id."""

    try:
        data, site_id, _path = await _request_response(http, account, credential, "site_info")
        _check_response_site(data, site_id, required=True)
    except Exception:
        raise _error("provider_verification_failed", "Tesla site verification failed.") from None
    return True


def register(registry: Registry) -> None:
    registry.add_toolkit(
        Toolkit(
            id=_TOOLKIT_ID,
            name="Tesla Energy",
            description="Read information and live status for the Tesla Energy site selected in a connected account.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=TESLA_ENERGY_DOCS,
            categories=["solar", "battery", "powerwall", "energy"],
        )
    )

    for name, handler, description, capabilities in (
        (
            "tesla_energy.get_site_info",
            get_site_info,
            "Read Tesla site assets, settings, and capabilities for the connected account's selected site.",
            ["get_energy_site_info", "energy site", "solar", "battery", "powerwall"],
        ),
        (
            "tesla_energy.get_live_status",
            get_live_status,
            "Read Tesla Energy live power, stored energy, grid state, and storm mode for the connected account's selected site.",
            ["get_live_energy_status", "live status", "current power", "storage state"],
        ),
    ):
        registry.add(
            Tool(
                name=name,
                toolkit=_TOOLKIT_ID,
                resource_scope="account",
                description=description,
                input_schema=schema({}),
                capabilities=capabilities,
                actions={Action.READ, Action.EXTERNAL},
                result_kind=DataKind.METERED,
                result_unit="mixed",
            ),
            handler,
        )
