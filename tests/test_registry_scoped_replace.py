from __future__ import annotations

import pytest

from energy_agent_tools.models import Tool, ToolAccountScope, Toolkit
from energy_agent_tools.registry import Registry


async def _first(arguments, context):
    raise AssertionError("Discovery must not execute handlers")


async def _second(arguments, context):
    raise AssertionError("Discovery must not execute handlers")


def _scope(account_id="connection", workspace_id="workspace"):
    return ToolAccountScope(user_id="owner", workspace_id=workspace_id, account_id=account_id)


def _toolkit(toolkit_id="private"):
    return Toolkit(
        id=toolkit_id,
        name=toolkit_id,
        description="Fixture",
        runtime="mcp-remote",
        status="experimental",
        auth_required=True,
    )


def _tool(name="private.read", description="meter", scope=None, schema=None):
    return Tool(
        name=name,
        toolkit=name.split(".")[0],
        resource_scope="account",
        account_scope=scope or _scope(),
        description=description,
        input_schema=schema or {"type": "object"},
        capabilities=[],
    )


def test_replacement_updates_search_and_handler_when_tool_count_does_not_change():
    registry = Registry()
    toolkit = _toolkit()
    first = _tool(description="kumquat")
    registry.replace_account_toolkit(toolkit, {first.name: (first, _first)}, account_scope=_scope())
    assert registry.search("kumquat") == [first]
    second = _tool(description="persimmon")
    registry.replace_account_toolkit(
        toolkit, {second.name: (second, _second)}, account_scope=_scope()
    )
    assert registry.search("kumquat") == []
    assert registry.search("persimmon") == [second]
    assert registry.handlers[second.name] is _second


def test_replacement_removes_stale_tools_without_changing_neighbor_namespace():
    registry = Registry()
    first, stale = _tool(), _tool(name="private.old")
    neighbor = _tool(name="neighbor.read", scope=_scope("neighbor"))
    registry.replace_account_toolkit(
        _toolkit(),
        {first.name: (first, _first), stale.name: (stale, _first)},
        account_scope=_scope(),
    )
    registry.replace_account_toolkit(
        _toolkit("neighbor"), {neighbor.name: (neighbor, _first)}, account_scope=_scope("neighbor")
    )
    replacement = _tool(name="private.new")
    registry.replace_account_toolkit(
        _toolkit(), {replacement.name: (replacement, _second)}, account_scope=_scope()
    )
    assert set(registry.tools) == {replacement.name, neighbor.name}
    assert registry.handlers[neighbor.name] is _first
    registry.remove_account_toolkit("private", account_scope=_scope())
    registry.remove_account_toolkit("private", account_scope=_scope())
    assert set(registry.tools) == {neighbor.name}
    assert set(registry.handlers) == {neighbor.name}
    assert set(registry.toolkits) == {"neighbor"}


@pytest.mark.parametrize("scope", [_scope("other"), _scope(workspace_id="other")])
def test_foreign_replacement_and_removal_leave_registry_unchanged(scope):
    registry = Registry()
    first = _tool()
    registry.replace_account_toolkit(
        _toolkit(), {first.name: (first, _first)}, account_scope=_scope()
    )
    replacement = _tool(scope=scope)
    for operation in [
        lambda: registry.replace_account_toolkit(
            _toolkit(), {replacement.name: (replacement, _second)}, account_scope=scope
        ),
        lambda: registry.remove_account_toolkit("private", account_scope=scope),
    ]:
        with pytest.raises(ValueError):
            operation()
        assert registry.get(first.name) == first
        assert registry.handlers[first.name] is _first


def test_invalid_replacement_is_atomic_and_operator_namespace_is_protected():
    registry = Registry()
    first = _tool()
    registry.replace_account_toolkit(
        _toolkit(), {first.name: (first, _first)}, account_scope=_scope()
    )
    good = _tool(name="private.good")
    invalid = _tool(
        name="private.invalid", schema={"type": "object", "$ref": "https://foreign.example/schema"}
    )
    with pytest.raises(ValueError):
        registry.replace_account_toolkit(
            _toolkit(),
            {good.name: (good, _second), invalid.name: (invalid, _second)},
            account_scope=_scope(),
        )
    assert set(registry.tools) == {first.name}
    assert registry.handlers[first.name] is _first
    registry.add_toolkit(_toolkit("operator"))
    operator = _tool(name="operator.read").model_copy(
        update={"resource_scope": "operator", "account_scope": None}
    )
    registry.add(operator, _first)
    with pytest.raises(ValueError):
        registry.replace_account_toolkit(
            _toolkit("operator"),
            {"operator.read": (_tool(name="operator.read"), _second)},
            account_scope=_scope(),
        )
    with pytest.raises(ValueError):
        registry.remove_account_toolkit("operator", account_scope=_scope())
    assert registry.handlers[operator.name] is _first
