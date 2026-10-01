"""Run the real optional engineering engines and write qualification evidence.

The OpenDSS cases in this script are deliberately small and explicit.  They are
independent checks of the adapter's balanced and unbalanced contracts, not claims
about the accuracy of a distribution model outside the supplied topology.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from energy_agent_tools.connectors.dss import power_flow


def _case(mode: str) -> dict[str, Any]:
    load: dict[str, Any] = {
        "id": "house",
        "bus": "load",
        "phases": 3,
        "connection": "wye",
        "kv": 11.0,
    }
    if mode == "balanced":
        load.update({"kw": 1_000.0, "kvar": 200.0})
    else:
        load.update({"kw_by_phase": [500.0, 300.0, 100.0], "kvar_by_phase": [100.0, 60.0, 20.0]})
    return {
        "network": {
            "mode": mode,
            "frequency_hz": 50.0,
            "buses": [
                {"id": "grid", "kv_ll": 11.0, "phases": 3},
                {"id": "load", "kv_ll": 11.0, "phases": 3},
            ],
            "source": {"bus": "grid", "kv_ll": 11.0, "phases": 3, "pu": 1.0},
            "lines": [
                {
                    "id": "feeder",
                    "from_bus": "grid",
                    "to_bus": "load",
                    "phases": 3,
                    "length_km": 1.0,
                    "r1_ohm_per_km": 0.2,
                    "x1_ohm_per_km": 0.4,
                    "r0_ohm_per_km": 0.6,
                    "x0_ohm_per_km": 1.2,
                    "ampacity_a": 100.0,
                }
            ],
            "loads": [load],
        }
    }


async def _run() -> dict[str, Any]:
    import opendssdirect as odd

    cases: list[dict[str, Any]] = []
    for mode in ("balanced", "unbalanced"):
        result = await power_flow(_case(mode), None)  # type: ignore[arg-type]
        buses = {str(row["id"]): row for row in result.data["buses"]}
        line = result.data["lines"][0]
        totals = result.data["totals"]
        phase_currents = [float(row["current_a"]) for row in line["phases"]]
        checks = {
            "converged": result.data["converged"] is True,
            "power_balance_kw_abs_lt_0_02": abs(float(totals["balance_error_kw"])) < 0.02,
            "three_phase_bus_output": len(buses["load"]["phases"]) == 3,
            "positive_line_current": max(phase_currents) > 0,
        }
        if mode == "balanced":
            checks["balanced_phase_current_spread_lt_0_01_a"] = (
                max(phase_currents) - min(phase_currents) < 0.01
            )
        else:
            checks["unbalanced_phase_current_order_is_preserved"] = (
                phase_currents[0] > phase_currents[1] > phase_currents[2]
            )
            load_voltages = [float(row["v_mag_pu"]) for row in buses["load"]["phases"]]
            checks["unbalanced_voltage_spread_gt_0_001_pu"] = (
                max(load_voltages) - min(load_voltages) > 0.001
            )
        cases.append(
            {
                "name": mode,
                "result": result.model_dump(mode="json"),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "adapter": "opendss.power_flow",
        "distribution": "opendssdirect.py",
        "distribution_version": importlib.metadata.version("opendssdirect.py"),
        "engine": "DSS-Extensions",
        "engine_version": str(odd.dss.Basic.Version()),
        "cases": cases,
        "qualification": {
            "status": "qualified-bounded-experimental",
            "real_engine": True,
            "scope": "in-memory steady-state balanced and unbalanced three-phase snapshot cases",
            "does_not_cover": [
                "arbitrary DSS model files or script execution",
                "protection, fault, dynamic, time-series, or controller studies",
                "operational safety or equipment certification",
            ],
        },
        "upstream_sources": [
            "https://dss-extensions.org/OpenDSSDirect.py/",
            "https://github.com/dss-extensions/OpenDSSDirect.py",
            "https://github.com/dss-extensions/dss-extensions/blob/main/docs/python_apis.md",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evidence/engine-qualification.json"),
        help="Path for the JSON evidence file",
    )
    arguments = parser.parse_args()
    evidence = asyncio.run(_run())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {arguments.output}")
    if not all(case["passed"] for case in evidence["cases"]):
        raise SystemExit("one or more OpenDSS qualification checks failed")


if __name__ == "__main__":
    main()
