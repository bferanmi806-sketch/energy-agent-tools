from __future__ import annotations

import sys
from pathlib import Path

import pytest

from energy_agent_tools.connectors.executable import register_executable
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    ExecutionContext,
    Session,
    Toolkit,
)
from energy_agent_tools.registry import Registry

FIXTURE = Path(__file__).parent / "fixtures" / "json_command.py"


def reviewed_executable(*args, **kwargs):
    return register_executable(*args, actions=[Action.READ], **kwargs)


def _registry() -> Registry:
    registry = Registry()
    registry.add_toolkit(
        Toolkit(
            id="fixture",
            name="Fixture",
            description="Executable fixture",
            runtime="executable",
            status="experimental",
        )
    )
    return registry


def _context(credential: str | None = None) -> ExecutionContext:
    account = None
    if credential is not None:
        account = ConnectedAccount(
            id="fixture-account",
            user_id="user",
            toolkit="fixture",
            auth=AuthConfig(scheme="local", credential_env="FIXTURE_SECRET"),
        )
    return ExecutionContext(
        session=Session(user_id="user"),
        account=account,
        credential=credential,
        http=None,  # type: ignore[arg-type]
        workbench=None,
    )


@pytest.mark.asyncio
async def test_fixed_argv_json_protocol_and_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEAK_ME", "parent-secret")
    registry = _registry()
    reviewed_executable(
        registry,
        "fixture",
        "fixture.result",
        [sys.executable, str(FIXTURE), "result"],
        unit="kWh",
        kind="calculated",
        credential_env="FIXTURE_SECRET",
    )

    result = await registry.handlers["fixture.result"]({"value": 7}, _context("child-secret"))
    assert result.kind.value == "calculated"
    assert result.data["received"] == {"value": 7}
    assert result.data["credential"] == "[REDACTED]"
    assert "child-secret" not in repr(result.model_dump())
    assert "parent-secret" not in repr(result.model_dump())


@pytest.mark.asyncio
async def test_fixed_argv_does_not_interpolate_input() -> None:
    registry = _registry()
    reviewed_executable(
        registry,
        "fixture",
        "fixture.echo",
        [sys.executable, str(FIXTURE), "echo"],
    )
    value = "$(touch /tmp/should-not-run); hello"
    result = await registry.handlers["fixture.echo"]({"value": value}, _context())
    assert result.data == {"value": value}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("fail", "executable_failed"),
        ("invalid", "invalid_executable_output"),
    ],
)
async def test_fixed_argv_failures_are_structured(mode: str, expected_code: str) -> None:
    registry = _registry()
    name = f"fixture.{mode}"
    reviewed_executable(registry, "fixture", name, [sys.executable, str(FIXTURE), mode])
    with pytest.raises(Exception) as error:
        await registry.handlers[name]({}, _context())
    assert getattr(error.value, "code", None) == expected_code


@pytest.mark.asyncio
async def test_fixed_argv_timeout_and_output_bound() -> None:
    registry = _registry()
    reviewed_executable(
        registry,
        "fixture",
        "fixture.sleep",
        [sys.executable, str(FIXTURE), "sleep"],
        timeout=0.1,
    )
    with pytest.raises(Exception) as timeout_error:
        await registry.handlers["fixture.sleep"]({"seconds": 3}, _context())
    assert getattr(timeout_error.value, "code", None) == "executable_timeout"

    reviewed_executable(
        registry,
        "fixture",
        "fixture.huge",
        [sys.executable, str(FIXTURE), "huge"],
        max_output_bytes=256,
    )
    with pytest.raises(Exception) as size_error:
        await registry.handlers["fixture.huge"]({}, _context())
    assert getattr(size_error.value, "code", None) == "executable_output_too_large"


async def test_unreviewed_executable_is_blocked():
    registry = _registry()
    register_executable(
        registry, "fixture", "fixture.blocked", [sys.executable, str(FIXTURE), "echo"]
    )
    assert registry.tools["fixture.blocked"].actions == {Action.WRITE}
    with pytest.raises(Exception) as error:
        await registry.handlers["fixture.blocked"]({}, _context())
    assert getattr(error.value, "code", None) == "action_not_allowed"


async def test_timeout_includes_a_child_that_never_reads_stdin():
    import asyncio

    registry = _registry()
    reviewed_executable(
        registry,
        "fixture",
        "fixture.no-read",
        [sys.executable, str(FIXTURE), "no-read"],
        timeout=0.1,
    )
    with pytest.raises(Exception) as error:
        await asyncio.wait_for(
            registry.handlers["fixture.no-read"]({"payload": "x" * 500000}, _context()), timeout=3
        )
    assert getattr(error.value, "code", None) == "executable_timeout"
