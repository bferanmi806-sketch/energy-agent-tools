"""Bounded integrations for public energy data and local models.

The connectors in this module deliberately expose provider operations rather than a
generic HTTP or SQL escape hatch.  Credentials come from ``ExecutionContext`` and
are never included in a schema, error, or provenance record.  Every network request
uses a fixed HTTPS origin, a bounded response, and no redirects.

The upstream contracts and the qualification evidence for these adapters are kept in
``docs/upstream-research.md``.
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import math
import os
import re
import signal
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import httpx

from ..models import (
    Action,
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

NESO_BASE = "https://api.neso.energy/api/3/action"
NESO_DOCS = "https://www.neso.energy/data-portal/api-guidance"
ELECTRICITY_MAPS_BASE = "https://api.electricitymaps.com/v4"
ELECTRICITY_MAPS_DOCS = "https://app.electricitymaps.com/docs"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
ENTSOE_DOCS = (
    "https://transparencyplatform.zendesk.com/hc/en-us/articles/15696677194644-Request-Endpoint"
)
WINDPOWERLIB_DOCS = "https://windpowerlib.readthedocs.io/en/stable/"
SQLITE_DOCS = "https://docs.python.org/3/library/sqlite3.html"
ENERGYPLUS_DOCS = "https://energyplus.readthedocs.io/en/stable/quick_start/quick_start.html"

_MAX_HTTP_BODY = 4 * 1024 * 1024
_MAX_NESO_BODY = 2 * 1024 * 1024
_MAX_ROWS = 10_000
_MAX_RANGE = timedelta(days=365)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_ZONE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def _error(code: str, message: str, retryable: bool = False) -> EnergyError:
    return EnergyError(code, message, retryable=retryable)


def _parse_time(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("invalid_time", f"{field} must be an ISO-8601 timestamp.") from exc
    else:
        raise _error("invalid_time", f"{field} must be an ISO-8601 timestamp.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error("naive_time", f"{field} must include a UTC offset.")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _range(
    args: Mapping[str, Any],
    *,
    maximum: timedelta = _MAX_RANGE,
    required: bool = False,
) -> tuple[datetime | None, datetime | None]:
    start_raw, end_raw = args.get("start"), args.get("end")
    if required and (start_raw is None or end_raw is None):
        raise _error("invalid_time_range", "start and end are required.")
    if start_raw is None and end_raw is None:
        return None, None
    if start_raw is None or end_raw is None:
        raise _error("invalid_time_range", "start and end must be supplied together.")
    start, end = _parse_time(start_raw, "start"), _parse_time(end_raw, "end")
    if end <= start:
        raise _error("invalid_time_range", "end must be after start.")
    if end - start > maximum:
        raise _error("range_too_large", "The requested time range is too large.")
    return start, end


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid_input", f"{field} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise _error("invalid_input", f"{field} must be a finite number in the allowed range.")
    return result


def _bounded_limit(value: Any, field: str = "limit", *, maximum: int = _MAX_ROWS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise _error("invalid_limit", f"{field} must be an integer from 1 to {maximum}.")
    return value


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme.lower(), (url.host or "").lower(), url.port


def _same_origin(actual: httpx.URL, expected: str) -> bool:
    try:
        return _origin(actual) == _origin(httpx.URL(expected))
    except Exception:
        return False


async def _request_bytes(
    ctx: ExecutionContext,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    expected_origin: str,
    max_bytes: int = _MAX_HTTP_BODY,
) -> bytes:
    """Perform one bounded GET with provider-safe error categories."""

    try:
        response = await ctx.http.request(
            "GET",
            url,
            params=dict(params or {}),
            headers=dict(headers or {}),
            follow_redirects=False,
        )
    except httpx.RequestError as exc:
        raise _error(
            "provider_unavailable", "The energy provider could not be reached.", True
        ) from exc
    if 300 <= response.status_code < 400:
        raise _error("unexpected_redirect", "The energy provider returned an unexpected redirect.")
    if response.status_code in {401, 403}:
        raise _error(
            "authentication_failed", "The energy provider rejected the configured credential."
        )
    if response.status_code == 404:
        raise _error("provider_not_found", "The requested provider resource was not found.")
    if response.status_code == 429:
        raise _error("rate_limited", "The energy provider rate limit was reached.", True)
    if response.status_code >= 500:
        raise _error("provider_unavailable", "The energy provider returned a server error.", True)
    if response.status_code >= 400:
        raise _error("provider_error", "The energy provider rejected the request.")
    if not _same_origin(response.url, expected_origin):
        raise _error("unsafe_redirect", "The provider response crossed the fixed origin.")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise _error("response_too_large", "The provider response exceeds the size limit.")
        except ValueError:
            pass
    content = response.content
    if len(content) > max_bytes:
        raise _error("response_too_large", "The provider response exceeds the size limit.")
    return content


def _json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError) as exc:
        raise _error("malformed_response", "The energy provider returned invalid JSON.") from exc


def _result(
    data: Any,
    *,
    kind: DataKind,
    unit: str,
    source: str,
    docs: str,
    endpoint: str,
    resolution: str | None = None,
    warnings: list[str] | None = None,
    quality: str = "provider-reported",
    field_units: dict[str, str] | None = None,
) -> EnergyResult:
    return EnergyResult(
        data=data,
        kind=kind,
        unit=unit,
        source=source,
        timezone="UTC",
        resolution=resolution,
        original_unit=unit,
        field_units=field_units or {},
        warnings=warnings or [],
        quality=quality,
        provenance=[{"provider": source, "endpoint": endpoint, "documentation": docs}],
    )


# ---------------------------------------------------------------------------
# NESO CKAN


_NESO_ACTIONS = {"package_search", "package_show", "datastore_search"}
_NESO_MIN_INTERVAL_SECONDS = 1.0
_NESO_DATASTORE_INTERVAL_SECONDS = 30.0
_NESO_LIMITERS: WeakKeyDictionary[Any, tuple[asyncio.Lock, float]] = WeakKeyDictionary()


async def _neso_request(ctx: ExecutionContext, action: str, params: Mapping[str, Any]) -> Any:
    if action not in _NESO_ACTIONS:
        raise _error("unsupported_operation", "The NESO operation is not available.")
    try:
        lock, next_allowed = _NESO_LIMITERS[ctx.http]
    except KeyError:
        lock, next_allowed = asyncio.Lock(), 0.0
        _NESO_LIMITERS[ctx.http] = (lock, next_allowed)
    async with lock:
        _, next_allowed = _NESO_LIMITERS[ctx.http]
        delay = next_allowed - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        interval = (
            _NESO_DATASTORE_INTERVAL_SECONDS
            if action == "datastore_search"
            else _NESO_MIN_INTERVAL_SECONDS
        )
        _NESO_LIMITERS[ctx.http] = (lock, time.monotonic() + interval)
        body = await _request_bytes(
            ctx,
            f"{NESO_BASE}/{action}",
            params=params,
            expected_origin=NESO_BASE,
            max_bytes=_MAX_NESO_BODY,
        )
    payload = _json(body)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise _error("provider_error", "NESO returned an unsuccessful response.")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _error("malformed_response", "NESO returned an invalid result object.")
    return result


def _neso_metadata(args: Mapping[str, Any]) -> tuple[str, int]:
    query = args.get("q")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise _error(
            "invalid_query", "q must be a non-empty search string of at most 200 characters."
        )
    rows = _bounded_limit(args.get("rows", 20), "rows", maximum=50)
    return query.strip(), rows


async def _neso_search(args: Json, ctx: ExecutionContext) -> EnergyResult:
    query, rows = _neso_metadata(args)
    payload = await _neso_request(ctx, "package_search", {"q": query, "rows": rows})
    results = payload.get("results")
    count = payload.get("count")
    if (
        not isinstance(results, list)
        or len(results) > 50
        or (count is not None and (isinstance(count, bool) or not isinstance(count, int)))
    ):
        raise _error("malformed_response", "NESO returned invalid dataset search results.")
    datasets = [item for item in results if isinstance(item, dict)]
    if len(datasets) != len(results):
        raise _error("malformed_response", "NESO returned an invalid dataset entry.")
    return _result(
        {"count": count if isinstance(count, int) else len(datasets), "datasets": datasets},
        kind=DataKind.CALCULATED,
        unit="dataset metadata",
        source="neso",
        docs=NESO_DOCS,
        endpoint="package_search",
        quality="provider-catalogue",
    )


def _neso_resource(package: Mapping[str, Any], resource_id: str | None) -> Mapping[str, Any]:
    resources = package.get("resources")
    if not isinstance(resources, list) or not resources:
        raise _error("provider_not_found", "NESO dataset has no queryable resources.")
    candidates = [resource for resource in resources if isinstance(resource, dict)]
    if resource_id is not None:
        matches = [resource for resource in candidates if str(resource.get("id")) == resource_id]
        if not matches:
            raise _error("provider_not_found", "The requested NESO resource was not found.")
        chosen = matches[0]
    else:
        active = [resource for resource in candidates if resource.get("datastore_active") is True]
        chosen = active[0] if active else candidates[0]
    identifier = chosen.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise _error("malformed_response", "NESO returned a resource without an identifier.")
    return chosen


def _neso_record_time(record: Mapping[str, Any]) -> datetime | None:
    for name in (
        "timestamp",
        "datetime",
        "DATE_GMT",
        "date_gmt",
        "SETTLEMENT_DATE",
        "from",
        "start",
    ):
        value = record.get(name)
        if isinstance(value, str):
            try:
                return _parse_time(value, name)
            except EnergyError:
                continue
    date_value, time_value = record.get("DATE_GMT"), record.get("TIME_GMT")
    if isinstance(date_value, str) and isinstance(time_value, str):
        try:
            return _parse_time(f"{date_value}T{time_value}:00+00:00", "DATE_GMT")
        except EnergyError:
            return None
    return None


async def _neso_query(args: Json, ctx: ExecutionContext) -> EnergyResult:
    dataset = args.get("dataset")
    if not isinstance(dataset, str) or not 1 <= len(dataset) <= 200 or " " in dataset:
        raise _error("invalid_dataset", "dataset must be a valid NESO package identifier.")
    resource_id = args.get("resource_id")
    if resource_id is not None and (
        not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 200
    ):
        raise _error("invalid_resource", "resource_id must be a non-empty string.")
    filters = args.get("filters", {})
    if not isinstance(filters, dict) or len(filters) > 8:
        raise _error("invalid_filters", "filters must contain at most eight scalar fields.")
    if any(
        not isinstance(key, str)
        or not _IDENTIFIER.fullmatch(key)
        or isinstance(value, (dict, list, tuple, set))
        for key, value in filters.items()
    ):
        raise _error("invalid_filters", "filters must contain simple named scalar values.")
    limit = _bounded_limit(args.get("limit", 1000))
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100_000:
        raise _error("invalid_offset", "offset must be an integer from 0 to 100000.")
    start, end = _range(args)
    package = await _neso_request(ctx, "package_show", {"id": dataset})
    resource = _neso_resource(package, resource_id)
    resource_key = str(resource["id"])
    query_params: dict[str, Any] = {"resource_id": resource_key, "limit": limit, "offset": offset}
    if filters:
        query_params["filters"] = json.dumps(filters, separators=(",", ":"), sort_keys=True)
    payload = await _neso_request(ctx, "datastore_search", query_params)
    records = payload.get("records")
    fields = payload.get("fields", [])
    if not isinstance(records, list) or len(records) > _MAX_ROWS or not isinstance(fields, list):
        raise _error("malformed_response", "NESO returned invalid datastore rows.")
    if any(not isinstance(row, dict) for row in records):
        raise _error("malformed_response", "NESO returned an invalid datastore row.")
    rows = [dict(row) for row in records]
    malformed_timestamps = 0
    if start is not None and end is not None:
        filtered: list[dict[str, Any]] = []
        for row in rows:
            stamp = _neso_record_time(row)
            if stamp is None:
                malformed_timestamps += 1
            elif start <= stamp < end:
                filtered.append(row)
        rows = filtered
    field_units: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("id") or field.get("name")
        info = field.get("info")
        unit = info.get("unit") if isinstance(info, dict) else None
        if isinstance(name, str) and isinstance(unit, str) and unit:
            field_units[name] = unit
    unit = (
        next(iter(set(field_units.values())))
        if len(set(field_units.values())) == 1
        else "provider-defined"
    )
    searchable = " ".join(
        str(package.get(key, "")) for key in ("name", "title", "notes", "metadata_modified")
    ).lower()
    kind = DataKind.FORECAST if "forecast" in searchable else DataKind.CALCULATED
    warnings = [
        "NESO row semantics follow the selected dataset metadata; no metering claim is inferred."
    ]
    if start is not None and end is not None and malformed_timestamps:
        warnings.append(
            f"NESO omitted {malformed_timestamps} row(s) with no parseable timestamp from the requested time scope."
        )
    return _result(
        {"dataset": dataset, "resource_id": resource_key, "rows": rows, "fields": fields},
        kind=kind,
        unit=unit,
        source="neso",
        docs=NESO_DOCS,
        endpoint="datastore_search",
        warnings=warnings,
        quality="provider-dataset",
        field_units=field_units,
    )


# ---------------------------------------------------------------------------
# Electricity Maps v4


_EM_SIGNALS = {
    "carbon-intensity": "gCO2eq/kWh",
    "electricity-mix": "MW",
    "total-load": "MW",
    "total-reported-load": "MW",
    "net-load": "MW",
}
_EM_MODES = {"latest", "history", "past", "past-range", "forecast"}
_EM_GRANULARITIES = {"5_minutes", "15_minutes", "hourly", "daily"}
_EM_RANGES = {
    "5_minutes": timedelta(days=3),
    "15_minutes": timedelta(days=4),
    "hourly": timedelta(days=10),
    "daily": timedelta(days=365),
}


def _credential(ctx: ExecutionContext, provider: str) -> str:
    value = ctx.credential
    if not isinstance(value, str) or not value:
        raise _error("credential_missing", f"A credential is required for {provider}.")
    return value


def _em_args(
    args: Mapping[str, Any],
) -> tuple[str, str, str, str, datetime | None, datetime | None]:
    signal = args.get("signal", "carbon-intensity")
    mode = args.get("mode", "latest")
    zone = args.get("zone")
    granularity = args.get("temporal_granularity", "hourly")
    if not isinstance(signal, str) or signal not in _EM_SIGNALS:
        raise _error("invalid_signal", "The Electricity Maps signal is unsupported.")
    if not isinstance(mode, str) or mode not in _EM_MODES:
        raise _error("invalid_mode", "The Electricity Maps query mode is unsupported.")
    if not isinstance(zone, str) or not _ZONE.fullmatch(zone):
        raise _error("invalid_zone", "zone must be a short Electricity Maps zone identifier.")
    if not isinstance(granularity, str) or granularity not in _EM_GRANULARITIES:
        raise _error("invalid_granularity", "The temporal granularity is unsupported.")
    start: datetime | None
    end: datetime | None
    if mode in {"past", "past-range"}:
        if mode == "past":
            if args.get("start") is None or args.get("end") is not None:
                raise _error("invalid_time_range", "past mode requires start and no end.")
            start, end = _parse_time(args["start"], "start"), None
        else:
            start, end = _range(args, maximum=_EM_RANGES[granularity], required=True)
    else:
        if args.get("start") is not None or args.get("end") is not None:
            raise _error("invalid_time_range", "start and end apply only to past modes.")
        start = end = None
    return signal, mode, zone, granularity, start, end


def _em_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise _error("malformed_response", "Electricity Maps returned an invalid object.")
    for key in ("data", "history", "forecast"):
        value = payload.get(key)
        if isinstance(value, list):
            if len(value) > _MAX_ROWS or any(not isinstance(item, dict) for item in value):
                raise _error("malformed_response", "Electricity Maps returned invalid rows.")
            return [dict(item) for item in value]
    if any(key in payload for key in ("carbonIntensity", "value", "datetime", "mix")):
        return [dict(payload)]
    raise _error("malformed_response", "Electricity Maps returned no recognised data rows.")


def _em_time(row: Mapping[str, Any]) -> str | None:
    value = row.get("datetime") or row.get("timestamp")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("malformed_response", "Electricity Maps returned an invalid timestamp.")
    return _iso(_parse_time(value, "datetime"))


def _em_normalize(
    signal: str, payload: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str, list[str]]:
    rows = _em_records(payload)
    raw_unit = payload.get("unit")
    unit = raw_unit if isinstance(raw_unit, str) else _EM_SIGNALS[signal]
    warnings: list[str] = []
    output: list[dict[str, Any]] = []
    estimated = False
    for original in rows:
        timestamp = _em_time(original)
        if original.get("isEstimated") is True:
            estimated = True
        row_start = len(output)
        if signal == "electricity-mix":
            mix = original.get("mix")
            if not isinstance(mix, dict):
                raise _error(
                    "malformed_response", "Electricity Maps returned an invalid electricity mix."
                )
            for name, raw in mix.items():
                if isinstance(raw, dict):
                    for sub_name, nested in raw.items():
                        if isinstance(nested, (int, float)) and not isinstance(nested, bool):
                            output.append(
                                {
                                    "timestamp": timestamp,
                                    "variable": f"{name}.{sub_name}",
                                    "value": float(nested),
                                    "unit": unit,
                                    "is_estimated": original.get("isEstimated"),
                                }
                            )
                elif raw is None or (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
                    output.append(
                        {
                            "timestamp": timestamp,
                            "variable": str(name),
                            "value": None if raw is None else float(raw),
                            "unit": unit,
                            "is_estimated": original.get("isEstimated"),
                        }
                    )
        else:
            if signal == "carbon-intensity":
                raw_value = original.get("carbonIntensity")
            else:
                raw_value = original.get("value")
            if raw_value is not None and (
                isinstance(raw_value, bool) or not isinstance(raw_value, (int, float))
            ):
                raise _error("malformed_response", "Electricity Maps returned a non-numeric value.")
            output.append(
                {
                    "timestamp": timestamp,
                    "value": None if raw_value is None else float(raw_value),
                    "unit": unit,
                    "is_estimated": original.get("isEstimated"),
                    "source": original.get("source"),
                }
            )
        if original.get("flowTraced") is not None:
            for row in output[row_start:]:
                row["flow_traced"] = bool(original["flowTraced"])
    if estimated:
        warnings.append("The provider marked one or more returned values as estimated.")
    if not output:
        warnings.append("Electricity Maps returned no numeric values for the requested signal.")
    return output, unit, warnings


async def _electricity_maps(args: Json, ctx: ExecutionContext) -> EnergyResult:
    signal, mode, zone, granularity, start, end = _em_args(args)
    token = _credential(ctx, "Electricity Maps")
    params: dict[str, Any] = {"zone": zone, "temporalGranularity": granularity}
    if args.get("flow_traced") is not None:
        if not isinstance(args.get("flow_traced"), bool):
            raise _error("invalid_flow_traced", "flow_traced must be a boolean.")
        params["flowTraced"] = args["flow_traced"]
    if args.get("data_source") is not None:
        data_source = args.get("data_source")
        if data_source not in {"normal", "flow-traced"}:
            raise _error("invalid_data_source", "data_source must be normal or flow-traced.")
        mapped_flow_traced = data_source == "flow-traced"
        if "flowTraced" in params and params["flowTraced"] != mapped_flow_traced:
            raise _error("invalid_data_source", "flow_traced and data_source disagree.")
        params["flowTraced"] = mapped_flow_traced
    if mode == "past":
        assert start is not None
        params["datetime"] = _iso(start)
    elif mode == "past-range":
        assert start is not None and end is not None
        params["start"] = _iso(start)
        params["end"] = _iso(end)
    endpoint = f"/v4/{signal}/{mode}"
    content = await _request_bytes(
        ctx,
        ELECTRICITY_MAPS_BASE + f"/{signal}/{mode}",
        params=params,
        headers={"auth-token": token},
        expected_origin=ELECTRICITY_MAPS_BASE,
    )
    payload = _json(content)
    if not isinstance(payload, dict):
        raise _error("malformed_response", "Electricity Maps returned an invalid object.")
    output, unit, warnings = _em_normalize(signal, payload)
    any_estimated = any(row.get("is_estimated") is True for row in output)
    if mode == "forecast":
        kind = DataKind.FORECAST
    elif signal == "total-reported-load":
        kind = DataKind.ESTIMATED if any_estimated else DataKind.METERED
    else:
        kind = DataKind.ESTIMATED if any_estimated else DataKind.CALCULATED
    warnings.append("Electricity Maps load and mix semantics follow the selected v4 endpoint.")
    return _result(
        {"zone": zone, "signal": signal, "mode": mode, "rows": output},
        kind=kind,
        unit=unit,
        source="electricitymaps",
        docs=ELECTRICITY_MAPS_DOCS,
        endpoint=endpoint,
        resolution=granularity,
        warnings=warnings,
        quality="provider-estimated" if any_estimated else "provider-reported",
    )


# ---------------------------------------------------------------------------
# ENTSO-E transparency XML


_ENTSOE_DOCUMENTS = {"A65", "A69", "A73", "A75"}
_ENTSOE_PROCESSES = {"A01", "A16", "A31", "A32", "A33"}
_ENTSOE_DOMAIN_PARAMETERS = {"in_Domain", "outBiddingZone_Domain"}
_ENTSOE_RESOLUTIONS = {
    "PT15M": timedelta(minutes=15),
    "PT30M": timedelta(minutes=30),
    "PT60M": timedelta(hours=1),
    "PT1H": timedelta(hours=1),
    "P1D": timedelta(days=1),
}


def _xml_local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(element) if _xml_local(child) == name), None)


def _xml_text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    child = next((item for item in element.iter() if _xml_local(item) == name), None)
    return child.text.strip() if child is not None and isinstance(child.text, str) else None


def _entsoe_resolution(value: str | None) -> tuple[str, timedelta]:
    if value not in _ENTSOE_RESOLUTIONS:
        raise _error("malformed_response", "ENTSO-E returned an unsupported period resolution.")
    return value, _ENTSOE_RESOLUTIONS[value]


def _entsoe_parse(
    content: bytes, document_type: str, process_type: str
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"<![" in upper:
        raise _error("unsafe_xml", "ENTSO-E XML declarations and entities are not accepted.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise _error("malformed_response", "ENTSO-E returned invalid XML.") from exc
    if _xml_local(root) not in {"Publication_MarketDocument", "Acknowledgement_MarketDocument"}:
        raise _error("malformed_response", "ENTSO-E returned an unexpected document.")
    reason_code = _xml_text(root, "code")
    if reason_code == "999":
        return [], None, ["ENTSO-E reported no matching data for this query."]
    series = [item for item in root.iter() if _xml_local(item) == "TimeSeries"]
    if not series:
        raise _error("no_data", "ENTSO-E returned no time series for this query.")
    output: list[dict[str, Any]] = []
    warnings: list[str] = []
    resolution_name: str | None = None
    for time_series in series:
        series_id = _xml_text(time_series, "mRID")
        domain = _xml_text(time_series, "inBiddingZone_Domain.mRID") or _xml_text(
            time_series, "outBiddingZone_Domain.mRID"
        )
        for period in [item for item in time_series.iter() if _xml_local(item) == "Period"]:
            interval = _xml_child(period, "timeInterval")
            start_raw = _xml_text(interval, "start")
            end_raw = _xml_text(interval, "end")
            if start_raw is None or end_raw is None:
                raise _error("malformed_response", "ENTSO-E period has no complete time interval.")
            try:
                period_start = _parse_time(start_raw, "period start")
                period_end = _parse_time(end_raw, "period end")
            except EnergyError as exc:
                raise _error(
                    "malformed_response", "ENTSO-E returned an invalid period timestamp."
                ) from exc
            resolution_raw = _xml_text(period, "resolution")
            resolution, step = _entsoe_resolution(resolution_raw)
            if resolution_name is None:
                resolution_name = resolution
            duration = period_end - period_start
            if duration.total_seconds() % step.total_seconds() != 0:
                raise _error(
                    "malformed_response", "ENTSO-E period is not divisible by its resolution."
                )
            expected = int(duration / step)
            if expected < 1 or expected > _MAX_ROWS:
                raise _error("result_too_large", "ENTSO-E period contains too many positions.")
            points: dict[int, float | None] = {}
            for point in [item for item in period.iter() if _xml_local(item) == "Point"]:
                position_raw = _xml_text(point, "position")
                if position_raw is None:
                    raise _error("malformed_response", "ENTSO-E point has no position.")
                try:
                    position = int(position_raw)
                except ValueError as exc:
                    raise _error(
                        "malformed_response", "ENTSO-E point position is invalid."
                    ) from exc
                if not 1 <= position <= expected or position in points:
                    raise _error(
                        "malformed_response", "ENTSO-E point positions are invalid or duplicated."
                    )
                quantity_raw = _xml_text(point, "quantity")
                if quantity_raw is None:
                    points[position] = None
                else:
                    try:
                        quantity = float(quantity_raw)
                    except ValueError as exc:
                        raise _error("malformed_response", "ENTSO-E quantity is invalid.") from exc
                    if not math.isfinite(quantity):
                        raise _error("malformed_response", "ENTSO-E quantity is not finite.")
                    points[position] = quantity
            for position in range(1, expected + 1):
                point_value = points.get(position)
                if position not in points:
                    warnings.append(
                        "ENTSO-E returned a period with missing positions; missing values are null."
                    )
                if len(output) >= _MAX_ROWS:
                    raise _error("result_too_large", "ENTSO-E returned too many time-series rows.")
                output.append(
                    {
                        "timestamp": _iso(period_start + (position - 1) * step),
                        "position": position,
                        "value": point_value,
                        "unit": "MW",
                        "series_id": series_id,
                        "domain": domain,
                        "document_type": document_type,
                        "process_type": process_type,
                    }
                )
    output.sort(key=lambda row: (row["timestamp"], row.get("series_id") or "", row["position"]))
    if not output:
        warnings.append("ENTSO-E returned no positions.")
    return output, resolution_name, sorted(set(warnings))


async def _entsoe_timeseries(args: Json, ctx: ExecutionContext) -> EnergyResult:
    token = _credential(ctx, "ENTSO-E")
    document_type = args.get("document_type", "A65")
    process_type = args.get("process_type", "A16")
    domain = args.get("domain")
    domain_parameter = args.get("domain_parameter")
    if document_type not in _ENTSOE_DOCUMENTS:
        raise _error("invalid_document_type", "The ENTSO-E document type is unsupported.")
    if process_type not in _ENTSOE_PROCESSES:
        raise _error("invalid_process_type", "The ENTSO-E process type is unsupported.")
    if (
        not isinstance(domain, str)
        or not 2 <= len(domain) <= 80
        or not re.fullmatch(r"[A-Za-z0-9._:-]+", domain)
    ):
        raise _error("invalid_domain", "domain must be a valid ENTSO-E area identifier.")
    if domain_parameter is None:
        domain_parameter = (
            "outBiddingZone_Domain" if document_type in {"A65", "A69"} else "in_Domain"
        )
    if domain_parameter not in _ENTSOE_DOMAIN_PARAMETERS:
        raise _error("invalid_domain_parameter", "The ENTSO-E domain parameter is unsupported.")
    start, end = _range(args, maximum=_MAX_RANGE, required=True)
    assert start is not None and end is not None
    params = {
        "securityToken": token,
        "documentType": document_type,
        "processType": process_type,
        domain_parameter: domain,
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }
    content = await _request_bytes(ctx, ENTSOE_BASE, params=params, expected_origin=ENTSOE_BASE)
    rows, resolution, warnings = _entsoe_parse(content, document_type, process_type)
    kind = DataKind.FORECAST if document_type == "A69" else DataKind.METERED
    if any(row["value"] is None for row in rows):
        warnings.append("ENTSO-E returned missing values; they are represented as null.")
    return _result(
        {
            "domain": domain,
            "document_type": document_type,
            "process_type": process_type,
            "rows": rows,
        },
        kind=kind,
        unit="MW",
        source="entsoe",
        docs=ENTSOE_DOCS,
        endpoint="/api",
        resolution=resolution,
        warnings=sorted(set(warnings)),
        quality="provider-forecast" if kind == DataKind.FORECAST else "provider-reported",
    )


# ---------------------------------------------------------------------------
# windpowerlib, with explicit offline weather and turbine inputs


def _wind_timestamp(value: Any, field: str) -> datetime:
    return _parse_time(value, field)


def _wind_curve(value: Any) -> dict[str, list[float]]:
    if not isinstance(value, list) or not 2 <= len(value) <= 200:
        raise _error("invalid_power_curve", "power_curve must contain two to 200 points.")
    speeds: list[float] = []
    powers: list[float] = []
    for item in value:
        if not isinstance(item, dict):
            raise _error("invalid_power_curve", "Each power curve point must be an object.")
        speed = _finite(item.get("wind_speed_m_s"), "power_curve.wind_speed_m_s", minimum=0)
        power = _finite(item.get("power_kw"), "power_curve.power_kw", minimum=0)
        speeds.append(speed)
        powers.append(power * 1000.0)
    if any(right <= left for left, right in zip(speeds, speeds[1:], strict=False)):
        raise _error("invalid_power_curve", "power_curve wind speeds must increase strictly.")
    return {"wind_speed": speeds, "value": powers}


async def _wind_power(args: Json, ctx: ExecutionContext) -> EnergyResult:
    del ctx
    try:
        import pandas as pd
        from windpowerlib import ModelChain, WindTurbine
    except ImportError as exc:
        raise _error(
            "dependency_unavailable", "Install the optional windpowerlib dependency."
        ) from exc
    hub_height = _finite(args.get("hub_height_m"), "hub_height_m", minimum=1)
    rows = args.get("weather_rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_ROWS:
        raise _error("invalid_weather", "weather_rows must contain one to 10000 rows.")
    timezone_name = args.get("timezone", "UTC")
    if not isinstance(timezone_name, str):
        raise _error("invalid_timezone", "timezone must be an IANA timezone name.")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
    except Exception as exc:
        raise _error("invalid_timezone", "timezone must be an IANA timezone name.") from exc
    timestamps: list[datetime] = []
    wind_speeds: list[float] = []
    roughness: list[float] = []
    temperatures: list[float] = []
    pressures: list[float] = []
    densities: list[float] = []
    measurement_height: float | None = None
    have_temperature = have_pressure = have_density = False
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise _error("invalid_weather", f"weather_rows[{index}] must be an object.")
        stamp = _wind_timestamp(raw.get("timestamp"), f"weather_rows[{index}].timestamp")
        speed = _finite(
            raw.get("wind_speed_m_s"), f"weather_rows[{index}].wind_speed_m_s", minimum=0
        )
        height = _finite(
            raw.get("measurement_height_m"),
            f"weather_rows[{index}].measurement_height_m",
            minimum=0.1,
        )
        rough = _finite(
            raw.get("roughness_length_m", raw.get("roughness_length", 0.1)),
            "roughness_length_m",
            minimum=0,
        )
        if measurement_height is None:
            measurement_height = height
        elif height != measurement_height:
            raise _error("invalid_weather", "All weather rows must use one measurement height.")
        timestamps.append(stamp)
        wind_speeds.append(speed)
        roughness.append(rough)
        temperature = raw.get("temperature_c")
        pressure = raw.get("pressure_pa")
        density = raw.get("density_kg_m3")
        if temperature is not None:
            have_temperature = True
            temperatures.append(_finite(temperature, "temperature_c") + 273.15)
        else:
            temperatures.append(float("nan"))
        if pressure is not None:
            have_pressure = True
            pressures.append(_finite(pressure, "pressure_pa", minimum=1))
        else:
            pressures.append(float("nan"))
        if density is not None:
            have_density = True
            densities.append(_finite(density, "density_kg_m3", minimum=0.01))
        else:
            densities.append(float("nan"))
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise _error("invalid_weather", "weather timestamps must be strictly increasing.")
    columns: list[tuple[str, float]] = [
        ("wind_speed", measurement_height or 10.0),
        ("roughness_length", 0),
    ]
    values: list[list[float]] = [wind_speeds, roughness]
    if have_temperature:
        if any(not math.isfinite(value) for value in temperatures):
            raise _error(
                "invalid_weather", "temperature_c must be supplied for every weather row when used."
            )
        columns.append(("temperature", measurement_height or 10.0))
        values.append(temperatures)
    if have_pressure:
        if any(not math.isfinite(value) for value in pressures):
            raise _error(
                "invalid_weather", "pressure_pa must be supplied for every weather row when used."
            )
        columns.append(("pressure", measurement_height or 10.0))
        values.append(pressures)
    if have_density:
        if any(not math.isfinite(value) for value in densities):
            raise _error(
                "invalid_weather", "density_kg_m3 must be supplied for every weather row when used."
            )
        columns.append(("density", measurement_height or 10.0))
        values.append(densities)
    weather = pd.DataFrame(
        {column: value for column, value in zip(columns, values, strict=True)},
        index=pd.DatetimeIndex(timestamps),
    )
    weather.columns = pd.MultiIndex.from_tuples(columns)
    turbine_type = args.get("turbine_type")
    curve = args.get("power_curve")
    if turbine_type is not None and curve is not None:
        raise _error("invalid_turbine", "Supply turbine_type or power_curve, not both.")
    try:
        if turbine_type is not None:
            if not isinstance(turbine_type, str) or not 1 <= len(turbine_type) <= 100:
                raise _error("invalid_turbine", "turbine_type must be a valid built-in type name.")
            # path='oedb' is windpowerlib's packaged offline data.  No remote
            # database or user-provided path is accepted by this connector.
            turbine = WindTurbine(hub_height=hub_height, turbine_type=turbine_type, path="oedb")
            model_name = turbine_type
        else:
            curve_data = _wind_curve(curve)
            nominal = args.get("nominal_power_kw")
            nominal_power = (
                _finite(nominal, "nominal_power_kw", minimum=0) * 1000
                if nominal is not None
                else max(curve_data["value"])
            )
            turbine = WindTurbine(
                hub_height=hub_height,
                nominal_power=nominal_power,
                power_curve=curve_data,
                path=None,
            )
            model_name = "explicit-power-curve"
        chain = ModelChain(turbine, wind_speed_model="logarithmic", density_correction=False)
        chain.run_model(weather)
        output_power = chain.power_output
    except EnergyError:
        raise
    except Exception as exc:
        raise _error(
            "wind_model_failed", "windpowerlib could not evaluate the supplied weather and turbine."
        ) from exc
    if output_power is None or len(output_power) != len(rows):
        raise _error("wind_model_failed", "windpowerlib returned an invalid power series.")
    count = args.get("number_of_turbines", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise _error(
            "invalid_turbine_count", "number_of_turbines must be an integer from 1 to 1000."
        )
    intervals: list[dict[str, Any]] = []
    powers_kw = [float(value) / 1000.0 * count for value in output_power]
    for index, (stamp, power) in enumerate(zip(timestamps, powers_kw, strict=True)):
        if index + 1 < len(timestamps):
            duration = (timestamps[index + 1] - stamp).total_seconds() / 3600.0
        else:
            duration = float(args.get("final_interval_hours", 1.0))
            if not math.isfinite(duration) or duration <= 0 or duration > 168:
                raise _error("invalid_weather", "final_interval_hours must be between 0 and 168.")
        intervals.append(
            {
                "timestamp": _iso(stamp),
                "wind_speed_m_s": wind_speeds[index],
                "power_kw": power,
                "energy_kwh": power * duration,
                "duration_hours": duration,
            }
        )
    return _result(
        {
            "turbine": model_name,
            "hub_height_m": hub_height,
            "number_of_turbines": count,
            "intervals": intervals,
        },
        kind=DataKind.ESTIMATED,
        unit="kW and kWh",
        source="windpowerlib",
        docs=WINDPOWERLIB_DOCS,
        endpoint="ModelChain.run_model",
        resolution="input interval",
        warnings=[
            "Weather and turbine inputs were supplied by the operator; no weather or turbine data was fetched."
        ],
        quality="modelled",
    )


# ---------------------------------------------------------------------------
# Safe read-only SQLite


def _safe_path(root: Path, relative: Any, *, suffixes: set[str] | None = None) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise _error("invalid_path", "A relative path is required.")
    root_resolved = root.expanduser().resolve()
    path = (root_resolved / relative).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise _error(
            "path_forbidden", "The path must remain inside the configured operator root."
        ) from exc
    if suffixes and path.suffix.lower() not in suffixes:
        raise _error("invalid_path", "The file extension is not supported.")
    if not path.is_file():
        raise _error("file_not_found", "The configured local file does not exist.")
    return path


def _sqlite_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _error("invalid_identifier", f"{field} must be a simple SQL identifier.")
    return value


def _sqlite_time(value: Any, timezone_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("invalid_time", "SQLite timestamp is not ISO-8601.") from exc
    else:
        raise _error("invalid_time", "SQLite timestamp is not ISO-8601.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        from zoneinfo import ZoneInfo

        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


def _sqlite_utc_text(value: Any, timezone_name: str) -> str:
    """Normalize one SQLite ISO value for exact UTC comparisons.

    SQLite's built-in lexical comparison treats offsets as ordinary text.  A
    private, generated function lets the bounded SELECT compare the same UTC
    representation that the result parser uses, including offset-aware and
    operator-timezone-naive values.
    """

    try:
        return _iso(_sqlite_time(value, timezone_name))
    except EnergyError as exc:
        raise ValueError(exc.message) from exc


def _sqlite_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db: str | None,
    source: str | None,
) -> int:
    del arg1, db, source
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if arg2 == "energy_agent_utc" else sqlite3.SQLITE_DENY
    denied = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_TRANSACTION,
    }
    return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK


async def _sqlite_read(args: Json, ctx: ExecutionContext, data_root: Path | None) -> EnergyResult:
    del ctx
    if data_root is None:
        raise _error("data_root_required", "SQLite tools require an operator-configured data root.")
    path = _safe_path(data_root, args.get("path"), suffixes={".db", ".sqlite", ".sqlite3"})
    table = _sqlite_identifier(args.get("table"), "table")
    timestamp_column = _sqlite_identifier(args.get("timestamp_column"), "timestamp_column")
    value_column = _sqlite_identifier(args.get("value_column"), "value_column")
    unit = args.get("unit", "provider-defined")
    if not isinstance(unit, str) or not 1 <= len(unit) <= 80:
        raise _error("invalid_unit", "unit must be a short string.")
    kind_raw = args.get("kind", DataKind.METERED.value)
    try:
        kind = DataKind(kind_raw)
    except ValueError as exc:
        raise _error("invalid_kind", "kind must be a supported energy data kind.") from exc
    timezone_name = args.get("timezone", "UTC")
    if not isinstance(timezone_name, str):
        raise _error("invalid_timezone", "timezone must be an IANA timezone name.")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
    except Exception as exc:
        raise _error("invalid_timezone", "timezone must be an IANA timezone name.") from exc
    limit = _bounded_limit(args.get("limit", 10_000))
    start, end = _range(args)
    quoted_table = '"' + table.replace('"', '""') + '"'
    quoted_timestamp = '"' + timestamp_column.replace('"', '""') + '"'
    quoted_value = '"' + value_column.replace('"', '""') + '"'
    sql = f"SELECT {quoted_timestamp}, {quoted_value} FROM {quoted_table}"
    utc_timestamp = f"energy_agent_utc({quoted_timestamp})"
    where: list[str] = []
    parameters: list[Any] = []
    if start is not None:
        where.append(f"{utc_timestamp} >= ?")
        parameters.append(_iso(start))
    if end is not None:
        where.append(f"{utc_timestamp} < ?")
        parameters.append(_iso(end))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {utc_timestamp} ASC LIMIT ?"
    parameters.append(limit)
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.create_function(
            "energy_agent_utc",
            1,
            lambda value: _sqlite_utc_text(value, timezone_name),
            deterministic=True,
        )
        connection.set_authorizer(_sqlite_authorizer)
        records = connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as exc:
        raise _error("sqlite_read_failed", "The SQLite read could not be completed.") from exc
    finally:
        try:
            connection.close()
        except (UnboundLocalError, AttributeError):
            pass
    output: list[dict[str, Any]] = []
    previous: datetime | None = None
    for record in records:
        timestamp = _sqlite_time(record[0], timezone_name)
        if previous is not None and timestamp <= previous:
            raise _error("invalid_timeseries", "SQLite timestamps must be strictly increasing.")
        previous = timestamp
        value = record[1]
        if value is not None:
            value = _finite(value, "value")
        output.append(
            {"timestamp": _iso(timestamp), "value": value, "unit": unit, "kind": kind.value}
        )
    return _result(
        output,
        kind=kind,
        unit=unit,
        source="sqlite",
        docs=SQLITE_DOCS,
        endpoint="read-only SELECT",
        warnings=[
            "SQLite rows were read with URI mode=ro and a write-denying authorizer; kind and unit were supplied by the operator."
        ],
        quality="operator-source",
    )


# ---------------------------------------------------------------------------
# Optional, bounded EnergyPlus execution


def _model_file(root: Path, value: Any, suffixes: set[str]) -> Path:
    return _safe_path(root, value, suffixes=suffixes)


async def _energyplus_run(
    args: Json,
    ctx: ExecutionContext,
    executable: Path,
    model_root: Path,
) -> EnergyResult:
    del ctx
    model = _model_file(model_root, args.get("model"), {".idf", ".epjson"})
    weather_value = args.get("weather")
    weather = (
        _model_file(model_root, weather_value, {".epw"}) if weather_value is not None else None
    )
    with tempfile.TemporaryDirectory(prefix="energyplus-") as output_dir:
        argv = [str(executable), "--output-directory", output_dir]
        if weather is not None:
            argv.extend(("--weather", str(weather)))
        if args.get("annual", False):
            argv.append("--annual")
        if args.get("design_day", False):
            argv.append("--design-day")
        if args.get("readvars", False):
            argv.append("--readvars")
        argv.append(str(model))
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(model_root),
                start_new_session=(os.name != "nt"),
            )
        except (OSError, ValueError) as exc:
            raise _error("executable_start_failed", "EnergyPlus could not be started.") from exc
        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=120.0)
        except TimeoutError as exc:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows CI only
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise _error(
                "simulation_timeout", "EnergyPlus exceeded the bounded runtime.", True
            ) from exc
        if return_code != 0:
            raise _error("simulation_failed", "EnergyPlus returned a non-zero exit status.")
        output_path = Path(output_dir)
        files: list[str] = []
        for candidate in sorted(output_path.iterdir()):
            if candidate.is_file():
                files.append(candidate.name)
                if len(files) >= 100:
                    break
        rows: list[dict[str, Any]] = []
        csv_path = output_path / "eplusout.csv"
        if csv_path.is_file():
            content = csv_path.read_bytes()
            if len(content) > _MAX_HTTP_BODY:
                raise _error("result_too_large", "EnergyPlus CSV output exceeds the size limit.")
            try:
                reader = csv.DictReader(content.decode("utf-8-sig").splitlines())
                for row in reader:
                    if len(rows) >= _MAX_ROWS:
                        raise _error("result_too_large", "EnergyPlus returned too many CSV rows.")
                    rows.append(dict(row))
            except UnicodeDecodeError as exc:
                raise _error("malformed_response", "EnergyPlus CSV output is not UTF-8.") from exc
        return _result(
            {
                "model": model.name,
                "weather": weather.name if weather else None,
                "files": files,
                "rows": rows,
            },
            kind=DataKind.SIMULATED,
            unit="provider-defined",
            source="energyplus",
            docs=ENERGYPLUS_DOCS,
            endpoint="energyplus fixed-argv process",
            warnings=[
                "EnergyPlus execution is bounded to 120 seconds and writes only inside a temporary output directory."
            ],
            quality="simulation-completed",
        )


def register_energyplus(registry: Registry, executable: Path, model_root: Path) -> None:
    """Register EnergyPlus only when an operator supplies a fixed executable and root."""

    executable = executable.expanduser().resolve()
    model_root = model_root.expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK) or not model_root.is_dir():
        raise ValueError("EnergyPlus executable and model_root must be existing operator paths.")
    registry.add_toolkit(
        Toolkit(
            id="energyplus",
            name="EnergyPlus",
            description="Run a bounded EnergyPlus model from an operator-owned IDF or epJSON root.",
            runtime="executable",
            status="stable",
            docs_url=ENERGYPLUS_DOCS,
        )
    )
    registry.add(
        Tool(
            name="energyplus.run_simulation",
            toolkit="energyplus",
            resource_scope="operator",
            description="Run one operator-owned EnergyPlus model with fixed arguments and bounded runtime.",
            input_schema=schema(
                {
                    "model": {"type": "string", "description": "Relative IDF or epJSON path."},
                    "weather": {"type": "string", "description": "Optional relative EPW path."},
                    "annual": {"type": "boolean", "default": False},
                    "design_day": {"type": "boolean", "default": False},
                    "readvars": {"type": "boolean", "default": False},
                },
                required=["model"],
            ),
            capabilities=["building simulation", "energyplus", "thermal model"],
            actions={Action.SIMULATE},
            result_kind=DataKind.SIMULATED,
            result_unit="provider-defined",
        ),
        lambda args, ctx: _energyplus_run(args, ctx, executable, model_root),
    )


# ---------------------------------------------------------------------------
# Registry wiring


def _windpowerlib_available() -> bool:
    return importlib.util.find_spec("windpowerlib") is not None


def register(registry: Registry, *, data_root: Path | None = None) -> None:
    """Register the bounded upstream and local integrations in this module."""

    registry.add_toolkit(
        Toolkit(
            id="neso",
            name="NESO Data Portal",
            description="Search and query bounded NESO CKAN datastore resources without arbitrary SQL.",
            runtime="http",
            status="stable",
            docs_url=NESO_DOCS,
            categories=["grid", "forecast", "datasets"],
        )
    )
    registry.add(
        Tool(
            name="neso.search_datasets",
            toolkit="neso",
            resource_scope="public",
            description="Search the NESO public CKAN dataset catalogue.",
            input_schema=schema(
                {
                    "q": {"type": "string", "minLength": 1, "maxLength": 200},
                    "rows": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                required=["q"],
            ),
            capabilities=["search_energy_datasets", "neso_dataset_catalogue"],
            actions={Action.READ, Action.EXTERNAL},
            result_kind=None,
            result_unit=None,
        ),
        _neso_search,
    )
    registry.add(
        Tool(
            name="neso.query_dataset",
            toolkit="neso",
            resource_scope="public",
            description="Read a bounded NESO datastore resource by package, resource, scalar filters, and row limit.",
            input_schema=schema(
                {
                    "dataset": {"type": "string", "minLength": 1, "maxLength": 200},
                    "resource_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "filters": {
                        "type": "object",
                        "maxProperties": 8,
                        "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_ROWS,
                        "default": 1000,
                    },
                    "offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
                },
                required=["dataset"],
            ),
            capabilities=["query_energy_dataset", "neso_datastore"],
            actions={Action.READ, Action.EXTERNAL},
            result_kind=None,
            result_unit=None,
        ),
        _neso_query,
    )
    registry.add_toolkit(
        Toolkit(
            id="electricitymaps",
            name="Electricity Maps",
            description="Read v4 carbon intensity, mix, load, net load, and forecast signals.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=ELECTRICITY_MAPS_DOCS,
            categories=["carbon", "grid", "forecast", "generation mix"],
        )
    )
    registry.add(
        Tool(
            name="electricitymaps.get_signal",
            toolkit="electricitymaps",
            resource_scope="account",
            description="Read one fixed Electricity Maps v4 signal with provider units and estimation flags preserved.",
            input_schema=schema(
                {
                    "signal": {
                        "type": "string",
                        "enum": sorted(_EM_SIGNALS),
                        "default": "carbon-intensity",
                    },
                    "mode": {"type": "string", "enum": sorted(_EM_MODES), "default": "latest"},
                    "zone": {"type": "string", "minLength": 2, "maxLength": 32},
                    "temporal_granularity": {
                        "type": "string",
                        "enum": sorted(_EM_GRANULARITIES),
                        "default": "hourly",
                    },
                    "flow_traced": {"type": "boolean"},
                    "data_source": {"type": "string", "enum": ["normal", "flow-traced"]},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                required=["zone"],
            ),
            capabilities=["get_carbon_intensity", "electricitymaps_signal"],
            actions={Action.READ, Action.EXTERNAL},
            result_kind=None,
            result_unit=None,
        ),
        _electricity_maps,
    )
    registry.add_toolkit(
        Toolkit(
            id="entsoe",
            name="ENTSO-E Transparency",
            description="Read bounded ENTSO-E transparency time series with secure XML parsing.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=ENTSOE_DOCS,
            categories=["grid", "generation", "load", "forecast"],
        )
    )
    registry.add(
        Tool(
            name="entsoe.get_timeseries",
            toolkit="entsoe",
            resource_scope="account",
            description="Read one ENTSO-E time series by document, process, area, and UTC interval.",
            input_schema=schema(
                {
                    "document_type": {
                        "type": "string",
                        "enum": sorted(_ENTSOE_DOCUMENTS),
                        "default": "A65",
                    },
                    "process_type": {
                        "type": "string",
                        "enum": sorted(_ENTSOE_PROCESSES),
                        "default": "A16",
                    },
                    "domain": {"type": "string", "minLength": 2, "maxLength": 80},
                    "domain_parameter": {
                        "type": "string",
                        "enum": sorted(_ENTSOE_DOMAIN_PARAMETERS),
                    },
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                required=["domain", "start", "end"],
            ),
            capabilities=["get_grid_load", "entsoe_timeseries"],
            actions={Action.READ, Action.EXTERNAL},
            result_kind=None,
            result_unit="MW",
        ),
        _entsoe_timeseries,
    )
    registry.add_toolkit(
        Toolkit(
            id="windpowerlib",
            name="windpowerlib",
            description="Run an offline wind turbine ModelChain from explicit weather and power-curve inputs.",
            runtime="python",
            status="experimental" if _windpowerlib_available() else "unavailable",
            docs_url=WINDPOWERLIB_DOCS,
            categories=["wind", "generation", "calculation"],
        )
    )
    registry.add(
        Tool(
            name="windpowerlib.estimate_generation",
            toolkit="windpowerlib",
            resource_scope="session",
            description="Estimate wind generation using windpowerlib's offline packaged data or an explicit power curve.",
            input_schema=schema(
                {
                    "hub_height_m": {"type": "number", "exclusiveMinimum": 0},
                    "turbine_type": {"type": "string", "maxLength": 100},
                    "power_curve": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "wind_speed_m_s": {"type": "number", "minimum": 0},
                                "power_kw": {"type": "number", "minimum": 0},
                            },
                            "required": ["wind_speed_m_s", "power_kw"],
                        },
                    },
                    "nominal_power_kw": {"type": "number", "minimum": 0},
                    "weather_rows": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MAX_ROWS,
                        "items": {"type": "object"},
                    },
                    "timezone": {"type": "string", "default": "UTC"},
                    "number_of_turbines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 1,
                    },
                    "final_interval_hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 168,
                        "default": 1,
                    },
                },
                required=["hub_height_m", "weather_rows"],
            ),
            capabilities=["estimate_wind_generation", "windpowerlib", "offline power curve model"],
            actions={Action.CALCULATE},
            result_kind=DataKind.ESTIMATED,
            result_unit="kW and kWh",
        ),
        _wind_power,
    )
    registry.add_toolkit(
        Toolkit(
            id="sqlite",
            name="Read-only SQLite",
            description="Read operator-owned SQLite time series through a generated SELECT only.",
            runtime="native",
            status="stable" if data_root is not None else "unavailable",
            docs_url=SQLITE_DOCS,
            categories=["local", "metering", "time series"],
        )
    )
    registry.add(
        Tool(
            name="sqlite.read_timeseries",
            toolkit="sqlite",
            resource_scope="operator",
            description="Read a bounded operator-owned SQLite table with explicit timestamp and value columns.",
            input_schema=schema(
                {
                    "path": {"type": "string"},
                    "table": {"type": "string"},
                    "timestamp_column": {"type": "string"},
                    "value_column": {"type": "string"},
                    "unit": {"type": "string", "default": "provider-defined"},
                    "kind": {
                        "type": "string",
                        "enum": [kind.value for kind in DataKind],
                        "default": DataKind.METERED.value,
                    },
                    "timezone": {"type": "string", "default": "UTC"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_ROWS,
                        "default": _MAX_ROWS,
                    },
                },
                required=["path", "table", "timestamp_column", "value_column"],
            ),
            capabilities=["read_local_timeseries", "sqlite_timeseries", "read-only telemetry"],
            actions={Action.READ},
            result_kind=None,
            result_unit=None,
        ),
        lambda args, ctx: _sqlite_read(args, ctx, data_root),
    )
