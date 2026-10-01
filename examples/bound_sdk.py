"""Run a provider-bound SDK walkthrough with a declared synthetic fixture.

Run from the repository root with ``uv run python examples/bound_sdk.py``.
The fixture is written to a temporary directory and no provider credential or
network service is used.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from energy_agent_tools import EnergyAgentTools


def write_fixture(data_root: Path) -> None:
    """Write two explicit half-hourly synthetic series for this example."""
    data_root.mkdir(parents=True, exist_ok=True)
    rows = [
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:30:00+00:00", "0.50", "0.20"),
        ("2026-01-01T00:30:00+00:00", "2026-01-01T01:00:00+00:00", "0.75", "0.25"),
        ("2026-01-01T01:00:00+00:00", "2026-01-01T01:30:00+00:00", "0.25", "0.15"),
        ("2026-01-01T01:30:00+00:00", "2026-01-01T02:00:00+00:00", "0.50", "0.30"),
    ]
    values_by_file = {
        "meter.csv": [meter_value for _, _, meter_value, _ in rows],
        "tariff.csv": [tariff_value for _, _, _, tariff_value in rows],
    }
    for filename, values in values_by_file.items():
        with (data_root / filename).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "end", "value"])
            writer.writeheader()
            for (timestamp, end, _, _), value in zip(rows, values, strict=True):
                writer.writerow({"timestamp": timestamp, "end": end, "value": value})


async def main() -> None:
    with TemporaryDirectory(prefix="energy-agent-bound-sdk-") as temporary:
        root = Path(temporary)
        data_root = root / "fixture"
        write_fixture(data_root)
        print("FIXTURE: four synthetic half-hourly meter and tariff rows")
        print("CREDENTIALS: none; provider calls are not made")

        async with EnergyAgentTools(root / "state", data_root=data_root) as tools:
            session = tools.session("synthetic-user")

            search = await session.dispatch(
                "ENERGY_SEARCH_TOOLS", {"query": "CSV meter consumption", "limit": 3}
            )
            assert "tools" in search, search
            print("DISPATCH: ENERGY_SEARCH_TOOLS returned", len(search["tools"]), "schemas")

            for provider in ("openai", "openai-responses", "anthropic"):
                schemas = await session.tools(provider)
                print(f"SCHEMAS: {provider} returned {len(schemas)} provider functions")

            meter = await session.execute(
                "CSV_READ_TIMESERIES",
                {
                    "file": "meter.csv",
                    "kind": "metered",
                    "unit": "kWh",
                    "timezone": "UTC",
                },
                persist=True,
            )
            tariff = await session.execute(
                "CSV_READ_TIMESERIES",
                {
                    "file": "tariff.csv",
                    "kind": "calculated",
                    "unit": "GBP/kWh",
                    "timezone": "UTC",
                },
                persist=True,
            )
            assert meter["ok"], meter
            assert tariff["ok"], tariff
            meter_id = meter["result"]["data"]["artifact_id"]
            tariff_id = tariff["result"]["data"]["artifact_id"]

            cost = await session.execute(
                "WORKBENCH_ENERGY_OPERATION",
                {
                    "operation": "cost",
                    "artifact_ids": [meter_id, tariff_id],
                    "parameters": {
                        "timestamp": "timestamp",
                        "column": "value",
                        "second_timestamp": "timestamp",
                        "second_column": "value",
                    },
                },
                input_artifacts=[meter_id, tariff_id],
            )
            assert cost["ok"], cost
            result = cost["result"]
            print(
                "COST:",
                json.dumps(
                    {
                        "unit": result["unit"],
                        "rows": result["data"],
                        "input_artifacts": [meter_id, tariff_id],
                    }
                ),
            )


if __name__ == "__main__":
    asyncio.run(main())
