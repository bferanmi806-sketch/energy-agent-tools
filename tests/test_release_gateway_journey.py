"""Release acceptance for discovery and composed workflows through MCP."""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from energy_agent_tools.server import create_server
from tests.fixtures.provider_sites import (
    SCENARIOS,
    WINDOW_END,
    WINDOW_START,
    build_provider_fixture,
)


@pytest.mark.parametrize("provider", sorted(SCENARIOS))
async def test_mcp_discovers_and_runs_cost_workflow_with_scoped_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """One MCP journey resolves three provider contracts without changing workflow semantics."""

    monkeypatch.setenv("FIXTURE_OCTOPUS_KEY", "octopus-fixture-secret")
    monkeypatch.setenv("FIXTURE_EMON_KEY", "emon-secret")
    fixture = build_provider_fixture(tmp_path / provider)
    agent = fixture.agent(tmp_path / f"{provider}-state")
    scenario = SCENARIOS[provider]
    try:
        session = agent.session(scenario.user_id, scenario.site_id)
        async with create_connected_server_and_client_session(
            create_server(agent, session)
        ) as client:
            discovery = await client.call_tool(
                "ENERGY_SEARCH_TOOLS", {"query": "electricity consumption", "limit": 10}
            )
            found = discovery.structuredContent
            assert found is not None
            provider_tool = {
                "octopus": "octopus_energy.get_consumption",
                "emon": "openenergymonitor.get_feed",
                "csv": "CSV_READ_TIMESERIES",
            }[provider]
            discovered = next(tool for tool in found["tools"] if tool["name"] == provider_tool)
            assert discovered["connection_available"] is True

            workflows = await client.call_tool(
                "ENERGY_SEARCH_TOOLS", {"query": "electricity cost", "limit": 10}
            )
            assert any(
                skill["id"] == "electricity-cost" for skill in workflows.structuredContent["skills"]
            )

            response = await client.call_tool(
                "ENERGY_RUN_SKILL",
                {
                    "skill_id": "electricity-cost",
                    "parameters": {
                        "start": WINDOW_START.isoformat(),
                        "end": WINDOW_END.isoformat(),
                    },
                },
            )
            workflow = response.structuredContent
            assert workflow is not None and workflow["ok"], workflow
            analysis = workflow["evidence"][-1]["analysis"]["result"]
            assert sum(row["cost"] for row in analysis["data"]) == pytest.approx(
                scenario.expected_cost_gbp
            )
            assert analysis["site_id"] == scenario.site_id
            source_kinds = {
                (entry.get("source"), entry.get("input_kind"))
                for entry in analysis["provenance"]
                if entry.get("input_kind") is not None
            }
            assert (scenario.consumption_source, "metered") in source_kinds
            assert (scenario.tariff_source, "calculated") in source_kinds

        # The public MCP executor applies the same account/site checks before provider I/O.
        bob = agent.session("bob", "bob-site")
        request_count = len(fixture.http.requests)
        async with create_connected_server_and_client_session(create_server(agent, bob)) as client:
            denied = await client.call_tool(
                "ENERGY_MULTI_EXECUTE_TOOL",
                {
                    "calls": [
                        {
                            "tool": "octopus_energy.get_consumption",
                            "account_id": "octopus-alice-main",
                            "arguments": {
                                "start": WINDOW_START.isoformat(),
                                "end": WINDOW_END.isoformat(),
                            },
                        }
                    ]
                },
            )
            result = denied.structuredContent["results"][0]
            assert result["error"]["code"] == "account_forbidden"
            assert len(fixture.http.requests) == request_count
    finally:
        await agent.close()
        await agent.http.aclose()
