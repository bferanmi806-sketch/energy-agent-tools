"""Deterministic MCP reference agent. No LLM or provider API key is required."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "energy_agent_tools", "serve", "--config", "examples/config.json"],
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as agent:
        await agent.initialize()
        search = await agent.call_tool(
            "ENERGY_SEARCH_TOOLS", {"query": "CSV meter consumption", "limit": 3}
        )
        found = search.structuredContent["tools"]
        assert "CSV_READ_TIMESERIES" in {t["name"] for t in found}
        result = await agent.call_tool(
            "ENERGY_MULTI_EXECUTE_TOOL",
            {
                "calls": [
                    {
                        "tool": "CSV_READ_TIMESERIES",
                        "arguments": {
                            "file": "meter.csv",
                            "kind": "metered",
                            "unit": "kWh",
                            "timezone": "UTC",
                        },
                        "persist": True,
                    }
                ]
            },
        )
        output = result.structuredContent["results"][0]
        assert output["ok"], output
        artifact = output["result"]["data"]["artifact_id"]
        total = await agent.call_tool(
            "ENERGY_MULTI_EXECUTE_TOOL",
            {
                "calls": [
                    {
                        "tool": "WORKBENCH_SUMMARIZE",
                        "arguments": {"artifact_id": artifact, "column": "kwh"},
                    }
                ]
            },
        )
        print(total.structuredContent)


if __name__ == "__main__":
    asyncio.run(main())
