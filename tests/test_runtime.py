from __future__ import annotations

import json

import httpx
import pytest

from energy_agent_tools.models import (
    Action,
    AuthConfig,
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


def make_agent(tmp_path, accounts=None, sites=None):
    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="test",
            name="Test",
            description="Test native runtime",
            runtime="native",
            status="stable",
        )
    )

    async def energy(args, ctx):
        return EnergyResult(
            data={"value": args["value"], "credential": ctx.credential},
            kind=DataKind.METERED,
            unit="kWh",
            source="fixture",
        )

    registry.add(
        Tool(
            name="TEST_ENERGY",
            toolkit="test",
            description="Get metered energy consumption",
            input_schema=schema({"value": {"type": "number"}}, ["value"]),
            capabilities=["get_energy_consumption"],
        ),
        energy,
    )
    return EnergyAgent(registry, tmp_path, accounts=accounts, sites=sites)


async def test_search_scope_and_schema_limit(tmp_path):
    agent = make_agent(tmp_path)
    session = agent.session("u")
    assert len(agent.search(session, "electricity used yesterday")) == 1
    assert agent.search(agent.session("u", toolkits=set()), "energy") == []
    assert (await agent.execute(agent.session("u", toolkits=set()), "TEST_ENERGY", {"value": 1}))[
        "error"
    ]["code"] == "tool_forbidden"
    with pytest.raises(EnergyError):
        agent.search(session, "", 1)
    with pytest.raises(EnergyError):
        agent.session("u", toolkits={"typo"})
    await agent.close()


async def test_accounts_never_cross_users_sites_or_ambiguity(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "unmistakably-private-secret")
    sites = [
        Site(id="a", name="A", user_id="u", timezone="Europe/London"),
        Site(id="b", name="B", user_id="other", timezone="UTC"),
    ]
    accounts = [
        ConnectedAccount(
            id="one",
            user_id="u",
            site_id="a",
            toolkit="test",
            auth=AuthConfig(scheme="bearer", credential_env="TEST_SECRET"),
        ),
        ConnectedAccount(id="two", user_id="u", site_id="a", toolkit="test"),
        ConnectedAccount(id="foreign", user_id="other", site_id="b", toolkit="test"),
    ]
    agent = make_agent(tmp_path, accounts, sites)
    session = agent.session("u", "a")
    assert "SECRET" not in json.dumps(agent.connections(session))
    assert (await agent.execute(session, "TEST_ENERGY", {"value": 2}))["error"][
        "code"
    ] == "ambiguous_account"
    assert (await agent.execute(session, "TEST_ENERGY", {"value": 2}, "foreign"))["error"][
        "code"
    ] == "account_forbidden"
    agent.select_account(session, "test", "one")
    output = await agent.execute(session, "TEST_ENERGY", {"value": 2}, persist=True)
    assert "unmistakably-private-secret" not in json.dumps(output)
    stored = agent.workbench.read(session, output["result"]["data"]["artifact_id"])
    assert stored.data["credential"] == "[REDACTED]"
    assert stored.provenance[-1]["account_id"] == "one"
    with pytest.raises(EnergyError):
        agent.session("other", "a")
    await agent.close()


async def test_policy_and_hooks_share_execution_path(tmp_path):
    agent = make_agent(tmp_path)
    session = agent.session("u")
    invoked = []

    async def switch(args, ctx):
        invoked.append(True)
        return EnergyResult(data="done", kind=DataKind.CALCULATED, unit="state", source="fixture")

    agent.registry.add(
        Tool(
            name="CONTROL",
            toolkit="test",
            description="Control",
            input_schema=schema({}),
            capabilities=[],
            actions={Action.CONTROL},
        ),
        switch,
    )
    result = await agent.execute(session, "CONTROL", {})
    assert result["error"]["code"] == "policy_denied"
    assert not invoked
    session.allowed_actions.add(Action.CONTROL)
    assert (await agent.execute(session, "CONTROL", {}))["ok"]
    agent.before.append(lambda tool, args, sess: {"value": "invalid"})
    result = await agent.execute(session, "TEST_ENERGY", {"value": 1})
    assert result["error"]["code"] == "invalid_arguments"
    agent.before.clear()
    agent.after.append(
        lambda tool, result, sess: result.model_copy(update={"warnings": ["hook ran"]})
    )
    result = await agent.execute(session, "TEST_ENERGY", {"value": 1})
    assert result["result"]["warnings"] == ["hook ran"]
    await agent.close()


async def test_provider_failures_sanitized_and_batch_partial_success(tmp_path):
    agent = make_agent(tmp_path)

    async def broken(args, ctx):
        raise RuntimeError("https://provider?api_key=VERY_SECRET")

    agent.registry.add(
        Tool(
            name="BROKEN",
            toolkit="test",
            description="broken",
            capabilities=[],
            input_schema=schema({}),
        ),
        broken,
    )
    outputs = await agent.multi_execute(
        agent.session("u"),
        [
            {"tool": "BROKEN"},
            {"tool": "TEST_ENERGY", "arguments": {"value": 3}},
            {"tool": "TEST_ENERGY", "arguments": {"value": 3, "api_key": "secret"}},
        ],
    )
    assert [o["ok"] for o in outputs] == [False, True, False]
    assert "VERY_SECRET" not in json.dumps(outputs)
    assert "api_key" not in outputs[2]["error"]["message"]
    await agent.close()


async def test_http_error_classification(tmp_path):
    agent = make_agent(tmp_path)

    async def fail(args, ctx):
        request = httpx.Request("GET", "https://example.test?secret=private")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("private", request=request, response=response)

    agent.registry.add(
        Tool(
            name="FAIL",
            toolkit="test",
            description="fail",
            capabilities=[],
            input_schema=schema({}),
        ),
        fail,
    )
    out = await agent.execute(agent.session("u"), "FAIL", {})
    assert out["error"]["retryable"]
    assert "private" not in json.dumps(out)
    await agent.close()
