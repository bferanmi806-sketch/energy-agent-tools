from __future__ import annotations

import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from energy_agent_tools.connectors.local import register, register_csv
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent
from energy_agent_tools.server import create_server, provider_tools


async def test_stdio_real_endpoint_search_execute_workbench(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "meter.csv").write_text(
        "timestamp,kwh\n2026-09-29T00:00:00Z,1\n2026-09-29T00:30:00Z,2\n"
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"user_id": "test", "data_root": str(data)}))
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "energy_agent_tools",
            "serve",
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert names == {
            "ENERGY_SEARCH_TOOLS",
            "ENERGY_GET_TOOL",
            "ENERGY_MANAGE_CONNECTIONS",
            "ENERGY_MULTI_EXECUTE_TOOL",
            "ENERGY_LIST_TOOLKITS",
            "ENERGY_LIST_SKILLS",
            "ENERGY_SITE_CONTEXT",
        }
        response = await client.call_tool(
            "ENERGY_SEARCH_TOOLS", {"query": "local CSV electricity consumption", "limit": 3}
        )
        found = response.structuredContent
        assert found is not None
        assert len(found["tools"]) <= 3
        assert any(t["name"] == "CSV_READ_TIMESERIES" for t in found["tools"])
        execute = await client.call_tool(
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
        output = execute.structuredContent["results"][0]
        assert output["ok"], output
        assert output["result"]["kind"] == "metered"
        artifact = output["result"]["data"]["artifact_id"]
        summary = await client.call_tool(
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
        result = summary.structuredContent["results"][0]["result"]
        assert result["data"]["sum"] == 3
        assert result["kind"] == "calculated"
        assert result["provenance"][0]["input_kind"] == "metered"
        invalid = await client.call_tool("ENERGY_GET_TOOL", {"name": "NOT_REAL"})
        assert invalid.structuredContent["error"]["code"] == "tool_not_found"


async def test_mcp_hooks_and_provider_helper_schemas(tmp_path):
    registry = Registry()
    register(registry)
    register_csv(registry, tmp_path)
    (tmp_path / "data.csv").write_text("kwh\n2\n")
    agent = EnergyAgent(registry, tmp_path / "state")
    session = agent.session("u")
    seen = []
    agent.before.append(lambda tool, args, sess: (seen.append(tool.name), args)[1])
    server = create_server(agent, session)
    schemas = await provider_tools(server, "anthropic")
    assert len(schemas) == 7
    assert all("input_schema" in t for t in schemas)
    async with create_connected_server_and_client_session(server) as client:
        response = await client.call_tool(
            "ENERGY_MULTI_EXECUTE_TOOL",
            {
                "calls": [
                    {
                        "tool": "CSV_READ_TIMESERIES",
                        "arguments": {
                            "file": "data.csv",
                            "kind": "metered",
                            "unit": "kWh",
                            "timezone": "UTC",
                        },
                    }
                ]
            },
        )
        assert response.structuredContent["results"][0]["ok"]
        assert seen == ["CSV_READ_TIMESERIES"]
