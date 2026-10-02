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


async def test_csv_quantity_declarations_survive_reopen_and_control_sums(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "interval.csv").write_text(
        "timestamp,kwh\n2026-09-30T00:00:00Z,2\n2026-09-30T00:30:00Z,3\n"
    )
    (data / "counter.csv").write_text(
        "timestamp,kwh\n2026-09-30T00:00:00Z,100\n2026-09-30T00:30:00Z,103\n"
    )
    registry = Registry()
    register(registry)
    register_csv(registry, data)
    state = tmp_path / "state"
    agent = EnergyAgent(registry, state)
    session = Session(user_id="local")
    base_args = {"kind": "metered", "unit": "kWh", "timezone": "UTC", "resolution": "30min"}

    interval = await agent.execute(
        session,
        "CSV_READ_TIMESERIES",
        {**base_args, "file": "interval.csv", "quantity_shape": "interval"},
        persist=True,
    )
    counter = await agent.execute(
        session,
        "CSV_READ_TIMESERIES",
        {**base_args, "file": "counter.csv", "quantity_shape": "counter"},
        persist=True,
    )
    assert interval["ok"] and counter["ok"]
    interval_id = interval["result"]["data"]["artifact_id"]
    counter_id = counter["result"]["data"]["artifact_id"]
    await agent.close()

    reopened = EnergyAgent(registry, state)
    try:
        persisted_counter = reopened.workbench.read(session, counter_id)
        assert persisted_counter.quantity_shape == "counter"
        assert persisted_counter.resolution == "30min"
        assert persisted_counter.provenance[0]["declared_quantity_shape"] == "counter"
        assert persisted_counter.provenance[0]["declared_resolution"] == "30min"

        interval_summary = await reopened.execute(
            session,
            "WORKBENCH_SUMMARIZE",
            {"artifact_id": interval_id, "column": "kwh"},
        )
        assert interval_summary["ok"]
        assert interval_summary["result"]["data"]["sum"] == 5
        interval_resample = await reopened.execute(
            session,
            "WORKBENCH_RESAMPLE",
            {
                "artifact_id": interval_id,
                "timestamp": "timestamp",
                "column": "kwh",
                "frequency": "1h",
                "aggregation": "sum",
            },
        )
        assert interval_resample["ok"]
        assert interval_resample["result"]["data"][0]["kwh"] == 5

        counter_summary = await reopened.execute(
            session,
            "WORKBENCH_SUMMARIZE",
            {"artifact_id": counter_id, "column": "kwh"},
        )
        assert counter_summary["ok"]
        assert counter_summary["result"]["data"]["sum"] is None
        counter_resample = await reopened.execute(
            session,
            "WORKBENCH_RESAMPLE",
            {
                "artifact_id": counter_id,
                "timestamp": "timestamp",
                "column": "kwh",
                "frequency": "1h",
                "aggregation": "sum",
            },
        )
        assert counter_resample["error"]["code"] == "quantity_incompatible"
    finally:
        await reopened.close()


async def test_csv_power_cannot_be_summed_and_invalid_shape_fails_at_runtime(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "power.csv").write_text(
        "timestamp,kw\n2026-09-30T00:00:00Z,2\n2026-09-30T00:30:00Z,4\n"
    )
    registry = Registry()
    register(registry)
    register_csv(registry, data)
    agent = EnergyAgent(registry, tmp_path / "state")
    session = Session(user_id="local")
    args = {
        "file": "power.csv",
        "kind": "metered",
        "unit": "kW",
        "timezone": "UTC",
        "quantity_shape": "instantaneous",
        "resolution": "30min",
    }
    try:
        invalid = await agent.execute(
            session,
            "CSV_READ_TIMESERIES",
            {**args, "quantity_shape": "cumulative"},
        )
        assert invalid["error"]["code"] == "invalid_arguments"
        empty_resolution = await agent.execute(
            session,
            "CSV_READ_TIMESERIES",
            {**args, "resolution": "  "},
        )
        assert empty_resolution["error"]["code"] == "invalid_arguments"

        imported = await agent.execute(session, "CSV_READ_TIMESERIES", args, persist=True)
        assert imported["ok"]
        artifact_id = imported["result"]["data"]["artifact_id"]
        summary = await agent.execute(
            session,
            "WORKBENCH_SUMMARIZE",
            {"artifact_id": artifact_id, "column": "kw"},
        )
        assert summary["ok"]
        assert summary["result"]["data"]["sum"] is None
        resample = await agent.execute(
            session,
            "WORKBENCH_RESAMPLE",
            {
                "artifact_id": artifact_id,
                "timestamp": "timestamp",
                "column": "kw",
                "frequency": "1h",
                "aggregation": "sum",
            },
        )
        assert resample["error"]["code"] == "quantity_incompatible"
    finally:
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
