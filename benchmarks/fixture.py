"""Deterministic synthetic data for the live energy-agent benchmark.

The fixture is intentionally local and clearly labelled.  It gives a model a
small, heterogeneous site context without touching private provider accounts
or real customer data.  The gateway itself remains the production runtime and
MCP server used by the project.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FIXTURE_USER = "benchmark-user"
FIXTURE_SITE = "synthetic-site"
FIXTURE_DATE = "2026-09-29"


@dataclass(frozen=True)
class Fixture:
    """Paths and public metadata for one isolated benchmark fixture."""

    root: Path
    config_path: Path
    state_dir: Path
    manifest: dict[str, Any]


def _rows() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    # 2026-09-29 00:00 in Europe/London is 2026-09-28 23:00Z.  Starting at
    # that instant makes the fixed fixture cover the complete declared local
    # benchmark day without relying on the machine clock.
    start = datetime(2026, 9, 28, 23, tzinfo=UTC)
    meter: list[dict[str, Any]] = []
    solar: list[dict[str, Any]] = []
    forecast: list[dict[str, Any]] = []
    grid: list[dict[str, Any]] = []
    for index in range(48):
        timestamp = start + timedelta(minutes=30 * index)
        # A flat base with two deliberate but explainable demand changes gives
        # the anomaly and peak prompts an objective answer.
        kwh = 0.25
        if index == 20:
            kwh = 3.5
        elif index == 37:
            kwh = 2.0
        hour = timestamp.hour + timestamp.minute / 60
        daylight = max(0.0, 1.0 - abs(hour - 12.5) / 7.0)
        solar_kwh = round(1.8 * daylight, 3)
        temperature = round(9.0 + 7.0 * max(0.0, 1.0 - abs(hour - 13.0) / 13.0), 2)
        price = 0.11 if 0 <= hour < 7 else 0.31 if 16 <= hour < 20 else 0.22
        carbon = 75.0 if 11 <= hour < 15 else 245.0 if 17 <= hour < 20 else 160.0
        row = {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "kwh": kwh,
            "power_kw": round(kwh * 2, 3),
            "temperature_c": temperature,
            "price_gbp_per_kwh": price,
            "carbon_g_per_kwh": carbon,
        }
        meter.append(row)
        solar.append(
            {
                "timestamp": row["timestamp"],
                "solar_kwh": solar_kwh,
                "power_kw": round(solar_kwh * 2, 3),
            }
        )
        forecast_timestamp = timestamp + timedelta(days=2)
        forecast.append(
            {
                "timestamp": forecast_timestamp.isoformat().replace("+00:00", "Z"),
                "forecast_solar_kwh": round(solar_kwh * 0.9, 3),
                "forecast_carbon_g_per_kwh": carbon,
            }
        )
        # This is a separate regional-grid power series.  It deliberately has
        # MW semantics and its own values/file; it is never a substitute for
        # site PV energy in kWh.
        grid_mw = 1.35 if 7 <= hour < 16 else 1.85 if 16 <= hour < 20 else 1.05
        grid.append({"timestamp": row["timestamp"], "grid_mw": grid_mw})
    return meter, solar, forecast, grid


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_fixture(root: Path) -> Fixture:
    """Create an isolated fixture and return its server configuration.

    The output has no credentials and all values are deterministic.  Callers
    should treat the directory as disposable benchmark input.
    """

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    meter, solar, forecast, grid = _rows()
    _write_csv(root / "meter.csv", meter)
    _write_csv(root / "solar.csv", solar)
    _write_csv(root / "forecast.csv", forecast)
    _write_csv(root / "grid.csv", grid)
    manifest: dict[str, Any] = {
        "synthetic": True,
        "fixture_id": "energy-agent-tools-benchmark-v1",
        "date": FIXTURE_DATE,
        "timezone": "Europe/London",
        "scenario_clock": {
            "today": "2026-09-30",
            "yesterday": "2026-09-29",
            "tomorrow": "2026-10-01",
        },
        "user_id": FIXTURE_USER,
        "site_id": FIXTURE_SITE,
        "files": {
            "meter.csv": {
                "kind": "metered",
                "unit": "kWh",
                "description": "Synthetic half-hour building electricity readings.",
            },
            "solar.csv": {
                "kind": "metered",
                "unit": "kWh",
                "description": "Synthetic PV generation readings.",
            },
            "forecast.csv": {
                "kind": "forecast",
                "unit": "kWh",
                "description": "Synthetic forecast rows, included to test data-kind discipline.",
            },
            "grid.csv": {
                "kind": "metered",
                "unit": "MW",
                "description": (
                    "Synthetic regional-grid generation power; separate from site PV kWh."
                ),
            },
        },
        "disclaimer": (
            "Synthetic benchmark data. Values do not describe a real site, customer, "
            "provider account, or grid event."
        ),
    }
    config = {
        "user_id": FIXTURE_USER,
        "site_id": FIXTURE_SITE,
        "data_root": str(root),
        "sites": [
            {
                "id": FIXTURE_SITE,
                "user_id": FIXTURE_USER,
                "name": "Synthetic test building",
                "timezone": "Europe/London",
                "latitude": 51.5,
                "longitude": -0.12,
            }
        ],
        "assets": [
            {
                "id": "synthetic-meter",
                "site_id": FIXTURE_SITE,
                "kind": "meter",
                "name": "Synthetic electricity meter",
                "metadata": {"file": "meter.csv", "unit": "kWh", "kind": "metered"},
            },
            {
                "id": "synthetic-pv",
                "site_id": FIXTURE_SITE,
                "kind": "pv",
                "name": "Synthetic rooftop PV",
                "metadata": {"file": "solar.csv", "unit": "kWh", "kind": "metered"},
            },
            {
                "id": "synthetic-battery",
                "site_id": FIXTURE_SITE,
                "kind": "battery",
                "name": "Synthetic 4 kWh battery",
                "metadata": {"capacity_kwh": 4, "max_charge_kw": 2},
            },
            {
                "id": "synthetic-grid",
                "site_id": FIXTURE_SITE,
                "kind": "grid",
                "name": "Synthetic regional grid generation",
                "metadata": {"fixture_file": "grid.csv", "declared_unit": "MW"},
            },
        ],
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return Fixture(root, config_path, root / "state", manifest)
