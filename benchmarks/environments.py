"""Qualified deterministic environments for a small development benchmark slice.

The scenario corpus remains a review and scoring contract.  This module adds
only the development cases whose data and provider boundaries have been
implemented and independently checked.  It deliberately does not change a
scenario's readiness field; the parent harness can promote a case after
reviewing these builders and tests.

Each builder uses the production ``EnergyAgent`` runtime and connectors.  HTTP
calls go through a local ``httpx.MockTransport`` with provider-shaped payloads,
so a run cannot contact a real provider or require a real credential.  The
returned ``BuiltEnvironment`` owns the injected HTTP client and exposes an
async ``close`` method for deterministic cleanup::

    built = build_environment("dev_consumption_daily_csv", root, state_dir)
    try:
        session = built.agent.session(built.context.user_id, built.context.site_id)
        result = await built.agent.execute(session, "CSV_READ_TIMESERIES", ...)
    finally:
        await built.close()
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.connectors import http, local
from energy_agent_tools.models import Asset, AuthConfig, ConnectedAccount, DataKind, Site
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent

from .scenarios import ScenarioCase, scenario_cases


class EnvironmentUnavailable(RuntimeError):
    """Raised for an unknown, held-out, or not-yet-qualified scenario."""


# Readiness stays in scenarios.py under the parent harness's control.  This is
# the independent qualification set that this module can actually build.
QUALIFIED_ENVIRONMENT_CASE_IDS: frozenset[str] = frozenset(
    {
        "dev_consumption_daily_csv",
        "dev_current_power_snapshot",
        "dev_consumption_interval_gap",
        "dev_two_account_selection",
    }
)

_SCENARIO_DATE = date(2026, 9, 29)
_USER_ID = "roadmap-user"


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """Fixed scope, clock, and local-day window for one environment."""

    case_id: str
    user_id: str
    site_id: str
    provider: str
    timezone: str
    scenario_date: date
    scenario_clock: datetime
    window_start: datetime
    window_end: datetime
    root: Path
    state_dir: Path
    account_ids: tuple[str, ...] = ()

    @property
    def start(self) -> datetime:
        """Compatibility alias used by harness adapters."""

        return self.window_start

    @property
    def end(self) -> datetime:
        """Compatibility alias used by harness adapters."""

        return self.window_end

    def arguments(self) -> dict[str, str]:
        return {
            "start": _iso(self.window_start),
            "end": _iso(self.window_end),
        }


@dataclass(slots=True)
class BuiltEnvironment:
    """A production runtime plus the deterministic context that owns it."""

    agent: EnergyAgent
    context: ScenarioContext
    _http: httpx.AsyncClient
    _closed: bool = False

    @property
    def scenario_date(self) -> date:
        return self.context.scenario_date

    def __iter__(self):
        """Permit ``agent, context = build_environment(...)`` integration."""

        yield self.agent
        yield self.context

    async def close(self) -> None:
        """Close the agent's vault and the injected client exactly once."""

        if self._closed:
            return
        self._closed = True
        await self.agent.close()
        await self._http.aclose()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _case_index() -> dict[str, ScenarioCase]:
    return {case.id: case for case in scenario_cases()}


def _context(case: ScenarioCase, root: Path, state_dir: Path) -> ScenarioContext:
    zone = ZoneInfo(case.timezone)
    local_start = datetime.combine(_SCENARIO_DATE, datetime.min.time(), tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    if case.id == "dev_current_power_snapshot":
        clock = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    else:
        clock = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    return ScenarioContext(
        case_id=case.id,
        user_id=_USER_ID,
        site_id=f"{case.id}-site",
        provider=case.provider,
        timezone=case.timezone,
        scenario_date=_SCENARIO_DATE,
        scenario_clock=clock,
        window_start=local_start.astimezone(UTC),
        window_end=local_end.astimezone(UTC),
        root=root,
        state_dir=state_dir,
    )


def _local_slots(context: ScenarioContext, count: int = 48) -> list[datetime]:
    zone = ZoneInfo(context.timezone)
    local_start = datetime.combine(context.scenario_date, datetime.min.time(), tzinfo=zone)
    return [local_start + timedelta(minutes=30 * index) for index in range(count)]


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    values = list(rows)
    if not values:
        raise ValueError(f"Cannot write an empty fixture: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def _daily_values() -> list[float]:
    # 48 explicit half-hour readings.  The independently checked total is
    # 42.5 kWh; the final adjustment represents a deterministic daytime peak.
    values = [0.4 + 0.1 * (index % 8) for index in range(48)]
    for index in range(24, 32):
        values[index] += 0.8125
    if sum(values) != 42.5:
        raise AssertionError("daily fixture total changed")
    return values


def _write_daily_csv(context: ScenarioContext) -> None:
    rows = []
    for timestamp, energy in zip(_local_slots(context), _daily_values(), strict=True):
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "energy_kwh": f"{energy:.4f}",
                "average_power_kw": f"{energy * 2:.4f}",
                "quality": "measured",
            }
        )
    _write_csv(context.root / "meter.csv", rows)


def _write_gap_csv_placeholder(context: ScenarioContext) -> None:
    # The actual interval-gap case is served by the Octopus adapter below.  A
    # small operator-readable manifest makes the intentional gap inspectable
    # without pretending that local CSV data is the provider response.
    (context.root / "fixture.json").write_text(
        '{"missing_intervals": 6, "frequency": "30min", "source": "octopus"}\n',
        encoding="utf-8",
    )


def _write_metadata(case: ScenarioCase, context: ScenarioContext) -> None:
    (context.root / "environment.json").write_text(
        "{\n"
        f'  "case_id": "{case.id}",\n'
        f'  "provider": "{case.provider}",\n'
        f'  "site_id": "{context.site_id}",\n'
        f'  "timezone": "{context.timezone}",\n'
        f'  "scenario_date": "{context.scenario_date.isoformat()}",\n'
        f'  "window_start": "{_iso(context.window_start)}",\n'
        f'  "window_end": "{_iso(context.window_end)}"\n'
        "}\n",
        encoding="utf-8",
    )


def _gap_rows(context: ScenarioContext) -> list[dict[str, Any]]:
    slots = _local_slots(context)
    missing = set(range(20, 26))
    rows: list[dict[str, Any]] = []
    for index, start in enumerate(slots):
        if index in missing:
            continue
        end = start + timedelta(minutes=30)
        rows.append(
            {
                "interval_start": _iso(start),
                "interval_end": _iso(end),
                "consumption": round(0.25 + index * 0.01, 4),
            }
        )
    return rows


def _mock_transport(case_id: str, context: ScenarioContext) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if case_id == "dev_current_power_snapshot":
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return httpx.Response(
                    401, json={"message": "authentication required"}, request=request
                )
            if path == "/api/states/sensor.school_power":
                sampled_at = context.scenario_clock - timedelta(seconds=45)
                return httpx.Response(
                    200,
                    json={
                        "entity_id": "sensor.school_power",
                        "state": "12.75",
                        "attributes": {
                            "unit_of_measurement": "kW",
                            "state_class": "measurement",
                        },
                        "last_updated": _iso(sampled_at),
                    },
                    request=request,
                )
        if case_id in {"dev_consumption_interval_gap", "dev_two_account_selection"}:
            if request.url.host == "api.octopus.energy" and path.endswith("/consumption/"):
                if not request.headers.get("authorization", "").startswith("Basic "):
                    return httpx.Response(
                        401, json={"message": "authentication required"}, request=request
                    )
                if case_id == "dev_two_account_selection":
                    amount = 1.0 if "MPAN-HOME" in path else 2.0
                    start = _local_slots(context)[0]
                    rows = [
                        {
                            "interval_start": _iso(start),
                            "interval_end": _iso(start + timedelta(minutes=30)),
                            "consumption": amount,
                        }
                    ]
                else:
                    rows = _gap_rows(context)
                return httpx.Response(
                    200,
                    json={"results": rows, "next": None},
                    request=request,
                )
        return httpx.Response(
            404, json={"message": "fixture route not configured"}, request=request
        )

    return httpx.MockTransport(handler)


def _account(
    identifier: str,
    context: ScenarioContext,
    toolkit: str,
    settings: dict[str, str],
) -> ConnectedAccount:
    return ConnectedAccount(
        id=identifier,
        user_id=context.user_id,
        toolkit=toolkit,
        site_id=context.site_id,
        auth=AuthConfig(scheme="bearer"),
        settings=settings,
    )


def _stored_accounts(
    case_id: str,
    context: ScenarioContext,
    client: httpx.AsyncClient,
    state_dir: Path,
) -> tuple[list[ConnectedAccount], AuthStore | None]:
    if case_id == "dev_current_power_snapshot":
        raw_accounts = [
            _account(
                "ha-school",
                context,
                "home-assistant",
                {"base_url": "https://fixture.local"},
            )
        ]
        credentials = {"ha-school": "fixture-ha-school-token"}
    elif case_id == "dev_consumption_interval_gap":
        raw_accounts = [
            _account(
                "octopus-gap",
                context,
                "octopus-energy-account",
                {"mpan": "MPAN-BRISTOL", "serial_number": "SERIAL-BRISTOL"},
            )
        ]
        credentials = {"octopus-gap": "fixture-octopus-gap-token"}
    elif case_id == "dev_two_account_selection":
        raw_accounts = [
            _account(
                "octopus-home",
                context,
                "octopus-energy-account",
                {"mpan": "MPAN-HOME", "serial_number": "SERIAL-HOME"},
            ),
            _account(
                "octopus-annex",
                context,
                "octopus-energy-account",
                {"mpan": "MPAN-ANNEX", "serial_number": "SERIAL-ANNEX"},
            ),
        ]
        credentials = {
            "octopus-home": "fixture-octopus-home-token",
            "octopus-annex": "fixture-octopus-annex-token",
        }
    else:
        return [], None

    vault = AuthStore(state_dir / "vault", Fernet.generate_key(), http=client)
    for account in raw_accounts:
        vault.configure(account, credentials[account.id])
    return [
        vault.get_account(context.user_id, account.id, site_id=context.site_id)
        for account in raw_accounts
    ], vault


def _sites_and_assets(
    case_id: str,
    context: ScenarioContext,
    accounts: list[ConnectedAccount],
) -> tuple[list[Site], list[Asset]]:
    site = Site(
        id=context.site_id,
        user_id=context.user_id,
        name=f"{case_id} qualified site",
        timezone=context.timezone,
        latitude=40.7 if case_id == "dev_current_power_snapshot" else 51.5,
        longitude=-74.0 if case_id == "dev_current_power_snapshot" else -2.6,
    )
    account_ids = [account.id for account in accounts]
    if case_id == "dev_consumption_daily_csv":
        assets = [
            Asset(
                id="manchester-office-meter",
                site_id=site.id,
                kind="meter",
                name="Manchester office meter",
                metadata={"fixture_file": "meter.csv", "energy_field": "energy_kwh"},
            )
        ]
    elif case_id == "dev_current_power_snapshot":
        assets = [
            Asset(
                id="new-york-school-meter",
                site_id=site.id,
                kind="meter",
                name="New York school main meter",
                account_ids=account_ids,
                metadata={"entity_id": "sensor.school_power", "unit": "kW"},
            )
        ]
    elif case_id == "dev_consumption_interval_gap":
        assets = [
            Asset(
                id="bristol-workshop-meter",
                site_id=site.id,
                kind="meter",
                name="Bristol workshop meter",
                account_ids=account_ids,
                metadata={"frequency": "30min", "missing_intervals": 6},
            )
        ]
    else:
        assets = [
            Asset(
                id="dublin-home-meter",
                site_id=site.id,
                kind="meter",
                name="Dublin home meter",
                account_ids=["octopus-home"],
                metadata={"mpan": "MPAN-HOME", "serial_number": "SERIAL-HOME"},
            ),
            Asset(
                id="dublin-annex-meter",
                site_id=site.id,
                kind="meter",
                name="Dublin annex meter",
                account_ids=["octopus-annex"],
                metadata={"mpan": "MPAN-ANNEX", "serial_number": "SERIAL-ANNEX"},
            ),
        ]
    return [site], assets


def _bindings(
    case_id: str,
    context: ScenarioContext,
    accounts: list[ConnectedAccount],
) -> list[CapabilityBinding]:
    if case_id == "dev_consumption_daily_csv":
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="CSV_READ_TIMESERIES",
                asset_id="manchester-office-meter",
                kind=DataKind.METERED,
                unit="kWh",
                resolution="30min",
                quality="verified fixture",
                preference=100,
                defaults={
                    "file": "meter.csv",
                    "kind": "metered",
                    "unit": "kWh",
                    "timezone": context.timezone,
                    "timestamp": "timestamp",
                },
                reviewed=True,
            )
        ]
    if case_id == "dev_current_power_snapshot":
        return [
            CapabilityBinding(
                capability="get_current_power",
                tool="home_assistant.get_state",
                account_id=accounts[0].id,
                asset_id="new-york-school-meter",
                kind=DataKind.METERED,
                unit="kW",
                quality="provider-reported",
                preference=100,
                defaults={"entity_id": "sensor.school_power"},
                reviewed=True,
            )
        ]
    if case_id == "dev_consumption_interval_gap":
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="octopus_energy.get_consumption",
                account_id=accounts[0].id,
                asset_id="bristol-workshop-meter",
                kind=DataKind.METERED,
                unit="kWh",
                resolution="30min",
                quality="provider-reported",
                preference=100,
                reviewed=True,
            )
        ]
    return [
        CapabilityBinding(
            capability="get_energy_consumption",
            tool="octopus_energy.get_consumption",
            account_id=account.id,
            asset_id=asset_id,
            kind=DataKind.METERED,
            unit="kWh",
            resolution="provider interval",
            quality="provider-reported",
            preference=100,
            reviewed=True,
        )
        for account, asset_id in zip(
            accounts,
            ("dublin-home-meter", "dublin-annex-meter"),
            strict=True,
        )
    ]


def qualified_environment_clocks() -> dict[str, datetime]:
    """Return only independently checked builders and their fixed clocks."""

    from .telemetry_environments import SCENARIO_CLOCKS as telemetry_clocks

    clocks = {
        case_id: datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        for case_id in QUALIFIED_ENVIRONMENT_CASE_IDS
    }
    clocks["dev_current_power_snapshot"] = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    if clocks.keys() & telemetry_clocks.keys():
        raise RuntimeError("Qualified environment families must own distinct case IDs.")
    return clocks | telemetry_clocks


def build_environment(case_id: str, root: Path, state_dir: Path) -> BuiltEnvironment:
    """Dispatch one independently qualified development environment."""

    from .telemetry_environments import QUALIFIED_TELEMETRY_CASE_IDS, build_telemetry_environment

    if case_id in QUALIFIED_TELEMETRY_CASE_IDS:
        return build_telemetry_environment(case_id, root, state_dir)

    case = _case_index().get(case_id)
    if case is None or case_id not in QUALIFIED_ENVIRONMENT_CASE_IDS:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} has no qualified environment.")
    if case.split != "development" or case.fixture_case_id is not None:
        raise EnvironmentUnavailable(f"Scenario {case_id!r} is not a development environment.")

    case_root = (Path(root).resolve() / case_id).resolve()
    case_state = (Path(state_dir).resolve() / case_id).resolve()
    case_root.mkdir(parents=True, exist_ok=True)
    case_state.mkdir(parents=True, exist_ok=True)
    context = _context(case, case_root, case_state)
    if case_id == "dev_consumption_daily_csv":
        _write_daily_csv(context)
    elif case_id == "dev_consumption_interval_gap":
        _write_gap_csv_placeholder(context)
    _write_metadata(case, context)

    client = httpx.AsyncClient(
        transport=_mock_transport(case_id, context),
        follow_redirects=False,
        timeout=5,
    )
    accounts, auth_store = _stored_accounts(case_id, context, client, case_state)
    context = replace(context, account_ids=tuple(account.id for account in accounts))
    registry = Registry()
    http.register(registry)
    local.register(registry)
    if case_id in {"dev_consumption_daily_csv"}:
        local.register_csv(registry, case_root)
    sites, assets = _sites_and_assets(case_id, context, accounts)
    agent = EnergyAgent(
        registry,
        case_state,
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
    "QUALIFIED_ENVIRONMENT_CASE_IDS",
    "ScenarioContext",
    "build_environment",
]
