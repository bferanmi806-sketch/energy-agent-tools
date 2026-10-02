"""Two-bus distribution feeder study with a real local pandapower solver."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.reference_projects._shared import (
    USER_ID,
    execute_capability,
    import_csv,
    make_tools,
    numeric_rows,
    site_config,
    source_summary,
)

SITE_ID = "cambridge-feeder-site"
TIMEZONE = "Europe/London"
LOAD_ASSET = "feeder-head-meter"
NETWORK_ASSET = "feeder-two-bus-model"


async def main() -> None:
    config = site_config(
        site_id=SITE_ID,
        name="Cambridge reference feeder",
        timezone=TIMEZONE,
        latitude=52.21,
        longitude=0.12,
        assets=[
            {
                "id": LOAD_ASSET,
                "site_id": SITE_ID,
                "kind": "feeder-meter",
                "name": "Feeder-head snapshot meter",
            },
            {
                "id": NETWORK_ASSET,
                "site_id": SITE_ID,
                "kind": "distribution-feeder-model",
                "name": "11 kV two-bus feeder",
                "metadata": {
                    "input_model": "caller supplied 11 kV, 1 km feeder with one measured downstream load",
                    "purpose": "read-only balanced snapshot power flow",
                },
            },
        ],
        bindings=[
            {
                "capability": "study_feeder_snapshot",
                "tool": "engineering.run_power_flow",
                "asset_id": NETWORK_ASSET,
                "kind": "simulated",
                "unit": "MW, Mvar, pu",
                "quality": "reviewed-reference-model",
                "reviewed": True,
            }
        ],
    )
    with TemporaryDirectory(prefix="energy-network-reference-") as temporary:
        async with make_tools(Path(temporary) / "state", config) as tools:
            session = tools.session(USER_ID, SITE_ID)
            load_id, load = await import_csv(
                session,
                "network_load.csv",
                kind="metered",
                unit="MW and Mvar",
                timezone=TIMEZONE,
                asset_id=LOAD_ASSET,
                quantity_shape="instantaneous",
            )
            row = numeric_rows(load, ("p_mw", "q_mvar"))[0]
            network = {
                "network": {
                    "sn_mva": 5.0,
                    "f_hz": 50.0,
                    "buses": [
                        {"id": "grid", "name": "Grid supply", "vn_kv": 11.0},
                        {"id": "load", "name": "Measured feeder load", "vn_kv": 11.0},
                    ],
                    "lines": [
                        {
                            "id": "feeder-1",
                            "from_bus": "grid",
                            "to_bus": "load",
                            "length_km": 1.0,
                            "r_ohm_per_km": 0.5,
                            "x_ohm_per_km": 0.4,
                            "max_i_ka": 0.4,
                        }
                    ],
                    "loads": [
                        {
                            "id": "measured-load",
                            "bus": "load",
                            "p_mw": row["p_mw"],
                            "q_mvar": row["q_mvar"],
                        }
                    ],
                    "ext_grid": [{"id": "upstream", "bus": "grid"}],
                }
            }
            result = await execute_capability(
                session,
                "study_feeder_snapshot",
                network,
                asset_id=NETWORK_ASSET,
                input_artifact_ids=[load_id],
                unit="MW, Mvar, pu",
            )
            data = result["data"]
            totals = data["totals"]
            load_out = data["loads"][0]
            load_bus = next(bus for bus in data["buses"] if bus["id"] == "load")
            assert result["kind"] == "simulated"
            assert data["converged"] is True
            assert abs(load_out["p_mw"] - 0.8) < 1e-12
            assert abs(load_out["q_mvar"] - 0.2) < 1e-12
            assert totals["line_loss_mw"] > 0
            assert abs(totals["balance_error_mw"]) < 1e-6
            assert 0.0 < load_bus["vm_pu"] < 1.0
            assert any(item.get("library") == "pandapower" for item in result["provenance"])
            assert any(item.get("artifact_id") == load_id for item in result["provenance"])

            print(
                json.dumps(
                    {
                        "project": "engineering-two-bus-feeder",
                        "site": {"id": SITE_ID, "timezone": TIMEZONE},
                        "assets": [LOAD_ASSET, NETWORK_ASSET],
                        "source": source_summary(load_id, load),
                        "independent_truth": {
                            "metered_load_mw": 0.8,
                            "metered_reactive_load_mvar": 0.2,
                        },
                        "power_flow": {
                            "kind": result["kind"],
                            "source": result["source"],
                            "converged": data["converged"],
                            "load_bus_voltage_pu": load_bus["vm_pu"],
                            "line_loss_mw": totals["line_loss_mw"],
                            "balance_error_mw": totals["balance_error_mw"],
                            "asset_id": result["asset_id"],
                            "provenance": result["provenance"],
                        },
                        "assumptions": result["assumptions"],
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
