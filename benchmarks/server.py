"""Native MCP server for the isolated synthetic benchmark fixture.

This module intentionally builds the same ``EnergyAgent`` and ``create_server``
used by the shipped gateway.  Only the HTTP boundary differs: synthetic
handlers are registered locally so a benchmark cannot accidentally query a
public provider or a private account.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from energy_agent_tools.capabilities import CapabilityBinding
from energy_agent_tools.connectors import engineering, local
from energy_agent_tools.models import (
    Action,
    Asset,
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
from energy_agent_tools.server import create_server

from .fixture import FIXTURE_SITE, FIXTURE_USER


def _read_rows(root: Path, filename: str) -> list[dict[str, Any]]:
    with (root / filename).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _parse_timestamp(value: Any) -> datetime:
    """Parse an offset-aware timestamp and compare it on the UTC timeline."""

    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("fixture timestamps must be ISO-8601 values") from exc
    if timestamp.tzinfo is None:
        raise ValueError("fixture timestamps must include a UTC offset")
    return timestamp.astimezone(UTC)


def _filter_rows(rows: list[dict[str, Any]], arguments: dict[str, Any]) -> list[dict[str, Any]]:
    start = _parse_timestamp(arguments["start"]) if arguments.get("start") else None
    end = _parse_timestamp(arguments["end"]) if arguments.get("end") else None
    output: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _parse_timestamp(row.get("timestamp", ""))
        if start and timestamp < start:
            continue
        if end and timestamp >= end:
            continue
        output.append(row)
    return output


def _synthetic_handler(
    root: Path,
    filename: str,
    field: str,
    *,
    kind: DataKind,
    unit: str,
    source: str,
    asset_id: str,
    description: str,
):
    async def handler(args: dict[str, Any], ctx: ExecutionContext) -> EnergyResult:
        rows = _filter_rows(_read_rows(root, filename), args)
        values = [
            {"timestamp": row["timestamp"], "value": float(row[field]), "asset_id": asset_id}
            for row in rows
        ]
        return EnergyResult(
            data=values,
            kind=kind,
            unit=unit,
            source=source,
            provider="synthetic-fixture",
            site_id=FIXTURE_SITE,
            asset_id=asset_id,
            timezone="UTC",
            resolution="30min",
            quality="fixture",
            assumptions=[description],
            warnings=["Synthetic benchmark data; no real provider or meter was queried."],
            provenance=[
                {
                    "fixture": "energy-agent-tools-benchmark-v1",
                    "file": filename,
                    "field": field,
                    "declared_kind": kind.value,
                    "declared_unit": unit,
                }
            ],
        )

    return handler


def build_fixture_agent(root: Path, state_dir: Path) -> EnergyAgent:
    registry = Registry()
    # Native workbench and engineering operations are retained.  No HTTP
    # connector is registered in this process, so every external-looking
    # capability is backed by the reviewed synthetic handlers below.
    engineering.register(registry)
    local.register(registry)
    local.register_csv(registry, root)

    registry.add_toolkit(
        Toolkit(
            id="synthetic-fixture",
            name="Synthetic benchmark fixture",
            description="Deterministic, local-only energy rows for model evaluation.",
            runtime="native",
            status="stable",
            categories=["meter", "pv", "tariff", "carbon", "weather", "grid"],
        )
    )
    argument_schema = schema(
        {
            "start": {"type": "string"},
            "end": {"type": "string"},
        }
    )
    definitions = [
        (
            "fixture.get_consumption",
            "Read synthetic half-hour building electricity consumption.",
            "meter.csv",
            "kwh",
            DataKind.METERED,
            "kWh",
            "synthetic-meter",
            "Synthetic meter rows are declared metered fixture values.",
            "get_energy_consumption",
        ),
        (
            "fixture.get_generation",
            "Read synthetic PV generation rows.",
            "solar.csv",
            "solar_kwh",
            DataKind.METERED,
            "kWh",
            "synthetic-pv",
            "Synthetic PV rows are declared metered fixture values.",
            "get_generation",
        ),
        (
            "fixture.get_solar_forecast",
            "Read synthetic solar forecast rows.",
            "forecast.csv",
            "forecast_solar_kwh",
            DataKind.FORECAST,
            "kWh",
            "synthetic-pv",
            "Synthetic PV forecast rows are not measured generation.",
            "get_solar_forecast",
        ),
        (
            "fixture.get_tariff",
            "Read synthetic interval electricity prices.",
            "meter.csv",
            "price_gbp_per_kwh",
            DataKind.CALCULATED,
            "GBP/kWh",
            "synthetic-meter",
            "Synthetic prices are fixture inputs, not a supplier account.",
            "get_tariff",
        ),
        (
            "fixture.get_carbon",
            "Read synthetic interval carbon intensity.",
            "meter.csv",
            "carbon_g_per_kwh",
            DataKind.METERED,
            "gCO2/kWh",
            "synthetic-meter",
            "Synthetic carbon rows are local fixture values.",
            "get_carbon_intensity",
        ),
        (
            "fixture.get_grid_generation",
            "Read synthetic regional grid generation power, separate from site PV energy.",
            "grid.csv",
            "grid_mw",
            DataKind.METERED,
            "MW",
            "synthetic-grid",
            "Synthetic regional-grid generation is a separate metered power fixture, not site PV.",
            "get_grid_generation",
        ),
        (
            "fixture.get_weather",
            "Read synthetic temperature rows.",
            "meter.csv",
            "temperature_c",
            DataKind.METERED,
            "°C",
            "synthetic-meter",
            "Synthetic temperature rows are local fixture values.",
            "get_weather",
        ),
    ]
    bindings: list[CapabilityBinding] = []
    for (
        name,
        description,
        filename,
        field,
        kind,
        unit,
        asset_id,
        assumption,
        capability,
    ) in definitions:
        registry.add(
            Tool(
                name=name,
                toolkit="synthetic-fixture",
                description=description,
                input_schema=argument_schema,
                capabilities=[capability],
                actions={Action.READ},
                result_kind=kind,
                result_unit=unit,
            ),
            _synthetic_handler(
                root,
                filename,
                field,
                kind=kind,
                unit=unit,
                source="synthetic-fixture",
                asset_id=asset_id,
                description=assumption,
            ),
        )
        bindings.append(
            CapabilityBinding(
                capability=capability,
                tool=name,
                asset_id=asset_id if capability == "get_grid_generation" else None,
                kind=kind,
                unit=unit,
                resolution="30min",
                quality="verified",
                preference=50,
                reviewed=True,
            )
        )
    site = Site(
        id=FIXTURE_SITE,
        user_id=FIXTURE_USER,
        name="Synthetic test building",
        timezone="Europe/London",
        latitude=51.5,
        longitude=-0.12,
    )
    assets = [
        Asset(
            id="synthetic-meter",
            site_id=FIXTURE_SITE,
            kind="meter",
            name="Synthetic electricity meter",
            metadata={"fixture_file": "meter.csv", "declared_unit": "kWh"},
        ),
        Asset(
            id="synthetic-pv",
            site_id=FIXTURE_SITE,
            kind="pv",
            name="Synthetic rooftop PV",
            metadata={"fixture_file": "solar.csv", "declared_unit": "kWh"},
        ),
        Asset(
            id="synthetic-battery",
            site_id=FIXTURE_SITE,
            kind="battery",
            name="Synthetic 4 kWh battery",
            metadata={"capacity_kwh": 4, "max_charge_kw": 2},
        ),
        Asset(
            id="synthetic-grid",
            site_id=FIXTURE_SITE,
            kind="grid",
            name="Synthetic regional grid generation",
            metadata={"fixture_file": "grid.csv", "declared_unit": "MW"},
        ),
    ]
    return EnergyAgent(
        registry,
        state_dir,
        sites=[site],
        assets=assets,
        bindings=bindings,
        calendar_clock=lambda: datetime(2026, 9, 30, 12, tzinfo=UTC),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local energy benchmark MCP fixture")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--scenario")
    args = parser.parse_args()
    if args.scenario:
        from .environments import build_environment

        built = build_environment(args.scenario, args.root.resolve(), args.state_dir.resolve())
        agent = built.agent
        session = agent.session(built.context.user_id, built.context.site_id)
    else:
        agent = build_fixture_agent(args.root.resolve(), args.state_dir.resolve())
        session = agent.session(FIXTURE_USER, FIXTURE_SITE)
    create_server(agent, session).run(transport="stdio")


if __name__ == "__main__":
    main()
