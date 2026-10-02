"""Independent provider and site fixtures for substitution contract tests.

The fixture keeps provider inputs separate so a generic workflow can be run
against Octopus, OpenEnergyMonitor, and an operator-approved CSV source.  The
HTTP scenarios are served through :class:`httpx.MockTransport`; no real
credentials or network calls are involved.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.connectors import analytics, local
from energy_agent_tools.connectors import http as http_connectors
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyResult,
    ExecutionContext,
    Site,
    Tool,
    Toolkit,
    schema,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent

WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(hours=2)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _timestamp_rows(values: tuple[float, ...]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": _iso(WINDOW_START + timedelta(minutes=30 * index)),
            "value": value,
            "end": _iso(WINDOW_START + timedelta(minutes=30 * (index + 1))),
        }
        for index, value in enumerate(values)
    ]


@dataclass(frozen=True)
class ProviderScenario:
    """One site-level consumption and tariff contract with independent truth."""

    provider: str
    user_id: str
    site_id: str
    account_id: str
    consumption_source: str
    tariff_source: str
    consumption_values: tuple[float, ...]
    tariff_values: tuple[float, ...]
    expected_consumption_kwh: float
    expected_cost_gbp: float


SCENARIOS: dict[str, ProviderScenario] = {
    "octopus": ProviderScenario(
        provider="octopus",
        user_id="alice",
        site_id="alice-octopus-site",
        account_id="octopus-alice-main",
        consumption_source="octopus-energy",
        tariff_source="octopus-energy",
        consumption_values=(1.0, 2.0, 0.5, 1.5),
        tariff_values=(10.0, 20.0, 30.0, 40.0),
        expected_consumption_kwh=5.0,
        expected_cost_gbp=1.25,
    ),
    "emon": ProviderScenario(
        provider="emon",
        user_id="alice",
        site_id="alice-emon-site",
        account_id="emon-alice",
        consumption_source="openenergymonitor",
        tariff_source="fixture-tariff",
        consumption_values=(0.75, 1.25, 2.25, 0.5),
        tariff_values=(7.5, 15.0, 22.5, 30.0),
        expected_consumption_kwh=4.75,
        expected_cost_gbp=0.9,
    ),
    "csv": ProviderScenario(
        provider="csv",
        user_id="alice",
        site_id="alice-csv-site",
        account_id="csv-alice",
        consumption_source="local-csv",
        tariff_source="fixture-tariff",
        consumption_values=(1.2, 0.8, 1.6, 2.4),
        tariff_values=(12.0, 18.0, 24.0, 30.0),
        expected_consumption_kwh=6.0,
        expected_cost_gbp=1.392,
    ),
}


class ProviderHTTPFixture:
    """MockTransport handler for provider-shaped HTTP responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.octopus.energy":
            return self._octopus(request)
        if request.url.host == "emon.example":
            return self._emoncms(request)
        return httpx.Response(404, json={"error": "unknown fixture endpoint"}, request=request)

    def _octopus(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/consumption/"):
            assert request.headers.get("authorization", "").startswith("Basic ")
            if "/MPAN-MAIN/" in path:
                values = SCENARIOS["octopus"].consumption_values
            elif "/MPAN-ALT/" in path:
                values = (2.0, 2.0, 2.0, 2.0)
            else:
                return httpx.Response(404, request=request)
            rows = [
                {
                    "interval_start": _iso(WINDOW_START + timedelta(minutes=30 * index)),
                    "interval_end": _iso(WINDOW_START + timedelta(minutes=30 * (index + 1))),
                    "consumption": value,
                }
                for index, value in enumerate(values)
            ]
            return httpx.Response(200, json={"results": rows, "next": None}, request=request)

        if "/standard-unit-rates/" in path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "valid_from": _iso(WINDOW_START + timedelta(minutes=30 * index)),
                            "valid_to": _iso(WINDOW_START + timedelta(minutes=30 * (index + 1))),
                            "value_inc_vat": value,
                        }
                        for index, value in enumerate(SCENARIOS["octopus"].tariff_values)
                    ],
                    "next": None,
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    def _emoncms(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emoncms/feed/data.json"
        assert request.url.params["apikey"] == "emon-secret"
        assert request.url.params["id"] == "101"
        assert request.url.params["interval"] == "1800"
        return httpx.Response(
            200,
            json=[
                [
                    int((WINDOW_START + timedelta(minutes=30 * index)).timestamp()),
                    value,
                ]
                for index, value in enumerate(SCENARIOS["emon"].consumption_values)
            ],
            request=request,
        )


@dataclass
class ProviderFixture:
    """Builds one isolated multi-user, multi-site gateway and its HTTP client."""

    data_root: Path
    http: ProviderHTTPFixture

    def write_csv(self) -> None:
        path = self.data_root / "csv_consumption.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "value", "end"])
            writer.writeheader()
            writer.writerows(_timestamp_rows(SCENARIOS["csv"].consumption_values))

    def sites(self) -> list[Site]:
        return [
            Site(
                id="alice-octopus-site",
                user_id="alice",
                name="Alice Octopus site",
                timezone="UTC",
            ),
            Site(
                id="alice-alt-site",
                user_id="alice",
                name="Alice alternate site",
                timezone="UTC",
            ),
            Site(
                id="alice-emon-site",
                user_id="alice",
                name="Alice Emoncms site",
                timezone="UTC",
            ),
            Site(
                id="alice-csv-site",
                user_id="alice",
                name="Alice CSV site",
                timezone="UTC",
            ),
            Site(id="bob-site", user_id="bob", name="Bob site", timezone="UTC"),
        ]

    def accounts(self) -> list[ConnectedAccount]:
        return [
            ConnectedAccount(
                id="octopus-alice-main",
                user_id="alice",
                site_id="alice-octopus-site",
                toolkit="octopus-energy-account",
                auth=AuthConfig(scheme="basic", credential_env="FIXTURE_OCTOPUS_KEY"),
                settings={"mpan": "MPAN-MAIN", "serial_number": "SERIAL-MAIN"},
            ),
            ConnectedAccount(
                id="octopus-alice-alt",
                user_id="alice",
                site_id="alice-alt-site",
                toolkit="octopus-energy-account",
                auth=AuthConfig(scheme="basic", credential_env="FIXTURE_OCTOPUS_KEY"),
                settings={"mpan": "MPAN-ALT", "serial_number": "SERIAL-ALT"},
            ),
            ConnectedAccount(
                id="octopus-bob",
                user_id="bob",
                site_id="bob-site",
                toolkit="octopus-energy-account",
                auth=AuthConfig(scheme="basic", credential_env="FIXTURE_OCTOPUS_KEY"),
                settings={"mpan": "MPAN-ALT", "serial_number": "SERIAL-BOB"},
            ),
            ConnectedAccount(
                id="octopus-tariff-alice",
                user_id="alice",
                site_id="alice-octopus-site",
                toolkit="octopus-energy",
                settings={
                    "product_code": "FIXTURE-PRODUCT",
                    "tariff_code": "FIXTURE-TARIFF",
                    "capability_defaults": {
                        "get_tariff": {
                            "product_code": "FIXTURE-PRODUCT",
                            "tariff_code": "FIXTURE-TARIFF",
                        }
                    },
                },
            ),
            ConnectedAccount(
                id="emon-alice",
                user_id="alice",
                site_id="alice-emon-site",
                toolkit="openenergymonitor",
                auth=AuthConfig(scheme="api-key", credential_env="FIXTURE_EMON_KEY"),
                settings={
                    "base_url": "https://emon.example/emoncms/",
                    "feed_id": 101,
                    "unit": "kWh",
                    "quantity_shape": "interval",
                    "interval_position": "start",
                    "interval_seconds": 1800,
                },
            ),
            ConnectedAccount(
                id="tariff-emon-alice",
                user_id="alice",
                site_id="alice-emon-site",
                toolkit="fixture-tariff",
                auth=AuthConfig(scheme="local"),
            ),
            ConnectedAccount(
                id="csv-alice",
                user_id="alice",
                site_id="alice-csv-site",
                toolkit="csv",
                auth=AuthConfig(scheme="local"),
            ),
            ConnectedAccount(
                id="tariff-csv-alice",
                user_id="alice",
                site_id="alice-csv-site",
                toolkit="fixture-tariff",
                auth=AuthConfig(scheme="local"),
            ),
        ]

    def bindings(self) -> list[CapabilityBinding]:
        return [
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="openenergymonitor.get_feed",
                account_id="emon-alice",
                kind=DataKind.METERED,
                unit="kWh",
                resolution="1800s",
                quantity_shape="interval",
                fixed_arguments={"interval": 1800},
                reviewed=True,
                quality="metered",
            ),
            CapabilityBinding(
                capability="get_energy_consumption",
                tool="CSV_READ_TIMESERIES",
                account_id="csv-alice",
                quantity_shape="interval",
                kind=DataKind.METERED,
                unit="kWh",
                defaults={
                    "file": "csv_consumption.csv",
                    "quantity_shape": "interval",
                    "kind": DataKind.METERED.value,
                    "unit": "kWh",
                    "timezone": "UTC",
                    "timestamp": "timestamp",
                },
                reviewed=True,
                quality="user-declared",
            ),
            CapabilityBinding(
                capability="get_tariff",
                tool="fixture_tariff.get_rates",
                account_id="tariff-emon-alice",
                kind=DataKind.CALCULATED,
                unit="p/kWh",
                resolution="1800s",
                fixed_arguments={"scenario": "emon"},
                reviewed=True,
                quality="fixture",
                preference=10,
            ),
            CapabilityBinding(
                capability="get_tariff",
                tool="fixture_tariff.get_rates",
                account_id="tariff-csv-alice",
                kind=DataKind.CALCULATED,
                unit="p/kWh",
                fixed_arguments={"scenario": "csv"},
                reviewed=True,
                quality="fixture",
                preference=10,
            ),
        ]

    def registry(self) -> Registry:
        registry = Registry()
        http_connectors.register(registry)
        local.register(registry)
        analytics.register(registry)
        local.register_csv(registry, self.data_root)
        registry.add_toolkit(
            Toolkit(
                id="fixture-tariff",
                name="Fixture tariff",
                description="Deterministic tariff rows for provider substitution tests.",
                runtime="native",
                status="stable",
            )
        )

        async def tariff(args: dict[str, Any], _ctx: ExecutionContext) -> EnergyResult:
            scenario = SCENARIOS[args["scenario"]]
            return EnergyResult(
                data=_timestamp_rows(scenario.tariff_values),
                kind=DataKind.CALCULATED,
                unit="p/kWh",
                source="fixture-tariff",
                timezone="UTC",
                resolution="1800s",
                original_unit="p/kWh",
                field_units={"value": "p/kWh"},
                quality="fixture",
                provenance=[
                    {
                        "provider": "fixture-tariff",
                        "scenario": args["scenario"],
                        "source": "independent-test-input",
                    }
                ],
            )

        registry.add(
            Tool(
                name="fixture_tariff.get_rates",
                toolkit="fixture-tariff",
                description="Read deterministic timestamp-aligned tariff fixture rows.",
                input_schema=schema(
                    {
                        "scenario": {"enum": sorted(SCENARIOS)},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    ["scenario"],
                ),
                capabilities=["get_tariff", "tariff", "price"],
                actions={Action.READ, Action.CALCULATE},
            ),
            tariff,
        )
        return registry

    def agent(self, root: Path) -> EnergyAgent:
        self.write_csv()
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        return EnergyAgent(
            self.registry(),
            root,
            accounts=self.accounts(),
            sites=self.sites(),
            http=client,
            bindings=self.bindings(),
        )


def build_provider_fixture(root: Path) -> ProviderFixture:
    """Create the disposable data root and return a multi-provider fixture."""

    fixture = ProviderFixture(root.resolve(), ProviderHTTPFixture())
    fixture.write_csv()
    return fixture
