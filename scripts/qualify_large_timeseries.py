"""Qualify a generated large-site stream through the shipped SDK, without network I/O."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.workbench import Workbench


async def qualify(count: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="energy-large-site-") as folder:
        root = Path(folder)
        data = root / "data"
        data.mkdir()
        start = datetime(2024, 1, 1, tzinfo=UTC)
        with (data / "site.csv").open("w") as stream:
            stream.write("timestamp,end,value\n")
            for index in range(count):
                left = start + timedelta(minutes=index)
                right = left + timedelta(minutes=1)
                stream.write(
                    f"{left.isoformat().replace('+00:00', 'Z')},{right.isoformat().replace('+00:00', 'Z')},{index % 13}\n"
                )
        config = {
            "sites": [
                {
                    "id": "site",
                    "user_id": "owner",
                    "name": "Synthetic large site",
                    "timezone": "UTC",
                }
            ],
            "assets": [
                {
                    "id": "feeder",
                    "site_id": "site",
                    "kind": "meter",
                    "name": "Synthetic interval feeder",
                }
            ],
        }
        async with EnergyAgentTools(root / "state", config, data_root=data) as energy:
            session = energy.session("owner", "site")
            durations = {}
            tracemalloc.start()
            try:
                before = time.monotonic()
                imported = await session.execute(
                    "DATASET_IMPORT_CSV",
                    {
                        "file": "site.csv",
                        "kind": "metered",
                        "unit": "kWh",
                        "timezone": "UTC",
                        "quantity_shape": "interval",
                        "resolution": "1min",
                    },
                    asset_id="feeder",
                )
                durations["import_seconds"] = time.monotonic() - before
                assert imported["ok"], imported
                ref = imported["result"]["data"]["dataset_id"]
                before = time.monotonic()
                summary = await session.execute(
                    "DATASET_SUMMARIZE",
                    {"artifact_id": ref, "column": "value"},
                    input_artifacts=[ref],
                )
                durations["summary_seconds"] = time.monotonic() - before
                assert summary["ok"], summary
                cycles, remainder = divmod(count, 13)
                expected = cycles * 78 + remainder * (remainder - 1) // 2
                measured = summary["result"]["data"]
                assert measured["count"] == count and measured["missing"] == 0
                assert math.isclose(measured["sum"], expected, rel_tol=1e-12, abs_tol=1e-6)
                before = time.monotonic()
                window = await session.execute(
                    "WORKBENCH_WINDOW",
                    {
                        "artifact_id": ref,
                        "timestamp": "timestamp",
                        "start": start.isoformat(),
                        "end": (start + timedelta(days=1)).isoformat(),
                    },
                    input_artifacts=[ref],
                    persist=True,
                )
                durations["day_window_seconds"] = time.monotonic() - before
                assert window["ok"], window
                selected = energy.agent.workbench.read(
                    session.context, window["result"]["data"]["artifact_id"]
                )
                assert len(selected.data) == 1440 and selected.kind.value == "metered"
                page = await session.execute(
                    "DATASET_PAGE", {"artifact_id": ref, "columns": ["value"], "limit": 1000}
                )
                assert page["ok"] and page["result"]["data"]["rows"]
                assert len(json.dumps(page).encode()) < energy.agent.workbench.inline_bytes
                foreign = await energy.session("other").execute(
                    "DATASET_PAGE", {"artifact_id": ref}
                )
                assert not foreign["ok"] and foreign["error"]["code"] == "artifact_not_found"
                backup = root / "backup.tar.gz"
                create_backup(root / "state", backup)
                restore_backup(backup, root / "restored")
                restored = Workbench(root / "restored")
                final_page = restored.partitioned.read_page(
                    session.context, ref, offset=count - 1, limit=1
                )
                assert final_page["total_rows"] == count
                assert final_page["rows"][0]["value"] == str((count - 1) % 13)
                _, peak = tracemalloc.get_traced_memory()
                assert peak < 32 * 1024 * 1024, peak
            finally:
                tracemalloc.stop()
            return {
                "fixture": {
                    "synthetic": True,
                    "physical_meter": False,
                    "rows": count,
                    "cadence": "1 minute",
                },
                "import": {
                    "kind": imported["result"]["kind"],
                    "dataset_id": ref,
                    "site_id": imported["result"]["site_id"],
                    "asset_id": imported["result"]["asset_id"],
                },
                "summary": measured,
                "independent_expected_kwh": expected,
                "window_rows": len(selected.data),
                "inline_page_rows": page["result"]["data"]["returned_rows"],
                "peak_traced_python_bytes": peak,
                "timings": durations,
                "backup_restore": "passed",
                "foreign_scope": foreign["error"]["code"],
                "limits": [
                    "Python allocations are traced; this is not an RSS or native SQLite memory measurement.",
                    "Temporal window selection scans chunks; it is not indexed by timestamp.",
                    "Bulk writes share SQLite's single writer; this is not a concurrent load or sustained soak qualification.",
                    "Meter semantics are synthetic declarations; no physical installation was read.",
                ],
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1_000_000)
    args = parser.parse_args()
    if not 100_001 <= args.rows <= 1_000_000:
        parser.error("rows must be between 100001 and 1000000")
    print(json.dumps(asyncio.run(qualify(args.rows)), indent=2))


if __name__ == "__main__":
    main()
