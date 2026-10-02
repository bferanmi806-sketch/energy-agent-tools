"""Qualified deterministic environments for telemetry development cases.

The builders in this module are deliberately independent from the broader
scenario readiness table.  They create a production :class:`EnergyAgent`,
use the shipped HTTP or CSV connectors, and keep provider-shaped fixtures
local to the environment.  A scenario enters ``QUALIFIED_TELEMETRY_CASE_IDS``
only when its raw values and semantic failure boundary can be checked without
trusting the implementation under test.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.connectors import http, local
from energy_agent_tools.models import Asset, AuthConfig, ConnectedAccount, DataKind, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent

from .environments import BuiltEnvironment, EnvironmentUnavailable, ScenarioContext
from .scenarios import ScenarioCase, scenario_cases

QUALIFIED_TELEMETRY_CASE_IDS: frozenset[str] = frozenset(
    {
        "dev_consumption_home_assistant",
        "dev_consumption_local_day_dublin",
        "dev_units_kw_kwh",
        "dev_counter_reset_quality",
        "dev_field_units_provenance",
    }
)

EXCLUDED_TELEMETRY_CASES: dict[str, str] = {
    "dev_consumption_counter_reset": (
        "The production CSV connector preserves declared kind and unit but has no reviewed "
        "quantity_shape field. A cumulative counter cannot receive a generic consumption "
        "binding until the connector carries an explicit counter declaration."
    )
}

_SCENARIO_DATE = date(2026, 9, 29)
_USER_ID = "telemetry-development-user"
_HA_TOKEN = "telemetry-home-assistant-fixture-token"
_EMON_KEY = "telemetry-emoncms-fixture-key"

SCENARIO_CLOCKS: dict[str, datetime] = {
    case_id: datetime(2026, 9, 30, 12, 0, tzinfo=UTC) for case_id in QUALIFIED_TELEMETRY_CASE_IDS
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _case_index() -> dict[str, ScenarioCase]:
    return {case.id: case for case in scenario_cases()}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    values = list(rows)
    if not values:
        raise ValueError("Telemetry fixture must contain at least one row")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def _local_day(timezone: str, day: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def _context(case: ScenarioCase, root: Path, state_dir: Path) -> ScenarioContext:
    local_start, local_end = _local_day(case.timezone, _SCENARIO_DATE)
    if case.id == "dev_counter_reset_quality":
        zone = ZoneInfo(case.timezone)
        local_start = datetime.combine(date(2026, 9, 28), time(23), tzinfo=zone)
        local_end = local_start + timedelta(hours=5)
    return ScenarioContext(
        case_id=case.id,
        user_id=_USER_ID,
        site_id=f"{case.id}-site",
        provider=case.provider,
        timezone=case.timezone,
        scenario_date=_SCENARIO_DATE,
        scenario_clock=SCENARIO_CLOCKS[case.id],
        window_start=local_start.astimezone(UTC),
        window_end=local_end.astimezone(UTC),
        root=root,
        state_dir=state_dir,
    )


def _metadata(context: ScenarioContext) -> dict[str, Any]:
    return {
        "case_id": context.case_id,
        "provider": context.provider,
        "site_id": context.site_id,
        "timezone": context.timezone,
        "scenario_date": context.scenario_date.isoformat(),
        "scenario_clock": _iso(context.scenario_clock),
        "window_start": _iso(context.window_start),
        "window_end": _iso(context.window_end),
    }


def _ha_rows(context: ScenarioContext) -> list[dict[str, Any]]:
    start_local, _ = _local_day(context.timezone, context.scenario_date)
    rows = []
    for index in range(25):
        observed = (start_local + timedelta(hours=index)).astimezone(UTC)
        rows.append(
            {
                "entity_id": "sensor.dublin_total_energy",
                "state": f"{100.0 + index:.3f}",
                "attributes": {
                    "unit_of_measurement": "kWh",
                    "state_class": "total_increasing",
                },
                "last_updated": _iso(observed),
                "last_changed": _iso(observed),
            }
        )
    return rows


def _emon_day_rows(context: ScenarioContext) -> list[list[float]]:
    start = context.window_start - timedelta(hours=1)
    rows: list[list[float]] = []
    for index in range(26):
        observed = start + timedelta(hours=index)
        value = float(observed.day + observed.hour)
        rows.append([observed.timestamp(), value])
    return rows


def _emon_reset_rows(context: ScenarioContext) -> list[list[float]]:
    values = [9998.0, 12.0, 13.0, 15.0, 16.0]
    return [
        [(context.window_start + timedelta(hours=index)).timestamp(), value]
        for index, value in enumerate(values)
    ]


def _mock_transport(case_id: str, context: ScenarioContext) -> httpx.MockTransport:
    ha_rows = _ha_rows(context)
    emon_rows = (
        _emon_reset_rows(context)
        if case_id == "dev_counter_reset_quality"
        else _emon_day_rows(context)
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if case_id == "dev_consumption_home_assistant":
            if request.headers.get("authorization") != f"Bearer {_HA_TOKEN}":
                return httpx.Response(
                    401, json={"message": "authentication required"}, request=request
                )
            if (
                request.url.path.startswith("/api/history/period/")
                and request.url.params.get("filter_entity_id") == "sensor.dublin_total_energy"
            ):
                return httpx.Response(200, json=[ha_rows], request=request)
            return httpx.Response(404, json={"message": "entity not found"}, request=request)

        if case_id in {"dev_consumption_local_day_dublin", "dev_counter_reset_quality"}:
            if request.url.host != "emon.local":
                return httpx.Response(404, json={"message": "unknown host"}, request=request)
            if request.url.path != "/emoncms/feed/data.json":
                return httpx.Response(404, json={"message": "unknown feed"}, request=request)
            if request.url.params.get("apikey") != _EMON_KEY:
                return httpx.Response(
                    401, json={"message": "authentication required"}, request=request
                )
            start = request.url.params.get("start")
            end = request.url.params.get("end")
            if start is None or end is None:
                return httpx.Response(400, json={"message": "range required"}, request=request)
            start_ms = float(start)
            end_ms = float(end)
            filtered = [row for row in emon_rows if start_ms <= row[0] * 1000 < end_ms]
            return httpx.Response(200, json=filtered, request=request)

        return httpx.Response(
            404, json={"message": "fixture route not configured"}, request=request
        )

    return httpx.MockTransport(handler)


def _account(
    identifier: str,
    context: ScenarioContext,
    toolkit: str,
    settings: dict[str, Any],
    scheme: Literal["bearer", "api-key"],
) -> ConnectedAccount:
    return ConnectedAccount(
        id=identifier,
        user_id=context.user_id,
        toolkit=toolkit,
        site_id=context.site_id,
        auth=AuthConfig(scheme=scheme, secret_id=identifier),
        settings=settings,
    )


def _accounts_and_vault(
    case_id: str,
    context: ScenarioContext,
    client: httpx.AsyncClient,
    state_dir: Path,
) -> tuple[list[ConnectedAccount], AuthStore | None]:
    if case_id == "dev_consumption_home_assistant":
        raw = [
            (
                _account(
                    "ha-dublin",
                    context,
                    "home-assistant",
                    {"base_url": "https://ha.local"},
                    "bearer",
                ),
                _HA_TOKEN,
            )
        ]
    elif case_id == "dev_consumption_local_day_dublin":
        raw = [
            (
                _account(
                    "emon-dublin",
                    context,
                    "openenergymonitor",
                    {
                        "base_url": "http://emon.local/emoncms",
                        "feed_id": 7,
                        "unit": "kWh",
                        "quantity_shape": "interval",
                    },
                    "api-key",
                ),
                _EMON_KEY,
            )
        ]
    elif case_id == "dev_counter_reset_quality":
        raw = [
            (
                _account(
                    "emon-cape-town",
                    context,
                    "openenergymonitor",
                    {
                        "base_url": "http://emon.local/emoncms",
                        "feed_id": 11,
                        "unit": "kWh",
                        "quantity_shape": "counter",
                    },
                    "api-key",
                ),
                _EMON_KEY,
            )
        ]
    else:
        return [], None
    vault = AuthStore(state_dir / "vault", Fernet.generate_key(), http=client)
    for account, credential in raw:
        vault.configure(account, credential)
    return [
        vault.get_account(context.user_id, account.id, site_id=context.site_id)
        for account, _ in raw
    ], vault


def _sites_and_assets(
    case_id: str, context: ScenarioContext, accounts: list[ConnectedAccount]
) -> tuple[list[Site], list[Asset]]:
    site = Site(
        id=context.site_id,
        user_id=context.user_id,
        name=f"{case_id} qualified site",
        timezone=context.timezone,
        latitude=53.35 if "dublin" in case_id else -33.9 if "cape" in case_id else 53.5,
        longitude=-6.26 if "dublin" in case_id else 18.4 if "cape" in case_id else -2.2,
    )
    account_ids = [account.id for account in accounts]
    if case_id == "dev_consumption_home_assistant":
        asset = Asset(
            id="dublin-home-meter",
            site_id=site.id,
            kind="meter",
            name="Dublin home cumulative energy sensor",
            account_ids=account_ids,
            metadata={
                "entity_id": "sensor.dublin_total_energy",
                "unit": "kWh",
                "quantity_shape": "counter",
            },
        )
    elif case_id == "dev_consumption_local_day_dublin":
        asset = Asset(
            id="dublin-home-meter",
            site_id=site.id,
            kind="meter",
            name="Dublin home interval feed",
            account_ids=account_ids,
            metadata={"feed_id": 7, "unit": "kWh", "quantity_shape": "interval"},
        )
    elif case_id == "dev_counter_reset_quality":
        asset = Asset(
            id="cape-town-counter",
            site_id=site.id,
            kind="meter",
            name="Cape Town cumulative energy counter",
            account_ids=account_ids,
            metadata={"feed_id": 11, "unit": "kWh", "quantity_shape": "counter"},
        )
    elif case_id == "dev_units_kw_kwh":
        asset = Asset(
            id="manchester-power-reading",
            site_id=site.id,
            kind="telemetry",
            name="Manchester office instantaneous power fixture",
            metadata={"unit": "kW", "quantity_shape": "instantaneous", "duration": "30min"},
        )
    else:
        asset = Asset(
            id="manchester-mixed-telemetry",
            site_id=site.id,
            kind="telemetry",
            name="Manchester office mixed field fixture",
            metadata={
                "fields": {
                    "power_kw": {"unit": "kW", "quantity_shape": "instantaneous"},
                    "energy_kwh": {"unit": "kWh", "quantity_shape": "interval"},
                }
            },
        )
    return [site], [asset]


def _bindings(
    case_id: str, context: ScenarioContext, accounts: list[ConnectedAccount]
) -> list[CapabilityBinding]:
    if case_id == "dev_consumption_home_assistant":
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="home_assistant.get_history",
                account_id=accounts[0].id,
                asset_id="dublin-home-meter",
                kind=DataKind.METERED,
                unit="kWh",
                quantity_shape="counter",
                resolution="state update",
                quality="provider-reported",
                preference=100,
                defaults={"entity_id": "sensor.dublin_total_energy"},
                reviewed=True,
            )
        ]
    if case_id == "dev_consumption_local_day_dublin":
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="openenergymonitor.get_feed",
                account_id=accounts[0].id,
                asset_id="dublin-home-meter",
                kind=DataKind.METERED,
                unit="kWh",
                quantity_shape="interval",
                resolution="3600s",
                quality="provider-reported",
                preference=100,
                defaults={"feed_id": 7, "unit": "kWh", "interval": 3600},
                reviewed=True,
            )
        ]
    if case_id == "dev_counter_reset_quality":
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="openenergymonitor.get_feed",
                account_id=accounts[0].id,
                asset_id="cape-town-counter",
                kind=DataKind.METERED,
                unit="kWh",
                quantity_shape="counter",
                resolution="3600s",
                quality="provider-reported",
                preference=100,
                defaults={"feed_id": 11, "unit": "kWh", "interval": 3600},
                reviewed=True,
            )
        ]
    if case_id == "dev_units_kw_kwh":
        return [
            CapabilityBinding(
                capability="get_current_power",
                tool="CSV_READ_TIMESERIES",
                asset_id="manchester-power-reading",
                kind=DataKind.METERED,
                unit="kW",
                quality="fixture-declared",
                preference=100,
                defaults={
                    "file": "power.csv",
                    "kind": "metered",
                    "unit": "kW",
                    "timezone": context.timezone,
                    "timestamp": "timestamp",
                },
                reviewed=True,
            )
        ]
    return [
        CapabilityBinding(
            capability="get_energy_consumption",
            tool="CSV_READ_TIMESERIES",
            asset_id="manchester-mixed-telemetry",
            kind=DataKind.METERED,
            unit="mixed",
            quality="fixture-declared",
            preference=100,
            defaults={
                "file": "fields.csv",
                "kind": "metered",
                "unit": "mixed",
                "timezone": context.timezone,
                "timestamp": "timestamp",
            },
            reviewed=True,
        )
    ]


def _write_case_fixture(case_id: str, context: ScenarioContext) -> None:
    rows: Any
    if case_id == "dev_consumption_home_assistant":
        rows = _ha_rows(context)
        _write_json(context.root / "provider-truth.json", {"rows": rows, "total_delta_kwh": 24.0})
    elif case_id == "dev_consumption_local_day_dublin":
        rows = _emon_day_rows(context)
        _write_json(
            context.root / "provider-truth.json",
            {"rows": rows, "unit": "kWh", "quantity_shape": "interval"},
        )
    elif case_id == "dev_counter_reset_quality":
        rows = _emon_reset_rows(context)
        _write_json(
            context.root / "provider-truth.json",
            {"rows": rows, "unit": "kWh", "quantity_shape": "counter", "valid_delta_kwh": 4.0},
        )
    elif case_id == "dev_units_kw_kwh":
        rows = [
            {
                "timestamp": "2026-09-29T10:00:00+01:00",
                "end": "2026-09-29T10:30:00+01:00",
                "power_kw": "5.0",
                "quantity_shape": "instantaneous",
            }
        ]
        _write_csv(context.root / "power.csv", rows)
        _write_json(context.root / "provider-truth.json", {"rows": rows, "expected_kwh": 2.5})
    elif case_id == "dev_field_units_provenance":
        rows = [
            {
                "timestamp": "2026-09-29T10:00:00+01:00",
                "variable": "power_kw",
                "value": "5.0",
                "unit": "kW",
            },
            {
                "timestamp": "2026-09-29T10:00:00+01:00",
                "variable": "energy_kwh",
                "value": "2.5",
                "unit": "kWh",
            },
        ]
        _write_csv(context.root / "fields.csv", rows)
        _write_json(
            context.root / "provider-truth.json",
            {"rows": rows, "field_units": {"power_kw": "kW", "energy_kwh": "kWh"}},
        )


def build_telemetry_environment(case_id: str, root: Path, state_dir: Path) -> BuiltEnvironment:
    """Build one independently qualified telemetry development case."""

    case = _case_index().get(case_id)
    if case is None or case_id not in QUALIFIED_TELEMETRY_CASE_IDS:
        reason = EXCLUDED_TELEMETRY_CASES.get(case_id)
        suffix = f" {reason}" if reason else ""
        raise EnvironmentUnavailable(
            f"Scenario {case_id!r} has no qualified telemetry environment.{suffix}"
        )
    if case.split != "development" or case.fixture_case_id is not None:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} is not a development case.")

    case_root = (Path(root).resolve() / case_id).resolve()
    case_state = (Path(state_dir).resolve() / case_id).resolve()
    case_root.mkdir(parents=True, exist_ok=True)
    case_state.mkdir(parents=True, exist_ok=True)
    context = _context(case, case_root, case_state)
    _write_case_fixture(case_id, context)
    _write_json(context.root / "environment.json", _metadata(context))

    client = httpx.AsyncClient(
        transport=_mock_transport(case_id, context),
        follow_redirects=False,
        timeout=5,
    )
    accounts, auth_store = _accounts_and_vault(case_id, context, client, case_state)
    context = replace(context, account_ids=tuple(account.id for account in accounts))
    registry = Registry()
    http.register(registry)
    local.register(registry)
    if case_id in {"dev_units_kw_kwh", "dev_field_units_provenance"}:
        local.register_csv(registry, context.root)
    sites, assets = _sites_and_assets(case_id, context, accounts)
    agent = EnergyAgent(
        registry,
        context.state_dir,
        accounts=accounts,
        sites=sites,
        assets=assets,
        http=client,
        auth_store=auth_store,
        bindings=_bindings(case_id, context, accounts),
        calendar_clock=lambda: context.scenario_clock,
    )
    return BuiltEnvironment(agent=agent, context=context, _http=client)


__all__ = [
    "BuiltEnvironment",
    "EnvironmentUnavailable",
    "EXCLUDED_TELEMETRY_CASES",
    "QUALIFIED_TELEMETRY_CASE_IDS",
    "SCENARIO_CLOCKS",
    "ScenarioContext",
    "build_telemetry_environment",
]
