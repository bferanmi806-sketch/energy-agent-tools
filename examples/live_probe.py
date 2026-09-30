"""Opt-in public-provider verification. Writes summaries, never private raw data."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from energy_agent_tools.app import build_agent


async def main(output: Path):
    agent = build_agent(Path(".energy-agent/live-probe"))
    session = agent.session("public-probe")
    checks = []
    try:
        # Discover a currently published public Octopus tariff instead of hardcoding an expired product.
        product = await agent.http.get(
            "https://api.octopus.energy/v1/products/", params={"is_variable": "true"}
        )
        product.raise_for_status()
        products = product.json()["results"]
        agile = next((p for p in products if p["code"].startswith("AGILE")), products[0])
        detail = await agent.http.get(f"https://api.octopus.energy/v1/products/{agile['code']}/")
        detail.raise_for_status()
        tariffs = detail.json().get("single_register_electricity_tariffs", {})
        tariff = next(iter(tariffs.values()))["direct_debit_monthly"]["code"]
        now = datetime.now(UTC).replace(microsecond=0)
        probes = [
            ("carbon_intensity_gb.get_intensity", {}),
            (
                "carbon_intensity_gb.get_intensity",
                {"start": now.isoformat(), "horizon_hours": 24, "region_id": 13},
            ),
            ("open_meteo.get_forecast", {"latitude": 51.5, "longitude": -0.12, "forecast_days": 1}),
            ("octopus_energy.get_tariffs", {"product_code": agile["code"], "tariff_code": tariff}),
            (
                "elexon.get_grid_data",
                {
                    "dataset": "FUELHH",
                    "start": (now - timedelta(days=1)).isoformat(),
                    "end": now.isoformat(),
                },
            ),
        ]
        for tool, arguments in probes:
            result = await agent.execute(session, tool, arguments, persist=True)
            check = {"tool": tool, "arguments": arguments, "ok": result["ok"]}
            if result["ok"]:
                envelope = result["result"]
                check.update(
                    {
                        "kind": envelope["kind"],
                        "unit": envelope["unit"],
                        "source": envelope["source"],
                        "rows": envelope["data"]["rows"],
                        "warnings": envelope["warnings"],
                    }
                )
                if not envelope["data"]["rows"]:
                    check["ok"] = False
                    check["error"] = {"code": "empty_live_result"}
            else:
                check["error"] = result["error"]
            checks.append(check)
            print(json.dumps(check))
    finally:
        await agent.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"checked_at": datetime.now(UTC).isoformat(), "checks": checks}, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(".energy-agent/live-probe-summary.json")
    )
    asyncio.run(main(parser.parse_args().output))
