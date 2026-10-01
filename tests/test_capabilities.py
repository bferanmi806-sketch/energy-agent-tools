from datetime import UTC, datetime

import pytest

from energy_agent_tools.capabilities import CapabilityBinding, CapabilityRequest
from energy_agent_tools.models import (
    Asset,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    Site,
    Tool,
    Toolkit,
    schema,
)
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def platform(tmp_path, bindings=None):
    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="meter",
            name="Meter",
            description="Interval meter",
            runtime="native",
            status="stable",
        )
    )

    async def read(args, context):
        return EnergyResult(
            data=[{"timestamp": args["start"], "value": 2}],
            kind=DataKind.METERED,
            unit="kWh",
            source="fixture",
            resolution="30min",
        )

    registry.add(
        Tool(
            name="meter.read",
            toolkit="meter",
            description="Electricity consumption meter",
            capabilities=["get_energy_consumption"],
            input_schema=schema({"start": {"type": "string"}}, ["start"]),
        ),
        read,
    )
    accounts = [
        ConnectedAccount(id="a", user_id="u", site_id="home", toolkit="meter"),
        ConnectedAccount(id="b", user_id="u", site_id="home", toolkit="meter"),
        ConnectedAccount(id="other", user_id="other", site_id="foreign", toolkit="meter"),
    ]
    return EnergyAgent(
        registry,
        tmp_path,
        accounts=accounts,
        sites=[
            Site(id="home", user_id="u", name="Home", timezone="Europe/London"),
            Site(id="foreign", user_id="other", name="Other", timezone="UTC"),
        ],
        assets=[
            Asset(
                id="main", site_id="home", kind="industrial-meter", name="Main", account_ids=["a"]
            )
        ],
        bindings=bindings,
    )


def binding(**kwargs):
    return CapabilityBinding(
        capability="get_energy_consumption",
        tool="meter.read",
        reviewed=True,
        kind=DataKind.METERED,
        unit="kWh",
        resolution="30min",
        **kwargs,
    )


async def test_resolution_ambiguity_asset_and_execute(tmp_path):
    agent = platform(tmp_path, [binding()])
    session = agent.session("u", "home")
    request = CapabilityRequest(
        capability="get_energy_consumption",
        arguments={"start": "2026-10-01T00:00:00Z"},
        kind=DataKind.METERED,
    )
    resolved = agent.resolver.resolve(session, request)
    assert resolved["status"] == "ambiguous"
    assert {c["account_id"] for c in resolved["candidates"]} == {"a", "b"}
    assert not (await agent.resolver.execute(session, request))["ok"]
    request.asset_id = "main"
    assert agent.resolver.resolve(session, request)["selected"]["account_id"] == "a"
    result = await agent.resolver.execute(session, request)
    assert result["ok"] and result["result"]["kind"] == "metered"
    assert result["result"]["site_id"] == "home"
    assert result["result"]["provider"] == "meter"
    await agent.close()


async def test_unreviewed_kind_coverage_and_schema_never_substitute(tmp_path):
    agent = platform(tmp_path)
    session = agent.session("u", "home")
    assert (
        agent.resolver.resolve(session, CapabilityRequest(capability="get_energy_consumption"))[
            "status"
        ]
        == "unavailable"
    )
    agent.resolver.bindings = [
        binding(account_id="a", coverage_start=datetime(2026, 10, 1, tzinfo=UTC))
    ]
    result = agent.resolver.resolve(
        session,
        CapabilityRequest(
            capability="get_energy_consumption",
            arguments={"start": "2026-09-01T00:00:00Z"},
            kind=DataKind.FORECAST,
        ),
    )
    reasons = result["candidates"][0]["reasons"]
    assert "measurement_kind_incompatible" in reasons and "outside_time_coverage" in reasons
    with pytest.raises(EnergyError, match="scope"):
        agent.resolver.resolve(
            session, CapabilityRequest(capability="get_energy_consumption", asset_id="foreign")
        )
    malformed = await agent.multi_execute(session, [{"tool": "meter.read", "arguments": {}}])
    assert malformed[0]["error"]["code"] == "invalid_arguments"
    with pytest.raises(EnergyError):
        await agent.multi_execute(session, [{"tool": "meter.read", "unexpected": True}])
    await agent.close()


async def test_mapping_preferences_pins_and_metadata(tmp_path):
    agent = platform(
        tmp_path,
        [
            binding(account_id="a", defaults={"start": "2026-10-01T00:00:00Z"}, preference=5),
            binding(account_id="b", preference=0),
        ],
    )
    session = agent.session("u", "home")
    assert (
        agent.resolver.resolve(session, CapabilityRequest(capability="get_energy_consumption"))[
            "selected"
        ]["account_id"]
        == "a"
    )
    session.account_ids["meter"] = "missing"
    assert (
        agent.resolver.resolve(session, CapabilityRequest(capability="get_energy_consumption"))[
            "selected"
        ]
        is None
    )
    assert all(
        c["account_id"] is None
        for c in agent.resolver.resolve(
            session, CapabilityRequest(capability="get_energy_consumption")
        )["candidates"]
    )
    await agent.close()


def test_binding_rejects_naive_and_reversed_coverage():
    with pytest.raises(ValueError, match="offset-aware"):
        binding(coverage_start=datetime(2026, 10, 1))
    with pytest.raises(ValueError, match="precedes"):
        binding(
            coverage_start=datetime(2026, 10, 2, tzinfo=UTC),
            coverage_end=datetime(2026, 10, 1, tzinfo=UTC),
        )


def test_binding_account_must_belong_to_asset(tmp_path):
    with pytest.raises(ValueError, match="bound asset"):
        platform(tmp_path, [binding(account_id="other", asset_id="main")])


async def test_multiple_provider_mapping_and_fixed_semantics(tmp_path):
    from energy_agent_tools.capabilities import CapabilityResolver

    agent = platform(tmp_path, [binding(account_id="a")])
    agent.registry.add_toolkit(
        Toolkit(
            id="alternative",
            name="Alternative",
            description="Other meter protocol",
            runtime="native",
            status="stable",
        )
    )

    async def alternate(args, context):
        return EnergyResult(
            data=[{"timestamp": args["from_time"], "value": 4}],
            kind=DataKind.METERED,
            unit="kWh",
            resolution="1800s",
            source="alternative-fixture",
        )

    agent.registry.add(
        Tool(
            name="alternative.read",
            toolkit="alternative",
            description="Alternative interval source",
            capabilities=["get_energy_consumption"],
            input_schema=schema(
                {
                    "from_time": {"type": "string"},
                    "quantity": {"type": "string", "enum": ["energy", "power"]},
                },
                ["from_time", "quantity"],
            ),
        ),
        alternate,
    )
    other = CapabilityBinding(
        capability="get_energy_consumption",
        tool="alternative.read",
        kind=DataKind.METERED,
        unit="kWh",
        resolution="30min",
        argument_map={"start": "from_time"},
        fixed_arguments={"quantity": "energy"},
        reviewed=True,
    )
    agent.resolver = CapabilityResolver(agent, [binding(account_id="a"), other])
    session = agent.session("u", "home")
    request = CapabilityRequest(
        capability="get_energy_consumption", arguments={"start": "2026-10-01T00:00:00Z"}
    )
    assert agent.resolver.resolve(session, request)["status"] == "ambiguous"
    request.tool = "alternative.read"
    result = await agent.resolver.execute(session, request)
    assert result["ok"] and result["result"]["source"] == "alternative-fixture"
    request.arguments["quantity"] = "power"
    assert (
        "arguments_incompatible_with_capability"
        in agent.resolver.resolve(session, request)["candidates"][0]["reasons"]
    )
    assert not (await agent.resolver.execute(session, request))["ok"]
    await agent.close()
