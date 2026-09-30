"""Run from the repository with uv run python examples/sdk.py."""

import asyncio
from pathlib import Path

from energy_agent_tools.app import build_agent
from energy_agent_tools.providers import format_tools, resolve_provider_name


async def main():
    agent = build_agent(Path(".energy-agent"), data_root=Path("examples/data"))
    try:
        session = agent.session("local")
        tools = agent.search(session, "CSV meter consumption", limit=3)
        # These schemas can be supplied to provider function calling.
        schemas = format_tools(tools, "openai")
        print("Discovered provider functions:", [s["function"]["name"] for s in schemas])
        # Map normalized provider function names back to registry names.
        name = resolve_provider_name(tools, "CSV_READ_TIMESERIES")
        output = await agent.execute(
            session,
            name,
            {"file": "meter.csv", "kind": "metered", "unit": "kWh", "timezone": "UTC"},
            persist=True,
        )
        if not output["ok"]:
            raise RuntimeError(output["error"])
        artifact = output["result"]["data"]["artifact_id"]
        summary = await agent.execute(
            session, "WORKBENCH_SUMMARIZE", {"artifact_id": artifact, "column": "kwh"}
        )
        print(summary)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
