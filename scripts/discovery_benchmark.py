"""Reproducible deterministic discovery relevance and scale measurements."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from energy_agent_tools.app import build_agent
from energy_agent_tools.models import DataKind, EnergyResult, Tool, Toolkit, schema
from energy_agent_tools.registry import Registry

QUERIES = [
    ("How much electricity did I use yesterday?", "octopus_energy.get_consumption"),
    ("What is the cleanest time to charge?", "carbon_intensity_gb.get_intensity"),
    ("Find tomorrow's solar irradiance forecast", "open_meteo.get_forecast"),
    ("Check voltage problems in an AC network", "engineering.run_power_flow"),
    ("Calculate building ventilation and envelope heat loss", "engineering.calculate_heat_loss"),
    ("Compare electricity prices for my tariff", "octopus_energy.get_tariffs"),
    ("Investigate a consumption spike", "WORKBENCH_ANOMALY"),
    ("Convert cumulative counter into interval energy", "WORKBENCH_ENERGY_OPERATION"),
]


async def handler(args, ctx):
    return EnergyResult(data=[], kind=DataKind.CALCULATED, unit="1", source="benchmark")


def main():
    with TemporaryDirectory() as directory:
        agent = build_agent(Path(directory))
        relevance = []
        for query, expected in QUERIES:
            matches = [tool.name for tool in agent.registry.search(query)]
            relevance.append(
                {"query": query, "expected": expected, "top5": matches, "hit": expected in matches}
            )
        scale = Registry()
        scale.add_toolkit(
            Toolkit(
                id="scale",
                name="Scale",
                description="Generated catalogue",
                runtime="native",
                status="experimental",
            )
        )
        for index in range(10000):
            scale.add(
                Tool(
                    name=f"scale.meter_{index}",
                    toolkit="scale",
                    description=f"Read interval electricity consumption meter building {index}",
                    input_schema=schema({}),
                    capabilities=["get_energy_consumption"],
                ),
                handler,
            )
        scale.search("electricity consumption 9999")
        durations = []
        for _ in range(100):
            start = time.perf_counter()
            scale.search("electricity consumption 9999")
            durations.append((time.perf_counter() - start) * 1000)
        print(
            json.dumps(
                {
                    "relevance": relevance,
                    "recall_at_5": sum(r["hit"] for r in relevance) / len(relevance),
                    "scale": {
                        "actions": 10000,
                        "queries": 100,
                        "median_ms": statistics.median(durations),
                        "p95_ms": sorted(durations)[94],
                    },
                    "qualification": "Deterministic synthetic catalogue; no embedding or learned ranking.",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
