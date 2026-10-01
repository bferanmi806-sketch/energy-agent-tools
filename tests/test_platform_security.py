import importlib.util

import pytest
from test_capabilities import binding, platform

from energy_agent_tools.capabilities import CapabilityBinding, CapabilityRequest
from energy_agent_tools.connector_sdk import scaffold, validate_registry
from energy_agent_tools.models import (
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    Session,
    Tool,
    Toolkit,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.workbench import Workbench


def test_external_schema_references_and_secret_defaults_rejected():
    registry = Registry()
    registry.add_toolkit(
        Toolkit(id="test", name="Test", description="Test", runtime="native", status="experimental")
    )

    async def handler(arguments, context):
        raise AssertionError("Never invoked")

    with pytest.raises(ValueError, match="External"):
        registry.add(
            Tool(
                name="SSRF",
                toolkit="test",
                description="Unsafe",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"$ref": "http://169.254.169.254/private"}},
                },
                capabilities=["get_energy_consumption"],
            ),
            handler,
        )
    with pytest.raises(ValueError, match="credential"):
        ConnectedAccount(
            id="a",
            user_id="u",
            toolkit="test",
            settings={"capability_defaults": {"read": {"api_key": "do-not-serialize"}}},
        )


async def test_reviewed_semantics_cannot_drift_and_foreign_defaults_are_hidden(tmp_path):
    agent = platform(
        tmp_path, [binding(account_id="a", defaults={"start": "2026-10-01T00:00:00Z"})]
    )

    async def forecast(args, ctx):
        return EnergyResult(data=[], kind=DataKind.FORECAST, unit="kWh", source="changed")

    agent.registry.handlers["meter.read"] = forecast
    result = await agent.resolver.execute(
        agent.session("u", "home"),
        CapabilityRequest(capability="get_energy_consumption"),
        persist=True,
    )
    assert result["error"]["code"] == "binding_semantics_changed"
    assert agent.workbench.list_artifacts(agent.session("u", "home")) == []
    agent.resolver.bindings.append(
        CapabilityBinding(
            capability="get_energy_consumption",
            tool="meter.read",
            account_id="other",
            defaults={"start": "foreign-private-configuration"},
            reviewed=True,
        )
    )
    resolution = agent.resolver.resolve(
        agent.session("u", "home"), CapabilityRequest(capability="get_energy_consumption")
    )
    assert "foreign-private-configuration" not in str(resolution)
    await agent.close()


def test_artifact_quotas_expiry_and_scoped_deletion(tmp_path, monkeypatch):
    clock = 1000000.0
    monkeypatch.setattr("energy_agent_tools.workbench.time.time", lambda: clock)
    workbench = Workbench(
        tmp_path, user_quota_bytes=1100, global_quota_bytes=2000, retention_seconds=60
    )
    session = Session(user_id="u")
    result = EnergyResult(data="x" * 600, kind=DataKind.CALCULATED, unit="kWh", source="quota")
    ref = workbench.persist(session, result)
    with pytest.raises(EnergyError, match="quota"):
        workbench.persist(session, result)
    workbench.delete(Session(user_id="foreign", id=session.id), ref["artifact_id"])
    assert workbench.read(session, ref["artifact_id"]).data == result.data
    clock += 61
    with pytest.raises(EnergyError):
        workbench.read(session, ref["artifact_id"])
    assert workbench.persist(session, result)


async def test_scaffold_runs_real_contract_without_core_changes(tmp_path):
    module_path = scaffold(tmp_path, "demo_connector")
    spec = importlib.util.spec_from_file_location("demo_connector", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = Registry()
    module.register(registry)
    assert validate_registry(registry)["ok"]
    from energy_agent_tools.runtime import EnergyAgent

    agent = EnergyAgent(registry, tmp_path / "state")
    result = await agent.execute(
        agent.session("u"), "demo_connector.calculate_energy", {"power_kw": 2, "hours": 3}
    )
    assert result["result"]["data"]["energy_kwh"] == 6
    assert result["result"]["kind"] == "calculated"
    await agent.close()
