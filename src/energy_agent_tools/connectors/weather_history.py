"""Open-Meteo historical temperature from gridded analysis/reanalysis."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    schema,
)
from ..registry import Registry
from .http import _iso, _parse_time, _request_json

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_WEATHER_DOCS = "https://open-meteo.com/en/docs/historical-weather-api"
MAX_HISTORY = timedelta(days=366)
_DATA_QUALITY_WARNING = "Historical temperature is gridded analysis/reanalysis data, not a physical thermometer measurement."


def _coordinates(args: Json) -> tuple[float, float]:
    latitude = args.get("latitude")
    longitude = args.get("longitude")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or not -90 <= latitude <= 90
        or not math.isfinite(float(latitude))
    ):
        raise EnergyError("invalid_location", "latitude must be a finite value between -90 and 90.")
    if (
        isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not -180 <= longitude <= 180
        or not math.isfinite(float(longitude))
    ):
        raise EnergyError(
            "invalid_location", "longitude must be a finite value between -180 and 180."
        )
    return float(latitude), float(longitude)


def _time_window(args: Json) -> tuple[datetime, datetime]:
    start = _parse_time(args.get("start"), "start")
    end = _parse_time(args.get("end"), "end")
    if end <= start:
        raise EnergyError("invalid_time_range", "end must be after start.")
    if end - start > MAX_HISTORY:
        raise EnergyError(
            "range_too_large", "The requested historical range cannot exceed 366 days."
        )
    return start, end


def _hourly_payload(payload: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
        raise EnergyError("malformed_response", "Open-Meteo returned no hourly historical data.")
    hourly = payload["hourly"]
    times = hourly.get("time")
    temperatures = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temperatures, list):
        raise EnergyError("malformed_response", "Open-Meteo returned invalid hourly columns.")
    if len(temperatures) != len(times):
        raise EnergyError("malformed_response", "Open-Meteo returned mismatched hourly columns.")

    units = payload.get("hourly_units")
    if not isinstance(units, dict):
        raise EnergyError("malformed_response", "Open-Meteo returned invalid hourly units.")
    temperature_unit = units.get("temperature_2m")
    if not isinstance(temperature_unit, str):
        raise EnergyError("malformed_response", "Open-Meteo did not declare temperature units.")
    if temperature_unit.strip().casefold() not in {
        "°c",
        "c",
        "degc",
        "degrees celsius",
        "celsius",
    }:
        raise EnergyError(
            "unit_mismatch", "Open-Meteo historical temperature units must be Celsius."
        )
    return times, temperatures


def _timestamp(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnergyError(
            "malformed_response", "Open-Meteo returned a non-numeric hourly timestamp."
        )
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise EnergyError(
            "malformed_response", "Open-Meteo returned an invalid hourly timestamp."
        ) from exc
    if not math.isfinite(seconds):
        raise EnergyError("malformed_response", "Open-Meteo returned an invalid hourly timestamp.")
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise EnergyError(
            "malformed_response", "Open-Meteo returned an out-of-range hourly timestamp."
        ) from exc


def _temperature(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnergyError("malformed_response", "Open-Meteo returned a non-numeric temperature.")
    try:
        temperature = float(value)
    except OverflowError as exc:
        raise EnergyError(
            "malformed_response", "Open-Meteo returned an invalid temperature."
        ) from exc
    if not math.isfinite(temperature):
        raise EnergyError("malformed_response", "Open-Meteo returned an invalid temperature.")
    return temperature


async def _historical_temperature(args: Json, ctx: ExecutionContext) -> EnergyResult:
    latitude, longitude = _coordinates(args)
    start, end = _time_window(args)
    query = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m",
        "timezone": "UTC",
        "temperature_unit": "celsius",
        "timeformat": "unixtime",
        "start_date": start.date().isoformat(),
        "end_date": (end - timedelta(microseconds=1)).date().isoformat(),
    }
    payload = await _request_json(ctx, ARCHIVE_URL, params=query)
    times, values = _hourly_payload(payload)

    rows: list[dict[str, Any]] = []
    included_instants: list[datetime] = []
    previous_timestamp: datetime | None = None
    has_null = False
    for raw_timestamp, raw_temperature in zip(times, values, strict=True):
        timestamp = _timestamp(raw_timestamp)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise EnergyError(
                "malformed_response",
                "Open-Meteo returned duplicate or non-increasing hourly timestamps.",
            )
        previous_timestamp = timestamp
        temperature = _temperature(raw_temperature)
        if timestamp < start or timestamp >= end:
            continue
        has_null |= temperature is None
        included_instants.append(timestamp)
        rows.append({"timestamp": _iso(timestamp), "temperature": temperature})

    warnings = [_DATA_QUALITY_WARNING]
    if has_null:
        warnings.append(
            "Open-Meteo returned null values for part of the requested temperature history."
        )

    return EnergyResult(
        data=rows,
        kind=DataKind.ESTIMATED,
        unit="degC",
        source="open-meteo",
        timezone="UTC",
        resolution="1h",
        provider="open-meteo",
        site_id=ctx.site_id,
        asset_id=ctx.asset_id,
        time_start=included_instants[0] if included_instants else None,
        time_end=included_instants[-1] if included_instants else None,
        quantity_shape="instantaneous",
        original_unit=payload["hourly_units"]["temperature_2m"],
        field_units={"temperature": "degC"},
        warnings=warnings,
        quality="gridded historical analysis/reanalysis",
        provenance=[
            {
                "provider": "open-meteo",
                "endpoint": "/v1/archive",
                "documentation": HISTORICAL_WEATHER_DOCS,
                "model_selection": "Best Match (provider default)",
                "requested_latitude": latitude,
                "requested_longitude": longitude,
                "requested_start": _iso(start),
                "requested_end": _iso(end),
            }
        ],
    )


def register(registry: Registry) -> None:
    """Add historical temperature retrieval to an already registered Open-Meteo toolkit."""

    registry.add(
        Tool(
            name="open_meteo.get_historical_temperature",
            toolkit="open-meteo",
            resource_scope="public",
            description=(
                "Get bounded hourly historical 2 m air temperature from Open-Meteo gridded "
                "analysis/reanalysis data."
            ),
            input_schema=schema(
                {
                    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                },
                required=["latitude", "longitude", "start", "end"],
            ),
            capabilities=["get_historical_weather"],
            actions={Action.READ},
            result_kind=DataKind.ESTIMATED,
            result_unit="degC",
        ),
        _historical_temperature,
    )
