from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import types

from energy_agent_tools.connectors import mcp_bridge
from energy_agent_tools.connectors.mcp_bridge import (
    import_mcp,
    inspect_mcp,
    inspect_mcp_manifest,
    mcp_schema_digest,
)
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    EnergyError,
    ExecutionContext,
    Session,
)
from energy_agent_tools.registry import Registry

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


def _context(
    *,
    allowed_actions: set[Action] | None = None,
    credential: str | None = None,
    credential_env: str | None = None,
) -> ExecutionContext:
    account = None
    if credential_env:
        account = ConnectedAccount(
            id="fixture-account",
            user_id="user",
            toolkit="fixture",
            auth=AuthConfig(scheme="mcp", credential_env=credential_env),
        )
    return ExecutionContext(
        session=Session(
            user_id="user",
            allowed_actions=allowed_actions
            or {
                Action.READ,
                Action.CALCULATE,
                Action.SIMULATE,
                Action.EXTERNAL,
            },
        ),
        account=account,
        credential=credential,
        http=None,  # type: ignore[arg-type]
        workbench=None,
    )


@pytest.mark.asyncio
async def test_stdio_import_initializes_discovers_calls_and_namespaces_tools() -> None:
    registry = Registry()
    toolkit = await import_mcp(
        registry,
        "fixture",
        command=sys.executable,
        args=[str(FIXTURE)],
        tool_metadata={
            "energy_echo": {
                "action": "read-only",
                "capabilities": ["site-consumption"],
                "unit": "kWh",
                "kind": "metered",
            }
        },
    )

    assert toolkit.runtime == "mcp-local"
    assert "fixture.energy_echo" in registry.tools
    assert registry.tools["fixture.energy_echo"].actions == {Action.READ}
    # The schema comes from MCP and remains available only for the discovered tool.
    assert "value" in registry.tools["fixture.energy_echo"].input_schema["properties"]

    result = await registry.handlers["fixture.energy_echo"]({"value": 3}, _context())
    assert result.data["value"] == 3
    assert result.kind.value == "metered"
    assert result.unit == "kWh"
    assert result.provenance == [{"toolkit": "fixture", "tool": "energy_echo"}]
    assert any("unverified" in warning.lower() for warning in result.warnings)
    assert "token=%5BREDACTED%5D" in result.data["url"]


@pytest.mark.asyncio
async def test_mcp_annotations_do_not_grant_action_and_unreviewed_is_blocked() -> None:
    registry = Registry()
    await import_mcp(registry, "fixture", command=sys.executable, args=[str(FIXTURE)])

    # The fixture advertises readOnlyHint=True, but no operator metadata means
    # configuration-write and the default session rejects it.
    assert registry.tools["fixture.energy_echo"].actions == {Action.WRITE}
    with pytest.raises(Exception) as error:
        await registry.handlers["fixture.energy_echo"]({"value": "blocked"}, _context())
    assert getattr(error.value, "code", None) == "action_not_allowed"

    result = await registry.handlers["fixture.energy_echo"](
        {"value": "reviewed at runtime"},
        _context(allowed_actions={Action.WRITE}),
    )
    assert result.kind.value == "estimated"
    assert any("marked estimated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_mcp_credential_is_server_side_and_not_returned() -> None:
    registry = Registry()
    await import_mcp(
        registry,
        "fixture",
        command=sys.executable,
        args=[str(FIXTURE)],
        credential_env="MCP_TEST_SECRET",
        tool_metadata={"energy_echo": {"action": "read-only"}},
    )

    result = await registry.handlers["fixture.energy_echo"](
        {"value": "credential"},
        _context(credential="super-secret-value", credential_env="MCP_TEST_SECRET"),
    )
    serialized = repr(result.model_dump())
    assert "super-secret-value" not in serialized
    assert "https://" not in repr(result.provenance)
    assert result.data["credential_echo"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_mcp_server_error_is_structured() -> None:
    registry = Registry()
    await import_mcp(
        registry,
        "fixture",
        command=sys.executable,
        args=[str(FIXTURE)],
        tool_metadata={"energy_error": {"action": "read-only"}},
    )

    with pytest.raises(Exception) as error:
        await registry.handlers["fixture.energy_error"]({"message": "bad input"}, _context())
    assert getattr(error.value, "code", None) == "mcp_tool_error"


async def test_invalid_import_is_atomic():
    registry = Registry()
    with pytest.raises(EnergyError):
        await import_mcp(
            registry,
            "atomic",
            command=sys.executable,
            args=[str(FIXTURE)],
            tool_metadata={"energy_sum": {"actions": ["not-an-action"]}},
        )
    assert "atomic" not in registry.toolkits
    assert not any(t.toolkit == "atomic" for t in registry.tools.values())
    assert not any(n.startswith("atomic.") for n in registry.handlers)


@pytest.mark.asyncio
async def test_mcp_review_version_and_schema_manifest_are_explicit() -> None:
    registry = Registry()
    await import_mcp(
        registry,
        "versioned",
        command=sys.executable,
        args=[str(FIXTURE)],
        version="2026.10",
        tool_metadata={
            "energy_sum": {
                "reviewed": True,
                "action": "calculation",
                "kind": "calculated",
                "unit": "kWh",
            }
        },
    )
    imported = registry.tools["versioned.energy_sum"]
    assert imported.version == "2026.10"
    assert imported.reviewed is True
    assert imported.result_kind.value == "calculated"
    assert imported.result_unit == "kWh"

    # Supplying a kind without the explicit review contract retains the safe
    # result metadata but does not claim that the upstream tool was reviewed.
    await import_mcp(
        registry,
        "unreviewed",
        command=sys.executable,
        args=[str(FIXTURE)],
        tool_metadata={
            "energy_sum": {"action": "calculation", "kind": "calculated", "unit": "kWh"}
        },
    )
    unreviewed = registry.tools["unreviewed.energy_sum"]
    assert unreviewed.reviewed is False
    assert unreviewed.result_kind is None
    assert unreviewed.result_unit is None

    schemas = {
        "zeta": {"type": "object", "properties": {"value": {"type": "number"}}},
        "alpha": {"type": "object", "properties": {"name": {"type": "string"}}},
    }
    manifest = inspect_mcp_manifest(
        schemas,
        toolkit_id="safe",
        version="2.0.0",
        tool_metadata={
            "alpha": {
                "reviewed": True,
                "action": "read-only",
                "kind": "metered",
                "unit": "kWh",
            }
        },
    )
    assert manifest["version"] == "2.0.0"
    assert manifest["upstream_names"] == ["alpha", "zeta"]
    assert manifest["schema_digest"] == mcp_schema_digest(schemas)
    assert manifest["digest"] == manifest["schema_digest"]
    assert manifest["annotations_untrusted"] is True
    assert manifest["tools"][0]["reviewed"] is True
    assert "inputSchema" not in repr(manifest)

    secret = "manifest-private-token"
    redacted = inspect_mcp_manifest(
        {secret: {"type": "object"}},
        toolkit_id="safe",
        tool_metadata={
            secret: {
                "reviewed": True,
                "action": "read-only",
                "kind": "metered",
                "unit": secret,
            }
        },
        secrets=[secret],
    )
    assert secret not in repr(redacted)
    assert "[REDACTED]" in repr(redacted)


@pytest.mark.asyncio
async def test_mcp_inspection_digest_rejects_schema_drift_before_registry_commit() -> None:
    manifest = await inspect_mcp(
        "fixture",
        command=sys.executable,
        args=[str(FIXTURE)],
        version="1.2.3",
    )
    assert manifest["schema_digest"]
    assert len(manifest["schema_digest"]) == 64

    registry = Registry()
    with pytest.raises(EnergyError) as error:
        await import_mcp(
            registry,
            "drifted",
            command=sys.executable,
            args=[str(FIXTURE)],
            expected_schema_digest="0" * 64,
        )
    assert error.value.code == "mcp_schema_drift"
    assert registry.toolkits == {}
    assert registry.tools == {}
    assert registry.handlers == {}


@pytest.mark.asyncio
async def test_mcp_runtime_schema_drift_is_rejected_before_call(monkeypatch) -> None:
    registry = Registry()
    await import_mcp(
        registry,
        "runtime-drift",
        command=sys.executable,
        args=[str(FIXTURE)],
        tool_metadata={"energy_sum": {"action": "calculation"}},
    )
    called = False

    @asynccontextmanager
    async def changed_server(*_args, **_kwargs):
        class ChangedSession:
            async def list_tools(self, cursor=None):
                assert cursor is None
                return types.ListToolsResult(
                    tools=[
                        types.Tool(
                            name="energy_sum",
                            description="same name, incompatible schema",
                            inputSchema={
                                "type": "object",
                                "properties": {"left": {"type": "string"}},
                                "required": ["left"],
                                "additionalProperties": False,
                            },
                        )
                    ]
                )

            async def call_tool(self, *_args, **_kwargs):
                nonlocal called
                called = True
                return types.CallToolResult(content=[])

        yield ChangedSession()

    monkeypatch.setattr(mcp_bridge, "_client_session", changed_server)
    with pytest.raises(EnergyError) as error:
        await registry.handlers["runtime-drift.energy_sum"]({"left": 2, "right": 3}, _context())
    assert error.value.code == "mcp_schema_drift"
    assert called is False
