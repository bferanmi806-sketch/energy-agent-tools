import json

from energy_agent_tools.connectors.local import register, register_csv
from energy_agent_tools.models import Session
from energy_agent_tools.providers import format_tools
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


async def test_csv_path_and_semantic_declarations(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "energy.csv").write_text("timestamp,kwh\n2026-09-30T00:00:00Z,2\n")
    registry = Registry()
    register_csv(registry, data)
    agent = EnergyAgent(registry, tmp_path / "state")
    sess = Session(user_id="local")
    args = {"file": "energy.csv", "kind": "metered", "unit": "kWh", "timezone": "UTC"}
    result = await agent.execute(sess, "CSV_READ_TIMESERIES", args, persist=True)
    assert result["ok"]
    artifact = result["result"]["data"]["artifact_id"]
    assert agent.workbench.read(sess, artifact).quality == "user-declared"
    args["file"] = "../secret"
    assert (await agent.execute(sess, "CSV_READ_TIMESERIES", args))["error"][
        "code"
    ] == "file_forbidden"
    await agent.close()


def test_provider_preserves_schema_optionality():
    registry = Registry()
    register(registry)
    tools = [t.public() for t in registry.tools.values()]
    original = json.dumps(tools, sort_keys=True)
    for provider in ["openai", "openai-responses", "anthropic"]:
        formatted = format_tools(tools, provider)
        assert len(formatted) == len(tools)
    assert original == json.dumps(tools, sort_keys=True)
    assert format_tools(tools, "openai")[0]["function"]["strict"] is False


def test_provider_aliases_are_valid_and_resolve_back():
    from energy_agent_tools.providers import resolve_provider_name

    tools = [{"name": "energy.tool", "description": "test", "input_schema": {"type": "object"}}]
    assert format_tools(tools, "openai")[0]["function"]["name"] == "energy_tool"
    assert resolve_provider_name(tools, "energy_tool") == "energy.tool"
