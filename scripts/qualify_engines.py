"""Run the real optional engineering engines and write qualification evidence.

The OpenDSS cases in this script are deliberately small and explicit.  They are
independent checks of the adapter's balanced and unbalanced contracts, not claims
about the accuracy of a distribution model outside the supplied topology.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from energy_agent_tools.connectors.dss import power_flow

_ENERGYPLUS_RELEASE_URL = "https://github.com/NatLabRockies/EnergyPlus/releases/tag/v26.2.0"
_ENERGYPLUS_ASSET_NAME = "EnergyPlus-26.2.0-4bd7a1f26f-Darwin-macOS13.3-x86_64.tar.gz"
_ENERGYPLUS_ASSET_SHA256 = "4746bb63f8419c3666fb1e390e2a387a0300474113694ebcd14b3c9745d3b68f"
_ENERGYPLUS_MODEL = "ExampleFiles/1ZoneUncontrolled3SurfaceZone.idf"
_ENERGYPLUS_WEATHER = "WeatherData/USA_CO_Golden-NREL.724666_TMY3.epw"


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _numeric_summary(rows: list[dict[str, Any]], key: str, unit: str) -> dict[str, Any]:
    values = [float(row[key].strip()) for row in rows if row.get(key, "").strip()]
    if not values or not all(math.isfinite(value) for value in values):
        raise RuntimeError(f"EnergyPlus output field {key!r} had no finite values")
    return {
        "field": key,
        "unit": unit,
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "sum": sum(values),
        "sample": values[:3],
    }


async def _qualify_energyplus(
    root: Path, executable: Path | None, archive: Path | None
) -> dict[str, Any]:
    """Run an official example through the existing fixed executable adapter."""

    from energy_agent_tools.connectors.extended import register_energyplus
    from energy_agent_tools.registry import Registry

    root = root.expanduser().resolve()
    executable = (executable or root / "energyplus").expanduser().resolve()
    model = root / _ENERGYPLUS_MODEL
    weather = root / _ENERGYPLUS_WEATHER
    if (
        not root.is_dir()
        or not executable.is_file()
        or not model.is_file()
        or not weather.is_file()
    ):
        raise RuntimeError("EnergyPlus root, executable, official example IDF, and EPW must exist")
    if not executable.stat().st_mode & 0o111:
        raise RuntimeError("EnergyPlus executable is not executable")

    release_asset: dict[str, Any] = {
        "release_url": _ENERGYPLUS_RELEASE_URL,
        "asset_name": _ENERGYPLUS_ASSET_NAME,
        "expected_sha256": _ENERGYPLUS_ASSET_SHA256,
    }
    if archive is not None:
        archive = archive.expanduser().resolve()
        if not archive.is_file():
            raise RuntimeError(f"EnergyPlus archive does not exist: {archive}")
        actual_sha256 = _sha256(archive)
        release_asset["actual_sha256"] = actual_sha256
        release_asset["sha256_matches_release"] = actual_sha256 == _ENERGYPLUS_ASSET_SHA256
        if actual_sha256 != _ENERGYPLUS_ASSET_SHA256:
            raise RuntimeError(
                "EnergyPlus archive digest does not match the official release asset"
            )

    version_process = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    version_stdout = version_process.stdout.strip()
    if not version_stdout.startswith("EnergyPlus, Version 26.2.0"):
        raise RuntimeError(f"unexpected EnergyPlus version output: {version_stdout!r}")

    registry = Registry()
    register_energyplus(registry, executable, root)
    result = await registry.handlers["energyplus.run_simulation"](
        {"model": _ENERGYPLUS_MODEL, "weather": _ENERGYPLUS_WEATHER, "readvars": True},
        None,  # type: ignore[arg-type]
    )
    rows = result.data["rows"]
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("EnergyPlus adapter returned no readvars CSV rows")
    equipment_positive = next(
        key for key in rows[0] if "TEST 352A:Other Equipment Total Heating Energy" in key
    )
    equipment_negative = next(
        key for key in rows[0] if "TEST 352 MINUS:Other Equipment Total Heating Energy" in key
    )
    positive_values = [
        (float(row[equipment_positive]), float(row[equipment_negative]))
        for row in rows
        if row[equipment_positive].strip() and row[equipment_negative].strip()
    ]
    balance_errors = [positive + negative for positive, negative in positive_values]
    if not balance_errors or not all(math.isfinite(value) for value in balance_errors):
        raise RuntimeError("EnergyPlus monthly energy-balance rows were not finite")
    monthly_balance = {
        "positive_field": equipment_positive,
        "negative_field": equipment_negative,
        "unit": "J",
        "count": len(balance_errors),
        "maximum_absolute_net_j": max(abs(value) for value in balance_errors),
        "sum_positive_j": sum(pair[0] for pair in positive_values),
        "sum_negative_j": sum(pair[1] for pair in positive_values),
        "sample_pairs_j": [list(pair) for pair in positive_values[:3]],
    }
    temperature_key = next(key for key in rows[0] if "Zone Mean Air Temperature" in key)
    convection_key = next(
        key for key in rows[0] if "Zone Air Heat Balance Surface Convection Rate" in key
    )
    storage_key = next(
        key for key in rows[0] if "Zone Air Heat Balance Air Energy Storage Rate" in key
    )
    checks = {
        "binary_version_is_26_2_0": version_stdout.startswith("EnergyPlus, Version 26.2.0"),
        "adapter_completed": result.data["model"] == Path(_ENERGYPLUS_MODEL).name,
        "adapter_emitted_readvars_rows": len(rows) > 8_000,
        "monthly_energy_balance_abs_net_lt_1e-6_j": monthly_balance["maximum_absolute_net_j"]
        < 1e-6,
        "temperature_outputs_finite": True,
        "heat_balance_outputs_finite": True,
    }
    numeric_outputs = {
        "zone_mean_air_temperature": _numeric_summary(rows, temperature_key, "C"),
        "zone_air_heat_balance_surface_convection_rate": _numeric_summary(
            rows, convection_key, "W"
        ),
        "zone_air_heat_balance_air_energy_storage_rate": _numeric_summary(rows, storage_key, "W"),
        "test_352a_other_equipment_heating_energy": _numeric_summary(rows, equipment_positive, "J"),
        "test_352_minus_other_equipment_heating_energy": _numeric_summary(
            rows, equipment_negative, "J"
        ),
    }
    return {
        "status": "qualified-real-engine",
        "real_engine": True,
        "release_asset": release_asset,
        "binary": {"path": executable.name, "version_stdout": version_stdout},
        "official_example": {"model": _ENERGYPLUS_MODEL, "weather": _ENERGYPLUS_WEATHER},
        "adapter_result": {
            "source": result.source,
            "kind": result.kind.value,
            "unit": result.unit,
            "quality": result.quality,
            "files": result.data["files"],
            "row_count": len(rows),
        },
        "numeric_outputs": numeric_outputs,
        "monthly_energy_balance": monthly_balance,
        "checks": checks,
        "passed": all(checks.values()),
        "upstream_sources": [
            _ENERGYPLUS_RELEASE_URL,
            "https://energyplus.readthedocs.io/en/stable/quick_start/quick_start.html",
            "https://energyplus.readthedocs.io/en/latest/api.html",
        ],
    }


async def _run(
    energyplus_root: Path | None = None,
    energyplus_executable: Path | None = None,
    energyplus_archive: Path | None = None,
) -> dict[str, Any]:
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
    energyplus = (
        await _qualify_energyplus(energyplus_root, energyplus_executable, energyplus_archive)
        if energyplus_root is not None
        else {
            "status": "not-run",
            "real_engine": False,
            "reason": "Pass --energyplus-root to run the official local EnergyPlus binary.",
            "passed": False,
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
        "energyplus": energyplus,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evidence/engine-qualification.json"),
        help="Path for the JSON evidence file",
    )
    parser.add_argument(
        "--energyplus-root",
        type=Path,
        help="Extracted official EnergyPlus root containing energyplus, ExampleFiles, and WeatherData",
    )
    parser.add_argument(
        "--energyplus-executable",
        type=Path,
        help="Optional explicit EnergyPlus executable; defaults to <root>/energyplus",
    )
    parser.add_argument(
        "--energyplus-archive",
        type=Path,
        help="Downloaded official EnergyPlus archive to verify against the release SHA-256",
    )
    arguments = parser.parse_args()
    evidence = asyncio.run(
        _run(
            arguments.energyplus_root, arguments.energyplus_executable, arguments.energyplus_archive
        )
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {arguments.output}")
    if not all(case["passed"] for case in evidence["cases"]):
        raise SystemExit("one or more OpenDSS qualification checks failed")
    if arguments.energyplus_root is not None and not evidence["energyplus"]["passed"]:
        raise SystemExit("EnergyPlus qualification checks failed")


if __name__ == "__main__":
    main()
