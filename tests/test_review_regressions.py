from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from energy_agent_tools import cli
from energy_agent_tools.app import build_agent, configure_mcp
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.capabilities import (
    CapabilityBinding,
    CapabilityRequest,
    CapabilityResolver,
)
from energy_agent_tools.connectors.mcp_bridge import MCPImportError
from energy_agent_tools.models import (
    Asset,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyResult,
    Site,
    Tool,
    Toolkit,
    schema,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def _meter_agent(root: Path, *, account_site: str = "home") -> EnergyAgent:
    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="meter",
            name="Meter",
            description="Reviewed meter fixture",
            runtime="native",
            status="stable",
            auth_required=True,
        )
    )

    async def read(arguments, _context):
        return EnergyResult(
            data=[{"timestamp": arguments["start"], "value": 2}],
            kind=DataKind.METERED,
            unit="kWh",
            source="fixture",
            resolution="30min",
        )

    registry.add(
        Tool(
            name="meter.read",
            toolkit="meter",
            description="Read a reviewed interval meter",
            capabilities=["get_energy_consumption"],
            input_schema=schema({"start": {"type": "string"}}, ["start"]),
        ),
        read,
    )
    return EnergyAgent(
        registry,
        root,
        accounts=[
            ConnectedAccount(
                id="meter-account",
                user_id="u",
                site_id=account_site,
                toolkit="meter",
            )
        ],
        sites=[
            Site(id="home", user_id="u", name="Home", timezone="UTC"),
            Site(id="foreign", user_id="u", name="Foreign", timezone="UTC"),
        ],
        assets=[Asset(id="main", site_id="home", kind="meter", name="Main meter")],
    )


def _reviewed_binding(**kwargs) -> CapabilityBinding:
    return CapabilityBinding(
        capability="get_energy_consumption",
        tool="meter.read",
        reviewed=True,
        kind=DataKind.METERED,
        unit="kWh",
        resolution="30min",
        **kwargs,
    )


async def test_fixed_arguments_cannot_be_changed_by_before_hooks(tmp_path: Path):
    agent = _meter_agent(tmp_path)
    try:
        binding = _reviewed_binding(fixed_arguments={"selector": "approved"})

        async def read(arguments, _context):
            return EnergyResult(
                data=arguments,
                kind=DataKind.METERED,
                unit="kWh",
                source="fixture",
                resolution="30min",
            )

        agent.registry.tools["meter.read"].input_schema = schema(
            {"start": {"type": "string"}, "selector": {"type": "string"}},
            ["start", "selector"],
        )
        agent.registry.handlers["meter.read"] = read
        agent.resolver = CapabilityResolver(agent, [binding])
        agent.before.append(lambda _tool, arguments, _session: {**arguments, "selector": "changed"})

        result = await agent.resolver.execute(
            agent.session("u", "home"),
            CapabilityRequest(
                capability="get_energy_consumption",
                arguments={"start": "2026-10-01T00:00:00Z"},
            ),
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "binding_arguments_changed"
    finally:
        await agent.close()


@pytest.mark.parametrize(
    ("update", "error_code"),
    [
        ({"site_id": "foreign"}, "result_scope_changed"),
        ({"asset_id": "foreign-asset"}, "result_scope_changed"),
        ({"original_unit": "MWh"}, "result_scope_changed"),
        ({"provenance": [{"provider": "forged"}]}, "provenance_changed"),
    ],
)
async def test_after_hooks_cannot_change_scope_or_source_provenance(
    tmp_path: Path, update: dict, error_code: str
):
    agent = _meter_agent(tmp_path)
    agent.resolver = CapabilityResolver(agent, [_reviewed_binding(account_id="meter-account")])
    try:
        agent.after.append(lambda _tool, result, _session: result.model_copy(update=update))
        result = await agent.resolver.execute(
            agent.session("u", "home"),
            CapabilityRequest(
                capability="get_energy_consumption",
                arguments={"start": "2026-10-01T00:00:00Z"},
                asset_id="main",
            ),
        )

        assert result["ok"] is False
        assert result["error"]["code"] == error_code
        assert agent.workbench.list_artifacts(agent.session("u", "home")) == []
    finally:
        await agent.close()


async def test_asset_bound_binding_does_not_select_a_foreign_site_account(tmp_path: Path):
    agent = _meter_agent(tmp_path, account_site="foreign")
    agent.resolver = CapabilityResolver(agent, [_reviewed_binding(asset_id="main")])
    try:
        resolution = agent.resolver.resolve(
            agent.session("u"),
            CapabilityRequest(
                capability="get_energy_consumption",
                arguments={"start": "2026-10-01T00:00:00Z"},
            ),
        )

        assert resolution["selected"] is None
        assert resolution["status"] == "unavailable"
        assert all(
            candidate["account_id"] != "meter-account" for candidate in resolution["candidates"]
        )
    finally:
        await agent.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defaults", {"api_key": "secret"}),
        ("fixed_arguments", {"refresh_token": "secret"}),
    ],
)
def test_capability_argument_maps_reject_secret_fields(field: str, value: dict):
    with pytest.raises(ValueError, match="credential"):
        CapabilityBinding(
            capability="get_energy_consumption",
            tool="meter.read",
            **{field: value},
        )


async def test_configure_mcp_commits_only_after_all_imports_and_bindings_validate(
    tmp_path: Path,
):
    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    config = {
        "mcp_servers": [
            {
                "toolkit_id": "first",
                "command": sys.executable,
                "args": [str(fixture)],
            },
            {"toolkit_id": "broken", "command": "/definitely/not/a-command"},
        ],
        "bindings": [],
    }
    agent = build_agent(tmp_path, config)
    original_toolkits = set(agent.registry.toolkits)
    original_tools = set(agent.registry.tools)
    try:
        with pytest.raises(MCPImportError, match="MCP initialization or discovery failed"):
            await configure_mcp(agent, config)
        assert set(agent.registry.toolkits) == original_toolkits
        assert set(agent.registry.tools) == original_tools
        assert not any(name.startswith("first.") for name in agent.registry.handlers)
    finally:
        await agent.close()


def test_cli_closes_agent_when_delayed_mcp_configuration_fails(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mcp_servers": [{"toolkit_id": "fixture"}]}))

    class FakeAgent:
        def __init__(self):
            self.closed = 0

        def session(self, user_id, site_id=None):
            return SimpleNamespace(user_id=user_id, site_id=site_id)

        async def close(self):
            self.closed += 1

    fake = FakeAgent()

    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: fake)

    async def fail(_agent, _config):
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(cli, "configure_mcp", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["energy-agent", "catalogue", "--config", str(config_path), "--state-dir", str(tmp_path)],
    )

    with pytest.raises(RuntimeError, match="configuration failed"):
        cli.main()
    assert fake.closed == 1


async def test_redaction_values_are_scoped_to_the_executing_user(tmp_path: Path):
    store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    store.configure(
        ConnectedAccount(
            id="alice-account",
            user_id="alice",
            toolkit="meter",
            auth=AuthConfig(scheme="bearer"),
        ),
        "alice-secret",
    )
    store.configure(
        ConnectedAccount(
            id="bob-account",
            user_id="bob",
            toolkit="meter",
            auth=AuthConfig(scheme="bearer"),
        ),
        "bob-secret",
    )
    registry = Registry()
    registry.add_toolkit(
        Toolkit(id="meter", name="Meter", description="Meter", runtime="native", status="stable")
    )
    agent = EnergyAgent(
        registry,
        tmp_path / "state",
        accounts=store.accounts("alice") + store.accounts("bob"),
        auth_store=store,
    )
    try:
        assert agent._secrets("alice") == ["alice-secret"]
        assert agent._secrets("bob") == ["bob-secret"]
    finally:
        await agent.close()
