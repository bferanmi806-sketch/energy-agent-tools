"""HTTP connectors for public and self-hosted energy services.

The connector handlers deliberately accept only user data and an execution
context.  API credentials are resolved by the caller and are never part of a
tool schema, result, error message, or provenance record.

The public endpoints implemented here are documented by their providers:

* NESO Carbon Intensity API: https://api.carbonintensity.org.uk/
* Open-Meteo Forecast API: https://open-meteo.com/en/docs
* Octopus REST API: https://docs.octopus.energy/rest/guides/endpoints/
* Home Assistant REST API: https://developers.home-assistant.io/docs/api/rest/
* Emoncms feed API: https://openenergymonitor.org/docs/emoncms/feed.html
* Elexon Insights API: https://developer.data.elexon.co.uk/

Only Home Assistant and Emoncms accept an account-configured base URL.  Their
request paths and pagination URLs are constrained to that configured origin.
All other origins are constants in this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    QuantityShape,
    Tool,
    Toolkit,
    schema,
)
from ..registry import Registry

CARBON_BASE = "https://api.carbonintensity.org.uk"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
OCTOPUS_BASE = "https://api.octopus.energy/v1"
ELEXON_BASE = "https://data.elexon.co.uk/bmrs/api/v1"

CARBON_DOCS = "https://api.carbonintensity.org.uk/"
OPEN_METEO_DOCS = "https://open-meteo.com/en/docs"
OCTOPUS_DOCS = "https://docs.octopus.energy/rest/guides/endpoints/"
HA_DOCS = "https://developers.home-assistant.io/docs/api/rest/"
EMON_DOCS = "https://openenergymonitor.org/docs/emoncms/feed.html"
ELEXON_DOCS = "https://developer.data.elexon.co.uk/"

MAX_PAGES = 20
MAX_ROWS = 10_000
MAX_RANGE = timedelta(days=31)


def _error(code: str, message: str, retryable: bool = False) -> EnergyError:
    """Create an error whose message contains no URL, credential, or body."""

    return EnergyError(code, message, retryable=retryable)


def _parse_time(value: Any, field: str, *, allow_date: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            if allow_date and len(text) == 10 and text[4] == "-" and text[7] == "-":
                parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("invalid_time", f"{field} must be an ISO-8601 timestamp.") from exc
    else:
        raise _error("invalid_time", f"{field} must be an ISO-8601 timestamp.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error("naive_time", f"{field} must include a UTC offset.")
    return parsed.astimezone(UTC)


def _time_range(
    args: Mapping[str, Any],
    *,
    start_names: tuple[str, ...] = ("start", "from"),
    end_names: tuple[str, ...] = ("end", "to"),
    allow_date: bool = False,
    maximum: timedelta = MAX_RANGE,
    allow_open_end: bool = False,
) -> tuple[datetime | None, datetime | None]:
    start_value = next((args.get(name) for name in start_names if args.get(name) is not None), None)
    end_value = next((args.get(name) for name in end_names if args.get(name) is not None), None)
    start = (
        _parse_time(start_value, start_names[0], allow_date=allow_date)
        if start_value is not None
        else None
    )
    end = (
        _parse_time(end_value, end_names[0], allow_date=allow_date)
        if end_value is not None
        else None
    )
    if (start is None) != (end is None) and not (allow_open_end and start is not None):
        raise _error("invalid_time_range", "Both start and end are required together.")
    if start is not None and end is not None:
        if end <= start:
            raise _error("invalid_time_range", "end must be after start.")
        if end - start > maximum:
            raise _error("range_too_large", "The requested time range is too large.")
    return start, end


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_number(value: Any, field: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("malformed_response", f"Provider returned a non-numeric {field}.")
    if not math.isfinite(float(value)):
        raise _error("malformed_response", f"Provider returned an invalid {field}.")
    return float(value)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme.lower(), (url.host or "").lower(), url.port


def _same_origin(url: str | httpx.URL, base: str | httpx.URL) -> bool:
    try:
        candidate = httpx.URL(url)
        origin = httpx.URL(base)
    except Exception:
        return False
    return _origin(candidate) == _origin(origin)


def _account_settings(ctx: ExecutionContext, provider: str) -> Mapping[str, Any]:
    if ctx.account is None:
        raise _error("account_required", f"A connected {provider} account is required.")
    settings = ctx.account.settings
    if not isinstance(settings, Mapping):
        raise _error("invalid_settings", f"The {provider} account settings are invalid.")
    return settings


def _configured_base(ctx: ExecutionContext, provider: str) -> str:
    settings = _account_settings(ctx, provider)
    value = settings.get("base_url") or settings.get("url") or settings.get("host")
    if not isinstance(value, str) or not value.strip():
        raise _error("missing_base_url", f"The {provider} account has no base URL configured.")
    try:
        parsed = httpx.URL(value.strip())
    except Exception as exc:
        raise _error("invalid_base_url", f"The {provider} base URL is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.username
        or parsed.password
    ):
        raise _error("invalid_base_url", f"The {provider} base URL is invalid.")
    # Joining relative paths below requires a trailing slash to preserve an
    # account's optional path prefix (for example /emoncms/).
    return str(parsed).rstrip("/") + "/"


def _relative_url(base: str, path: str) -> str:
    return str(httpx.URL(base).join(path.lstrip("/")))


async def _request_json(
    ctx: ExecutionContext,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    expected_origin: str | None = None,
) -> Any:
    """Issue one JSON GET without following redirects or exposing body text."""

    request_kwargs: dict[str, Any] = {
        "headers": dict(headers or {}),
        "auth": auth,
        "follow_redirects": False,
    }
    # Passing params={} to httpx replaces a URL's existing query string.  A
    # pagination URL already contains its cursor and must be requested as-is.
    if params is not None:
        request_kwargs["params"] = dict(params)
    try:
        response = await ctx.http.request(
            "GET",
            url,
            **request_kwargs,
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
    if expected_origin is not None and not _same_origin(response.url, expected_origin):
        raise _error("unsafe_redirect", "The provider response crossed the configured origin.")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise _error("malformed_response", "The energy provider returned invalid JSON.") from exc


def _provenance(provider: str, docs_url: str, endpoint: str) -> list[dict[str, str]]:
    return [{"provider": provider, "endpoint": endpoint, "documentation": docs_url}]


def _result(
    data: Any,
    *,
    kind: DataKind,
    unit: str,
    source: str,
    docs_url: str,
    endpoint: str,
    resolution: str | None = None,
    warnings: list[str] | None = None,
    quality: str = "provider-reported",
    quantity_shape: QuantityShape | None = None,
) -> EnergyResult:
    starts: list[datetime] = []
    ends: list[datetime] = []
    durations: set[int] = set()
    field_units: dict[str, str] = {}
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            start_text = row.get("from") or row.get("timestamp")
            end_text = row.get("to")
            if isinstance(start_text, str):
                try:
                    start = _parse_time(start_text, "timestamp")
                    starts.append(start)
                    if isinstance(end_text, str):
                        end = _parse_time(end_text, "interval end")
                        if end > start:
                            ends.append(end)
                            durations.add(int((end - start).total_seconds()))
                except EnergyError:
                    pass  # Preserve provider warnings; unknown coverage stays unspecified.
            if isinstance(row.get("variable"), str) and isinstance(row.get("unit"), str):
                field_units[row["variable"]] = row["unit"]
    if (
        resolution in {"provider interval", "provider tariff period"}
        and len(durations) == 1
        and len(ends) == len(data)
    ):
        resolution = f"{next(iter(durations))}s"
    if resolution == "30m":
        resolution = "30min"
    return EnergyResult(
        data=data,
        kind=kind,
        quantity_shape=quantity_shape,
        unit=unit,
        source=source,
        timezone="UTC",
        resolution=resolution,
        provider=source,
        original_unit=unit,
        field_units=field_units,
        time_start=min(starts) if starts else None,
        time_end=max(ends) if ends else None,
        assumptions=[],
        warnings=warnings or [],
        quality=quality,
        provenance=_provenance(source, docs_url, endpoint),
    )


def _rows(payload: Any, *, key: str = "data") -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and key in payload:
        values = payload[key]
    else:
        raise _error("malformed_response", "The energy provider returned an unexpected JSON shape.")
    if values is None:
        raise _error("malformed_response", "The energy provider returned a null data collection.")
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise _error(
            "malformed_response", "The energy provider returned an invalid data collection."
        )
    if len(values) > MAX_ROWS:
        raise _error("result_too_large", "The provider returned more rows than this tool permits.")
    return values


def _pagination_url(next_url: Any, origin: str) -> str | None:
    if next_url in (None, ""):
        return None
    if not isinstance(next_url, str):
        raise _error("malformed_response", "The provider returned an invalid pagination URL.")
    candidate = str(httpx.URL(origin).join(next_url))
    if not _same_origin(candidate, origin):
        raise _error("unsafe_redirect", "The provider returned a pagination URL on another origin.")
    return candidate


async def _paginated_json(
    ctx: ExecutionContext,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    origin = url
    current = url
    current_params: Mapping[str, Any] | None = params
    collected: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        payload = await _request_json(
            ctx,
            current,
            params=current_params,
            headers=headers,
            auth=auth,
            expected_origin=origin,
        )
        page = _rows(payload, key="results")
        collected.extend(page)
        if len(collected) > MAX_ROWS:
            raise _error(
                "result_too_large", "The provider returned more rows than this tool permits."
            )
        if not isinstance(payload, dict):
            return collected
        current = _pagination_url(payload.get("next"), origin) or ""
        if not current:
            return collected
        current_params = None
    raise _error("pagination_limit", "The provider returned too many pages.")


def _credential(ctx: ExecutionContext, provider: str) -> str:
    if not isinstance(ctx.credential, str) or not ctx.credential:
        raise _error("credential_required", f"A credential is required for {provider}.")
    return ctx.credential


def _safe_segment(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9._~-]+", value):
        raise _error("invalid_identifier", f"{field} is invalid.")
    return quote(value, safe="-._~")


def _carbon_kind(rows: list[dict[str, Any]]) -> DataKind:
    kinds = {row.get("kind") for row in rows}
    if kinds == {DataKind.FORECAST.value}:
        return DataKind.FORECAST
    if kinds == {DataKind.METERED.value}:
        return DataKind.METERED
    return DataKind.CALCULATED


def _carbon_records(
    payload: Any, region_id: int | None, postcode: str | None
) -> list[dict[str, Any]]:
    """Flatten national and all four regional Carbon Intensity response shapes."""

    if not isinstance(payload, dict) or "data" not in payload:
        raise _error("malformed_response", "Carbon Intensity returned an unexpected JSON shape.")
    data = payload["data"]
    if data is None:
        raise _error("malformed_response", "Carbon Intensity returned null data.")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        # /regional/intensity/.../{regionid,postcode} returns one region object.
        metadata = {key: value for key, value in data.items() if key != "data"}
        return [dict(metadata, **record) for record in _rows(data, key="data")]
    if not isinstance(data, list):
        raise _error("malformed_response", "Carbon Intensity returned invalid data.")
    flattened: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise _error("malformed_response", "Carbon Intensity returned invalid regional data.")
        regions = item.get("regions")
        if isinstance(regions, list):
            outer = {key: value for key, value in item.items() if key != "regions"}
            for region in regions:
                if not isinstance(region, dict):
                    raise _error(
                        "malformed_response", "Carbon Intensity returned invalid region data."
                    )
                if region_id is not None and region.get("regionid") != region_id:
                    continue
                if postcode is not None and region.get("postcode", postcode) != postcode:
                    continue
                flattened.append(dict(outer, **region))
            continue
        nested = item.get("data")
        if isinstance(nested, list):
            metadata = {key: value for key, value in item.items() if key != "data"}
            for record in nested:
                if not isinstance(record, dict):
                    raise _error(
                        "malformed_response", "Carbon Intensity returned invalid regional data."
                    )
                flattened.append(dict(metadata, **record))
            continue
        flattened.append(item)
    if len(flattened) > MAX_ROWS:
        raise _error("result_too_large", "The provider returned more rows than this tool permits.")
    return flattened


async def _carbon_intensity(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    start, end = _time_range(args, maximum=timedelta(days=7), allow_open_end=True)
    region_id = args.get("region_id")
    postcode = args.get("postcode")
    if region_id is not None and postcode is not None:
        raise _error("invalid_region", "Choose region_id or postcode, not both.")
    if region_id is not None:
        if (
            isinstance(region_id, bool)
            or not isinstance(region_id, int)
            or not 1 <= region_id <= 17
        ):
            raise _error("invalid_region", "region_id must be an integer from 1 to 17.")
    if postcode is not None:
        if not isinstance(postcode, str) or not re.fullmatch(
            r"[A-Za-z]{1,2}\d[A-Za-z\d]?", postcode.strip()
        ):
            raise _error("invalid_region", "postcode must be a valid outward postcode.")
        postcode = postcode.strip().upper()

    regional = region_id is not None or postcode is not None
    if not regional:
        prefix = "/intensity"
        if start is None:
            endpoint = prefix
        elif end is not None:
            endpoint = f"{prefix}/{_iso(start)}/{_iso(end)}"
        else:
            horizon = args.get("horizon_hours", 24)
            if horizon not in {24, 48}:
                raise _error("invalid_horizon", "horizon_hours must be 24 or 48.")
            endpoint = f"{prefix}/{_iso(start)}/fw{horizon}h"
    elif start is None:
        endpoint = "/regional"
        if region_id is not None:
            endpoint += f"/regionid/{region_id}"
        elif postcode is not None:
            endpoint += f"/postcode/{quote(postcode, safe='')}"
    else:
        prefix = "/regional/intensity"
        if end is not None:
            endpoint = f"{prefix}/{_iso(start)}/{_iso(end)}"
        else:
            horizon = args.get("horizon_hours", 24)
            if horizon not in {24, 48}:
                raise _error("invalid_horizon", "horizon_hours must be 24 or 48.")
            endpoint = f"{prefix}/{_iso(start)}/fw{horizon}h"
        if region_id is not None:
            endpoint += f"/regionid/{region_id}"
        elif postcode is not None:
            endpoint += f"/postcode/{quote(postcode, safe='')}"

    payload = await _request_json(ctx, CARBON_BASE + endpoint)
    records = _carbon_records(payload, region_id, postcode)
    result_rows: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record.get("intensity"), dict):
            raise _error(
                "malformed_response", "Carbon Intensity returned an invalid intensity row."
            )
        intensity = record["intensity"]
        actual = _json_number(intensity.get("actual"), "actual carbon intensity", allow_none=True)
        forecast = _json_number(
            intensity.get("forecast"), "forecast carbon intensity", allow_none=True
        )
        if actual is None and forecast is None:
            raise _error("malformed_response", "Carbon Intensity returned no intensity value.")
        value = actual if actual is not None else forecast
        # Carbon intensity is an emissions calculation even when the API marks
        # a value as actual; it is never a meter reading.  Keep the API's
        # actual/forecast distinction in ``status`` and in each row's kind.
        row_kind = DataKind.CALCULATED.value if actual is not None else DataKind.FORECAST.value
        row: dict[str, Any] = {
            "from": record.get("from"),
            "to": record.get("to"),
            "value": value,
            "unit": "gCO2/kWh",
            "kind": row_kind,
            "status": "actual" if actual is not None else "forecast",
            "actual": actual,
            "forecast": forecast,
            "index": intensity.get("index"),
        }
        if isinstance(record.get("generationmix"), list):
            row["generation_mix"] = record["generationmix"]
        for key in ("regionid", "shortname", "dnoregion", "postcode"):
            if key in record:
                row[key] = record[key]
        result_rows.append(row)
    return _result(
        result_rows,
        kind=_carbon_kind(result_rows),
        unit="gCO2/kWh",
        source="carbon-intensity-gb",
        docs_url=CARBON_DOCS,
        endpoint=endpoint,
        resolution="30m",
    )


OPEN_METEO_UNITS = {
    "temperature_2m": "°C",
    "apparent_temperature": "°C",
    "relative_humidity_2m": "%",
    "cloud_cover": "%",
    "precipitation": "mm",
    "rain": "mm",
    "showers": "mm",
    "snowfall": "cm",
    "wind_speed_10m": "km/h",
    "wind_gusts_10m": "km/h",
    "wind_direction_10m": "°",
    "shortwave_radiation": "W/m²",
    "direct_radiation": "W/m²",
    "diffuse_radiation": "W/m²",
    "direct_normal_irradiance": "W/m²",
    "global_tilted_irradiance": "W/m²",
    "shortwave_radiation_instant": "W/m²",
    "weather_code": "wmo code",
}
OPEN_METEO_DEFAULTS = ["temperature_2m", "cloud_cover", "wind_speed_10m", "shortwave_radiation"]


async def _open_meteo_forecast(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    latitude = args.get("latitude")
    longitude = args.get("longitude")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or not -90 <= latitude <= 90
    ):
        raise _error("invalid_location", "latitude must be between -90 and 90.")
    if (
        isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not -180 <= longitude <= 180
    ):
        raise _error("invalid_location", "longitude must be between -180 and 180.")
    start, end = _time_range(args, allow_date=True, maximum=timedelta(days=16))
    variables = args.get("variables", OPEN_METEO_DEFAULTS)
    if not isinstance(variables, list) or not variables or len(variables) > 20:
        raise _error("invalid_variables", "variables must be a non-empty list of at most 20 names.")
    if any(
        not isinstance(variable, str) or variable not in OPEN_METEO_UNITS for variable in variables
    ):
        raise _error("invalid_variables", "One or more Open-Meteo variables are unsupported.")
    if start is not None:
        assert end is not None
        query: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(dict.fromkeys(variables)),
            "timezone": "UTC",
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat() if end else start.date().isoformat(),
        }
    else:
        forecast_days = args.get("forecast_days", 1)
        if (
            isinstance(forecast_days, bool)
            or not isinstance(forecast_days, int)
            or not 1 <= forecast_days <= 16
        ):
            raise _error("invalid_forecast_days", "forecast_days must be an integer from 1 to 16.")
        query = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(dict.fromkeys(variables)),
            "timezone": "UTC",
            "forecast_days": forecast_days,
        }
    payload = await _request_json(ctx, OPEN_METEO_BASE, params=query)
    if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
        raise _error("malformed_response", "Open-Meteo returned no hourly data.")
    hourly = payload["hourly"]
    times = hourly.get("time")
    if not isinstance(times, list):
        raise _error("malformed_response", "Open-Meteo returned invalid hourly timestamps.")
    unique_variables = list(dict.fromkeys(variables))
    for variable in unique_variables:
        values = hourly.get(variable)
        if not isinstance(values, list) or len(values) != len(times):
            raise _error("malformed_response", "Open-Meteo returned mismatched hourly columns.")
    provider_units = payload.get("hourly_units")
    if provider_units is None:
        provider_units = {}
    if not isinstance(provider_units, dict):
        raise _error("malformed_response", "Open-Meteo returned invalid hourly units.")
    units = {
        variable: provider_units.get(variable, OPEN_METEO_UNITS[variable])
        for variable in unique_variables
    }
    if any(not isinstance(unit_value, str) for unit_value in units.values()):
        raise _error("malformed_response", "Open-Meteo returned invalid hourly units.")
    null_values = False
    range_end = end if end is not None else datetime.max.replace(tzinfo=UTC)
    result_rows: list[dict[str, Any]] = []
    for index, time_value in enumerate(times):
        try:
            timestamp = _parse_time(time_value, "hourly time", allow_date=False)
        except EnergyError:
            # The API returns UTC timestamps without an offset when timezone=UTC.
            if isinstance(time_value, str) and len(time_value) == 16:
                timestamp = _parse_time(time_value + "+00:00", "hourly time")
            else:
                raise
        if start is not None and (timestamp < start or timestamp >= range_end):
            continue
        for variable in unique_variables:
            if hourly[variable][index] is None:
                null_values = True
            result_rows.append(
                {
                    "timestamp": _iso(timestamp),
                    "variable": variable,
                    "value": hourly[variable][index],
                    "unit": units[variable],
                    "kind": DataKind.FORECAST.value,
                }
            )
    unit_values = {units[variable] for variable in unique_variables}
    unit = next(iter(unit_values)) if len(unit_values) == 1 else "mixed"
    warnings = (
        ["Open-Meteo returned null values for part of the requested forecast."]
        if null_values
        else []
    )
    return _result(
        result_rows,
        kind=DataKind.FORECAST,
        unit=unit,
        source="open-meteo",
        docs_url=OPEN_METEO_DOCS,
        endpoint="/v1/forecast",
        resolution="1h",
        warnings=warnings,
    )


def _octopus_settings(ctx: ExecutionContext) -> Mapping[str, Any]:
    return _account_settings(ctx, "Octopus Energy")


def _octopus_meter(ctx: ExecutionContext) -> tuple[str, str]:
    settings = _octopus_settings(ctx)
    mpan = (
        settings.get("mpan")
        or settings.get("meter_point")
        or settings.get("electricity_meter_point")
    )
    serial = (
        settings.get("serial_number")
        or settings.get("meter_serial")
        or settings.get("meter_serial_number")
    )
    return _safe_segment(mpan, "mpan"), _safe_segment(serial, "serial_number")


def _octopus_product_tariff(args: Mapping[str, Any], ctx: ExecutionContext) -> tuple[str, str]:
    # Published product rates are public.  An account is optional when the
    # product/tariff codes are supplied in the call; configured settings are a
    # convenience for installations that keep those identifiers locally.
    settings: Mapping[str, Any] = {}
    if ctx.account is not None:
        settings = ctx.account.settings
    product = args.get("product_code") or settings.get("product_code")
    tariff = args.get("tariff_code") or settings.get("tariff_code")
    return _safe_segment(product, "product_code"), _safe_segment(tariff, "tariff_code")


def _octopus_period_params(args: Mapping[str, Any], *, forward: bool = False) -> dict[str, Any]:
    start, end = _time_range(args, maximum=timedelta(days=31))
    if start is None:
        now = datetime.now(UTC)
        start, end = (now, now + timedelta(days=2)) if forward else (now - timedelta(days=1), now)
    assert end is not None
    return {
        "period_from": _iso(start),
        "period_to": _iso(end),
        "page_size": min(int(args.get("page_size", 2500)), 2500),
    }


async def _octopus_consumption(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    mpan, serial = _octopus_meter(ctx)
    credential = _credential(ctx, "Octopus Energy")
    endpoint = f"/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    values = await _paginated_json(
        ctx,
        OCTOPUS_BASE + endpoint,
        params=_octopus_period_params(args),
        auth=httpx.BasicAuth(credential, ""),
    )
    result_rows: list[dict[str, Any]] = []
    for value in values:
        start_text = value.get("interval_start")
        end_text = value.get("interval_end")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            raise _error("malformed_response", "Octopus returned an invalid consumption interval.")
        start = _parse_time(start_text, "interval_start")
        end = _parse_time(end_text, "interval_end")
        if end <= start:
            raise _error(
                "malformed_response", "Octopus returned a non-positive consumption interval."
            )
        consumption = _json_number(value.get("consumption"), "consumption")
        result_rows.append(
            {
                "from": _iso(start),
                "to": _iso(end),
                "value": consumption,
                "unit": "kWh",
                "kind": DataKind.METERED.value,
            }
        )
    return _result(
        result_rows,
        kind=DataKind.METERED,
        unit="kWh",
        source="octopus-energy",
        docs_url=OCTOPUS_DOCS,
        endpoint=endpoint,
        resolution="provider interval",
    )


async def _octopus_tariffs(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    product, tariff = _octopus_product_tariff(args, ctx)
    credential = ctx.credential
    endpoint = f"/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/"
    auth = httpx.BasicAuth(credential, "") if isinstance(credential, str) and credential else None
    values = await _paginated_json(
        ctx,
        OCTOPUS_BASE + endpoint,
        params=_octopus_period_params(args, forward=True),
        auth=auth,
    )
    result_rows: list[dict[str, Any]] = []
    for value in values:
        valid_from = value.get("valid_from")
        valid_to = value.get("valid_to")
        if not isinstance(valid_from, str):
            raise _error("malformed_response", "Octopus returned an invalid tariff interval.")
        start = _parse_time(valid_from, "valid_from")
        end = _parse_time(valid_to, "valid_to") if isinstance(valid_to, str) else None
        if end is not None and end <= start:
            raise _error("malformed_response", "Octopus returned a non-positive tariff interval.")
        inc = _json_number(value.get("value_inc_vat"), "value_inc_vat", allow_none=True)
        exc = _json_number(value.get("value_exc_vat"), "value_exc_vat", allow_none=True)
        if inc is None and exc is None:
            raise _error("malformed_response", "Octopus returned a tariff without a price.")
        row: dict[str, Any] = {
            "from": _iso(start),
            "to": _iso(end) if end else None,
            "value": inc if inc is not None else exc,
            "unit": "p/kWh",
            "kind": DataKind.CALCULATED.value,
            "value_inc_vat": inc,
            "value_exc_vat": exc,
        }
        if "standing_charge_inc_vat" in value:
            row["standing_charge_inc_vat"] = value["standing_charge_inc_vat"]
        result_rows.append(row)
    return _result(
        result_rows,
        kind=DataKind.CALCULATED,
        unit="p/kWh",
        source="octopus-energy",
        docs_url=OCTOPUS_DOCS,
        endpoint=endpoint,
        resolution="provider tariff period",
    )


def _ha_headers(ctx: ExecutionContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {_credential(ctx, 'Home Assistant')}"}


def _ha_entity(value: Any) -> str:
    if not isinstance(value, str) or not value or "/" in value or "?" in value or "#" in value:
        raise _error("invalid_entity", "entity_id is invalid.")
    return quote(value, safe="._:-")


def _ha_kind(attributes: Mapping[str, Any]) -> DataKind:
    """Infer measurement semantics from HA's state_class metadata."""

    state_class = attributes.get("state_class")
    if state_class in {"measurement", "total", "total_increasing"}:
        return DataKind.METERED
    if state_class == "calculated":
        return DataKind.CALCULATED
    # HA permits arbitrary state sensors.  Without a state_class the value's
    # physical semantics are unknown, so do not present it as a meter read.
    return DataKind.ESTIMATED


def _ha_common_result(
    data: Any,
    *,
    kind: DataKind,
    unit: str,
    endpoint: str,
    resolution: str | None = None,
    warnings: list[str] | None = None,
) -> EnergyResult:
    return _result(
        data,
        kind=kind,
        unit=unit,
        source="home-assistant",
        docs_url=HA_DOCS,
        endpoint=endpoint,
        resolution=resolution,
        warnings=warnings,
    )


def _ha_shape(attributes: Mapping[str, Any], ctx: ExecutionContext) -> QuantityShape | None:
    if attributes.get("state_class") in {"total", "total_increasing"}:
        return "counter"
    declared = ctx.account.settings.get("quantity_shape") if ctx.account else None
    if declared in {"interval", "instantaneous", "counter"}:
        return declared
    return "instantaneous" if attributes.get("state_class") == "measurement" else None


async def _ha_state(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    base = _configured_base(ctx, "Home Assistant")
    entity = _ha_entity(args.get("entity_id"))
    endpoint = f"/api/states/{entity}"
    payload = await _request_json(
        ctx,
        _relative_url(base, endpoint),
        headers=_ha_headers(ctx),
        expected_origin=base,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("entity_id"), str):
        raise _error("malformed_response", "Home Assistant returned an invalid state.")
    attrs = payload.get("attributes")
    if attrs is None:
        attrs = {}
    if not isinstance(attrs, dict):
        raise _error("malformed_response", "Home Assistant returned invalid state attributes.")
    kind = _ha_kind(attrs)
    warnings = (
        []
        if attrs.get("state_class")
        else [
            "Home Assistant entity has no state_class; telemetry semantics are treated as estimated."
        ]
    )
    state = {
        "entity_id": payload["entity_id"],
        "state": payload.get("state"),
        "attributes": attrs,
        "last_changed": payload.get("last_changed"),
        "last_updated": payload.get("last_updated"),
        "unit": attrs.get("unit_of_measurement", "state"),
        "kind": kind.value,
    }
    result = _ha_common_result(
        state,
        kind=kind,
        unit=str(state["unit"]),
        endpoint=endpoint,
        resolution="state update",
        warnings=warnings,
    )

    result.quantity_shape = _ha_shape(attrs, ctx)
    observed = payload.get("last_updated") or payload.get("last_changed")
    if observed:
        result.time_start = _parse_time(observed, "state timestamp")
        result.time_end = result.time_start
    return result


async def _ha_history(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    base = _configured_base(ctx, "Home Assistant")
    entity = _ha_entity(args.get("entity_id"))
    start, end = _time_range(args, maximum=MAX_RANGE)
    if start is None or end is None:
        raise _error("invalid_time_range", "Home Assistant history requires start and end.")
    endpoint = f"/api/history/period/{quote(_iso(start), safe=':TZ-')}"
    params: dict[str, Any] = {
        "filter_entity_id": entity,
        "end_time": _iso(end),
        "minimal_response": "false",
        "significant_changes_only": "false",
    }
    payload = await _request_json(
        ctx,
        _relative_url(base, endpoint),
        params=params,
        headers=_ha_headers(ctx),
        expected_origin=base,
    )
    if payload is None or not isinstance(payload, list):
        raise _error("malformed_response", "Home Assistant returned an invalid history response.")
    history: list[dict[str, Any]] = []
    unit_values: set[str] = set()
    kinds: set[DataKind] = set()
    shapes: set[QuantityShape | None] = set()
    warnings: list[str] = []
    for entity_history in payload:
        if not isinstance(entity_history, list):
            raise _error("malformed_response", "Home Assistant returned invalid history rows.")
        for item in entity_history:
            if not isinstance(item, dict):
                raise _error("malformed_response", "Home Assistant returned invalid history rows.")
            attrs = item.get("attributes")
            if attrs is None:
                attrs = {}
            if not isinstance(attrs, dict):
                raise _error(
                    "malformed_response", "Home Assistant returned invalid history attributes."
                )
            shapes.add(_ha_shape(attrs, ctx))
            unit = str(attrs.get("unit_of_measurement", "state"))
            unit_values.add(unit)
            kind = _ha_kind(attrs)
            kinds.add(kind)
            if (
                not attrs.get("state_class")
                and "Home Assistant entity has no state_class; telemetry semantics are treated as estimated."
                not in warnings
            ):
                warnings.append(
                    "Home Assistant entity has no state_class; telemetry semantics are treated as estimated."
                )
            timestamp = item.get("last_updated") or item.get("last_changed")
            if isinstance(timestamp, str):
                timestamp = _iso(_parse_time(timestamp, "history timestamp"))
            history.append(
                {
                    "entity_id": item.get("entity_id", args.get("entity_id")),
                    "timestamp": timestamp,
                    "state": item.get("state"),
                    "value": _ha_numeric(item.get("state")),
                    "attributes": attrs,
                    "unit": unit,
                    "kind": kind.value,
                }
            )
    unit = next(iter(unit_values)) if len(unit_values) == 1 else "mixed"
    result_kind = next(iter(kinds)) if len(kinds) == 1 else DataKind.CALCULATED
    result = _ha_common_result(
        history,
        kind=result_kind,
        unit=unit,
        endpoint="/api/history/period",
        resolution="state update",
        warnings=warnings,
    )

    result.quantity_shape = next(iter(shapes)) if len(shapes) == 1 else None
    return result


def _ha_numeric(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _emon_feed_id(args: Mapping[str, Any], ctx: ExecutionContext) -> str:
    settings = _account_settings(ctx, "OpenEnergyMonitor")
    feed_id = args.get("feed_id") or settings.get("feed_id") or settings.get("feed")
    if isinstance(feed_id, bool) or not isinstance(feed_id, (int, str)) or str(feed_id) == "":
        raise _error("missing_feed_id", "An Emoncms feed_id is required.")
    if not re.fullmatch(r"\d+", str(feed_id)):
        raise _error("invalid_feed_id", "feed_id must be a numeric feed identifier.")
    return str(feed_id)


async def _emon_feed(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    base = _configured_base(ctx, "OpenEnergyMonitor")
    credential = _credential(ctx, "OpenEnergyMonitor")
    feed_id = _emon_feed_id(args, ctx)
    start, end = _time_range(args, maximum=MAX_RANGE)
    unit = (
        args.get("unit")
        or _account_settings(ctx, "OpenEnergyMonitor").get("unit")
        or "provider-defined"
    )
    if not isinstance(unit, str):
        raise _error("invalid_unit", "The Emoncms feed unit is invalid.")
    if start is None:
        endpoint = "/feed/value.json"
        payload = await _request_json(
            ctx,
            _relative_url(base, endpoint),
            params={"id": feed_id, "apikey": credential},
            expected_origin=base,
        )
        if payload is None:
            raise _error("malformed_response", "Emoncms returned a null feed response.")
        if isinstance(payload, (dict, list)):
            # Some installations return {value: ...} while the documented API
            # returns a scalar.  Preserve either form without guessing units.
            if isinstance(payload, dict) and "value" in payload:
                value = payload["value"]
            else:
                raise _error("malformed_response", "Emoncms returned an invalid feed value.")
        else:
            value = payload
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise _error("malformed_response", "Emoncms returned a non-numeric feed value.")
        return _result(
            [
                {
                    "timestamp": None,
                    "value": value,
                    "unit": unit,
                    "kind": DataKind.METERED.value,
                }
            ],
            kind=DataKind.METERED,
            unit=unit,
            source="openenergymonitor",
            quantity_shape=_account_settings(ctx, "OpenEnergyMonitor").get("quantity_shape"),
            docs_url=EMON_DOCS,
            endpoint=endpoint,
            warnings=[
                "Emoncms feed units are provider-defined unless configured on the account.",
                "The scalar feed endpoint supplies no observation timestamp; freshness is unknown.",
            ],
        )

    interval = args.get("interval", 60)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 86_400:
        raise _error("invalid_interval", "interval must be an integer from 1 to 86400 seconds.")
    assert end is not None
    endpoint = "/feed/data.json"
    params = {
        "id": feed_id,
        "start": int(start.timestamp() * 1000),
        "end": int(end.timestamp() * 1000),
        "interval": interval,
        "apikey": credential,
    }
    payload = await _request_json(
        ctx, _relative_url(base, endpoint), params=params, expected_origin=base
    )
    if payload is None:
        raise _error("malformed_response", "Emoncms returned a null feed response.")
    if not isinstance(payload, list) or any(
        not isinstance(row, list) or len(row) < 2 for row in payload
    ):
        raise _error("malformed_response", "Emoncms returned invalid feed rows.")
    if len(payload) > MAX_ROWS:
        raise _error("result_too_large", "The provider returned more rows than this tool permits.")
    null_values = False
    values: list[dict[str, Any]] = []
    for row in payload:
        stamp = row[0]
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise _error("malformed_response", "Emoncms returned an invalid feed timestamp.")
        seconds = float(stamp) / (1000 if float(stamp) > 100_000_000_000 else 1)
        timestamp = datetime.fromtimestamp(seconds, UTC)
        value = row[1]
        if value is None:
            null_values = True
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise _error("malformed_response", "Emoncms returned an invalid feed value.")
        values.append(
            {
                "timestamp": _iso(timestamp),
                "value": value,
                "unit": unit,
                "kind": DataKind.METERED.value,
            }
        )
    warnings = ["Emoncms feed units are provider-defined unless configured on the account."]
    if null_values:
        warnings.append("Emoncms returned null values for part of the requested feed.")
    return _result(
        values,
        kind=DataKind.METERED,
        unit=unit,
        source="openenergymonitor",
        quantity_shape=_account_settings(ctx, "OpenEnergyMonitor").get("quantity_shape"),
        docs_url=EMON_DOCS,
        endpoint=endpoint,
        resolution=f"{interval}s",
        warnings=warnings,
    )


ELEXON_DATASETS = {
    "FUELHH",
    "FUELINST",
    "INDO",
    "INDDEM",
    "INDGEN",
    "NDF",
    "NDFD",
    "TSDF",
    "TSDFD",
    "WINDFOR",
}
ELEXON_FORECASTS = {"INDDEM", "INDGEN", "NDF", "NDFD", "TSDF", "TSDFD", "WINDFOR"}


def _elexon_value(row: Mapping[str, Any]) -> Any:
    for name in (
        "generation",
        "demand",
        "quantity",
        "outputUsable",
        "margin",
        "windGeneration",
        "solarGeneration",
        "value",
    ):
        if name in row:
            return row[name]
    return None


def _elexon_time(row: Mapping[str, Any]) -> str | None:
    for name in ("startTime", "measurementTime", "publishTime", "effectiveFrom"):
        value = row.get(name)
        if isinstance(value, str):
            try:
                return _iso(_parse_time(value, name))
            except EnergyError:
                return value
    return None


async def _elexon_grid(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
    dataset = args.get("dataset", "FUELHH")
    if not isinstance(dataset, str) or dataset not in ELEXON_DATASETS:
        raise _error("invalid_dataset", "The Elexon dataset is unsupported by this tool.")
    start, end = _time_range(args, maximum=timedelta(days=31))
    endpoint = f"/datasets/{dataset}"
    params: dict[str, Any] = {"format": "json"}
    if start is not None:
        assert end is not None
        params["publishDateTimeFrom"] = _iso(start)
        params["publishDateTimeTo"] = _iso(end)
    fuel_type = args.get("fuel_type")
    if fuel_type is not None:
        if not isinstance(fuel_type, list) or any(not isinstance(item, str) for item in fuel_type):
            raise _error("invalid_fuel_type", "fuel_type must be a list of strings.")
        params["fuelType"] = fuel_type
    payload = await _request_json(ctx, ELEXON_BASE + endpoint, params=params)
    records = _rows(payload)
    null_values = False
    result_rows: list[dict[str, Any]] = []
    for original in records:
        row = dict(original)
        value = _elexon_value(original)
        if value is None:
            null_values = True
        if value is not None:
            value = _json_number(value, "grid value", allow_none=True)
        row["timestamp"] = _elexon_time(original)
        row["value"] = value
        row["unit"] = "MW"
        row["kind"] = (
            DataKind.FORECAST.value if dataset in ELEXON_FORECASTS else DataKind.METERED.value
        )
        result_rows.append(row)
    kind = DataKind.FORECAST if dataset in ELEXON_FORECASTS else DataKind.METERED
    warnings = (
        ["Elexon returned null values for part of the requested dataset."] if null_values else []
    )
    return _result(
        result_rows,
        kind=kind,
        unit="MW",
        source="elexon",
        docs_url=ELEXON_DOCS,
        endpoint=endpoint,
        resolution="30m"
        if dataset in {"FUELHH", "INDDEM", "INDGEN", "NDF", "NDFD", "TSDF", "TSDFD"}
        else None,
        warnings=warnings,
    )


def _toolkits() -> list[Toolkit]:
    return [
        Toolkit(
            id="carbon-intensity-gb",
            name="Carbon Intensity GB",
            description="NESO Great Britain carbon intensity actuals and forecasts.",
            runtime="http",
            status="stable",
            docs_url=CARBON_DOCS,
        ),
        Toolkit(
            id="open-meteo",
            name="Open-Meteo",
            description="Hourly weather and solar radiation forecasts.",
            runtime="http",
            status="stable",
            docs_url=OPEN_METEO_DOCS,
        ),
        Toolkit(
            id="octopus-energy",
            name="Octopus Energy",
            description="Octopus smart meter consumption and published electricity tariffs.",
            runtime="http",
            status="stable",
            docs_url=OCTOPUS_DOCS,
        ),
        Toolkit(
            id="octopus-energy-account",
            name="Octopus Energy Account",
            description="Octopus smart meter consumption for a connected account.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=OCTOPUS_DOCS,
        ),
        Toolkit(
            id="home-assistant",
            name="Home Assistant",
            description="Read Home Assistant entity telemetry and history.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=HA_DOCS,
        ),
        Toolkit(
            id="openenergymonitor",
            name="OpenEnergyMonitor",
            description="Read Emoncms feed values and time-series data.",
            runtime="http",
            status="requires credentials",
            auth_required=True,
            docs_url=EMON_DOCS,
        ),
        Toolkit(
            id="elexon",
            name="Elexon Insights",
            description="Public GB grid generation, demand and forecast datasets.",
            runtime="http",
            status="stable",
            docs_url=ELEXON_DOCS,
        ),
    ]


def register(registry: Registry) -> None:
    """Register all HTTP energy toolkits and handlers into ``registry``."""

    for toolkit in _toolkits():
        registry.add_toolkit(toolkit)

    registry.add(
        Tool(
            name="carbon_intensity_gb.get_intensity",
            toolkit="carbon-intensity-gb",
            description="Get current or historical Great Britain carbon intensity, including actual and forecast rows.",
            input_schema=schema(
                {
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "horizon_hours": {"type": "integer", "enum": [24, 48], "default": 24},
                    "region_id": {"type": "integer", "minimum": 1, "maximum": 17},
                    "postcode": {"type": "string"},
                }
            ),
            capabilities=[
                "get_carbon_intensity",
                "carbon intensity",
                "forecast",
                "generation mix",
                "GB grid",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _carbon_intensity,
    )
    registry.add(
        Tool(
            name="open_meteo.get_forecast",
            toolkit="open-meteo",
            description="Get hourly weather and solar radiation forecast values for a latitude and longitude.",
            input_schema=schema(
                {
                    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "forecast_days": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
                    "variables": {"type": "array", "items": {"type": "string"}},
                },
                required=["latitude", "longitude"],
            ),
            capabilities=[
                "get_weather",
                "weather",
                "solar radiation",
                "forecast",
                "wind",
                "temperature",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _open_meteo_forecast,
    )
    registry.add(
        Tool(
            name="octopus_energy.get_consumption",
            toolkit="octopus-energy-account",
            description="Read Octopus electricity meter consumption using meter identifiers from the connected account.",
            input_schema=schema(
                {
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2500,
                        "default": 2500,
                    },
                }
            ),
            capabilities=[
                "get_energy_consumption",
                "consumption",
                "meter",
                "smart meter",
                "electricity",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _octopus_consumption,
    )
    registry.add(
        Tool(
            name="octopus_energy.get_tariffs",
            toolkit="octopus-energy",
            description="Read Octopus published electricity unit rates using product and tariff settings.",
            input_schema=schema(
                {
                    "product_code": {"type": "string"},
                    "tariff_code": {"type": "string"},
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2500,
                        "default": 2500,
                    },
                }
            ),
            capabilities=["get_tariff", "tariff", "price", "electricity", "time of use"],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _octopus_tariffs,
    )
    registry.add(
        Tool(
            name="home_assistant.get_state",
            toolkit="home-assistant",
            description="Read one Home Assistant entity state and its measurement attributes.",
            input_schema=schema({"entity_id": {"type": "string"}}, required=["entity_id"]),
            capabilities=["get_current_power", "telemetry", "home", "building", "sensor"],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _ha_state,
    )
    registry.add(
        Tool(
            name="home_assistant.get_history",
            toolkit="home-assistant",
            description="Read Home Assistant historical state changes for one entity and a bounded UTC range.",
            input_schema=schema(
                {
                    "entity_id": {"type": "string"},
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                },
                required=["entity_id", "start", "end"],
            ),
            capabilities=[
                "get_energy_consumption",
                "get_generation",
                "get_storage_state",
                "telemetry",
                "history",
                "time series",
                "building",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _ha_history,
    )
    registry.add(
        Tool(
            name="openenergymonitor.get_feed",
            toolkit="openenergymonitor",
            description="Read an OpenEnergyMonitor Emoncms feed value or bounded time series.",
            input_schema=schema(
                {
                    "feed_id": {"type": "integer", "minimum": 1},
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "interval": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 60},
                    "unit": {"type": "string"},
                }
            ),
            capabilities=[
                "get_current_power",
                "get_energy_consumption",
                "get_generation",
                "get_storage_state",
                "telemetry",
                "feed",
                "consumption",
                "generation",
                "time series",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _emon_feed,
    )
    registry.add(
        Tool(
            name="elexon.get_grid_data",
            toolkit="elexon",
            description="Read public Elexon Insights generation, demand or forecast datasets in MW.",
            input_schema=schema(
                {
                    "dataset": {
                        "type": "string",
                        "enum": sorted(ELEXON_DATASETS),
                        "default": "FUELHH",
                    },
                    "start": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "end": {"type": "string", "description": "UTC offset-aware ISO timestamp."},
                    "fuel_type": {"type": "array", "items": {"type": "string"}},
                }
            ),
            capabilities=[
                "get_generation",
                "get_current_power",
                "grid",
                "generation",
                "demand",
                "forecast",
                "MW",
            ],
            actions={Action.READ, Action.EXTERNAL},
        ),
        _elexon_grid,
    )


__all__ = ["register"]
