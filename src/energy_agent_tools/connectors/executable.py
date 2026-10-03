"""Safe adapter for fixed-argv JSON-line executables.

The adapter is intentionally small and boring: an operator supplies the
complete executable argument vector, a JSON object is written to stdin, and a
single JSON document is read from stdout.  User input is never interpolated
into a shell command.  The child receives a restricted environment, output is
bounded, and timeout/non-zero/invalid-output cases are surfaced as structured
``EnergyError`` values.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
)
from ..registry import Registry

__all__ = [
    "ExecutableAdapter",
    "register_executable",
    "register_executable_tool",
]


_SAFE_INHERITED_ENV = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
}
_SENSITIVE_TEXT_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|credential|password|passwd|secret|token)",
    re.I,
)


def _as_action(value: Action | str) -> Action:
    if isinstance(value, Action):
        return value
    aliases = {
        "read": Action.READ,
        "read-only": Action.READ,
        "readonly": Action.READ,
        "calculate": Action.CALCULATE,
        "calculation": Action.CALCULATE,
        "simulate": Action.SIMULATE,
        "simulation": Action.SIMULATE,
        "external": Action.EXTERNAL,
        "external-data": Action.EXTERNAL,
        "write": Action.WRITE,
        "configuration-write": Action.WRITE,
        "control": Action.CONTROL,
        "physical-control": Action.CONTROL,
        "critical": Action.CRITICAL,
        "safety-critical": Action.CRITICAL,
    }
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown executable action: {value!r}") from exc


def _as_kind(value: DataKind | str) -> DataKind:
    if isinstance(value, DataKind):
        return value
    try:
        return DataKind(value.strip().lower().replace("_", "-"))
    except ValueError as exc:
        raise ValueError(f"Unknown executable data kind: {value!r}") from exc


def _safe_text(value: str, secrets: Sequence[str] = ()) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    # Do not hide every useful diagnostic merely because it contains a word
    # such as "token".  Redact obvious key=value values instead.
    result = re.sub(
        r"(?i)(access[_-]?token|api[_-]?key|authorization|credential|password|passwd|secret|token)"
        r"\s*[:=]\s*([^\s,;&]+)",
        r"\1=[REDACTED]",
        result,
    )
    # A provider may return a URL carrying a token even when the token was not
    # the local account credential.  Strip userinfo and sensitive query values
    # before the result reaches an agent or provenance store.
    if "://" in result:
        try:
            split = urlsplit(result)
            if split.scheme and split.netloc:
                host = split.hostname or ""
                port = f":{split.port}" if split.port is not None else ""
                query = [
                    (
                        key,
                        "[REDACTED]"
                        if re.search(
                            r"(?:token|secret|password|api[_-]?key|authorization|credential)",
                            key,
                            re.I,
                        )
                        else val,
                    )
                    for key, val in parse_qsl(split.query, keep_blank_values=True)
                ]
                result = urlunsplit(
                    (split.scheme, f"{host}{port}", split.path, urlencode(query), "")
                )
        except ValueError:
            result = "[redacted-url]"
    return result


def _safe_environment(
    explicit: Mapping[str, str] | None,
    context: ExecutionContext | None,
    credential_env: str | None,
) -> dict[str, str]:
    """Construct a restricted child environment.

    Only a platform-neutral allowlist is inherited.  Explicit operator values
    are added intentionally, and credentials are added for this execution only
    when an auth configuration names their environment variable.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_INHERITED_ENV or key.startswith("LC_")
    }
    if explicit:
        environment.update({str(key): str(value) for key, value in explicit.items()})
    if context is not None and context.credential and credential_env:
        environment[credential_env] = context.credential
    return environment


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI only
            process.terminate()
        else:
            # The process is created in its own session when possible; signal
            # the group so a child cannot survive the adapter timeout.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except (TimeoutError, ProcessLookupError):
        try:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
            else:  # pragma: no cover - Windows CI only
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > limit:
            # Keep at most the configured cap.  The caller will terminate the
            # process and discard the rest of the stream.
            chunks.append(chunk[: max(0, limit - (total - len(chunk)))])
            return b"".join(chunks), True
        chunks.append(chunk)


async def _run_fixed_argv(
    argv: tuple[str, ...],
    payload: bytes,
    *,
    timeout: float,
    max_output_bytes: int,
    environment: Mapping[str, str],
    cwd: str | Path | None,
) -> tuple[int, bytes, bytes, bool, bool]:
    """Run one process and return ``(code, stdout, stderr, timed_out, overflow)``."""

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environment),
            cwd=os.fspath(cwd) if cwd is not None else None,
            start_new_session=(os.name != "nt"),
        )
    except (OSError, ValueError) as exc:
        raise EnergyError("executable_start_failed", f"Could not start executable: {exc}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    async def write_input() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def read_output(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        data, overflow = await _read_bounded(stream, max_output_bytes)
        if overflow and process.returncode is None:
            await _terminate_process(process)
        return data, overflow

    writer_task = asyncio.create_task(write_input())
    stdout_task = asyncio.create_task(read_output(process.stdout))
    stderr_task = asyncio.create_task(read_output(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    tasks = (writer_task, stdout_task, stderr_task, wait_task)
    try:
        try:
            (
                _,
                (stdout, stdout_overflow),
                (stderr, stderr_overflow),
                returncode,
            ) = await asyncio.wait_for(
                asyncio.gather(writer_task, stdout_task, stderr_task, wait_task), timeout=timeout
            )
            return returncode, stdout, stderr, False, stdout_overflow or stderr_overflow
        except TimeoutError:
            await _terminate_process(process)
            return -signal.SIGKILL, b"", b"", True, False
    finally:
        if process.returncode is None:
            await _terminate_process(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ExecutableAdapter:
    """Register one fixed command and expose it as a registry handler."""

    def __init__(
        self,
        registry: Registry,
        toolkit_id: str,
        name: str,
        argv: Sequence[str],
        *,
        description: str | None = None,
        input_schema: Json | None = None,
        capabilities: Sequence[str] = (),
        actions: Sequence[Action | str] | Action | str = (Action.WRITE,),
        idempotent: bool = True,
        unit: str = "unknown",
        kind: DataKind | str = DataKind.ESTIMATED,
        source: str | None = None,
        timezone: str = "UTC",
        resolution: str | None = None,
        assumptions: Sequence[str] = (),
        quality: str = "unknown",
        timeout: float = 30.0,
        max_output_bytes: int = 1_048_576,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        credential_env: str | None = None,
    ) -> None:
        if not toolkit_id or not name:
            raise ValueError("toolkit_id and name are required")
        if isinstance(argv, (str, bytes)):
            command: tuple[str, ...] = (os.fsdecode(argv),)
        else:
            command = tuple(os.fspath(item) for item in argv)
        if not command or not command[0]:
            raise ValueError("argv must contain an executable")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if isinstance(actions, (Action, str)):
            action_set = frozenset({_as_action(actions)})
        else:
            action_set = frozenset(_as_action(action) for action in actions)
        if not action_set:
            raise ValueError("At least one action is required")

        self.registry = registry
        self.toolkit_id = toolkit_id
        self.name = name
        self.argv = command
        self.description = description or f"Execute {command[0]} with JSON input"
        self.input_schema = input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": True,
        }
        self.capabilities = tuple(str(capability) for capability in capabilities)
        self.actions = action_set
        self.idempotent = idempotent
        self.unit = unit
        self.kind = _as_kind(kind)
        self.source = source or f"executable:{toolkit_id}"
        self.timezone = timezone
        self.resolution = resolution
        self.assumptions = tuple(str(value) for value in assumptions)
        self.quality = quality
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.env = dict(env or {})
        self.cwd = cwd
        self.credential_env = credential_env

        self.tool = Tool(
            name=name,
            toolkit=toolkit_id,
            description=self.description,
            input_schema=self.input_schema,
            capabilities=list(self.capabilities),
            actions=set(self.actions),
            idempotent=idempotent,
            resource_scope="operator",
        )
        registry.add(self.tool, self._execute)

    async def _execute(self, arguments: Json, context: ExecutionContext) -> EnergyResult:
        if not self.actions.issubset(context.session.allowed_actions):
            allowed = ", ".join(sorted(action.value for action in context.session.allowed_actions))
            required = ", ".join(sorted(action.value for action in self.actions))
            raise EnergyError(
                "action_not_allowed",
                f"Executable tool {self.name!r} requires [{required}]; session allows [{allowed}].",
            )

        try:
            payload = (
                json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":"), default=str
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise EnergyError(
                "invalid_input", f"Executable input is not JSON serializable: {exc}"
            ) from exc

        if len(payload) > self.max_output_bytes:
            raise EnergyError(
                "executable_input_too_large", "Executable JSON input exceeds the output bound."
            )

        return_code, stdout, stderr, timed_out, overflow = await _run_fixed_argv(
            self.argv,
            payload,
            timeout=self.timeout,
            max_output_bytes=self.max_output_bytes,
            environment=_safe_environment(self.env, context, self.credential_env),
            cwd=self.cwd,
        )
        secrets = (context.credential,) if context.credential else ()
        if timed_out:
            raise EnergyError(
                "executable_timeout",
                f"Executable {self.argv[0]!r} exceeded {self.timeout:g}s.",
                retryable=True,
            )
        if overflow:
            raise EnergyError(
                "executable_output_too_large",
                f"Executable output exceeded {self.max_output_bytes} bytes.",
            )
        if return_code != 0:
            detail = _safe_text(stderr.decode("utf-8", errors="replace")[:2000], secrets).strip()
            suffix = f": {detail}" if detail else ""
            raise EnergyError(
                "executable_failed",
                f"Executable exited with status {return_code}{suffix}",
            )
        try:
            output = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnergyError(
                "invalid_executable_output",
                f"Executable did not return one valid JSON document: {_safe_text(str(exc), secrets)}",
            ) from exc

        result: dict[str, Any]
        if isinstance(output, Mapping) and {"data", "unit", "source"}.issubset(output):
            result = {str(key): value for key, value in output.items()}
            result.setdefault("kind", self.kind)
            result.setdefault("unit", self.unit)
            result.setdefault("source", self.source)
        else:
            result = {
                "data": output,
                "kind": self.kind,
                "unit": self.unit,
                "source": self.source,
            }
        result.setdefault("timezone", self.timezone)
        if self.resolution is not None:
            result.setdefault("resolution", self.resolution)
        result.setdefault("assumptions", list(self.assumptions))
        warnings = list(result.get("warnings", []))
        warnings.append("Executable output is unverified by Energy Agent Tools.")
        result["warnings"] = warnings
        result.setdefault("quality", self.quality)
        result.setdefault("provenance", [{"toolkit": self.toolkit_id, "tool": self.name}])
        # Credential values must not re-enter the agent context even if a
        # misbehaving executable echoes its environment.
        configured_secrets = tuple(
            str(value) for key, value in self.env.items() if _SENSITIVE_TEXT_RE.search(str(key))
        )
        result = _redact_json(result, (*secrets, *configured_secrets))
        try:
            return EnergyResult.model_validate(result)
        except Exception as exc:
            raise EnergyError(
                "invalid_energy_result",
                f"Executable JSON was not a valid EnergyResult: {_safe_text(str(exc), secrets)[:2000]}",
            ) from exc


def _redact_json(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _safe_text(value, secrets)
    if isinstance(value, Mapping):
        return {str(key): _redact_json(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json(item, secrets) for item in value]
    return value


def register_executable(
    registry: Registry,
    toolkit_id: str,
    name: str,
    argv: Sequence[str] | str | None = None,
    **kwargs: Any,
) -> Tool:
    """Register a fixed executable and return its public :class:`Tool` model.

    ``argv`` is the preferred form.  For configuration loaders that naturally
    split a command and its arguments, ``command=...`` and ``args=[...]`` are
    accepted as keyword aliases and are combined once at registration time.
    """

    command = kwargs.pop("command", None)
    args = kwargs.pop("args", ())
    if argv is not None and command is not None:
        raise TypeError("Provide argv or command/args, not both")
    if argv is None:
        if command is None:
            raise TypeError("An executable argv or command is required")
        argv = [str(command), *(str(arg) for arg in args)]
    elif command is not None:
        raise TypeError("Provide argv or command/args, not both")

    adapter = ExecutableAdapter(registry, toolkit_id, name, argv, **kwargs)
    return adapter.tool


# A descriptive alias for callers that prefer an explicit function name.
register_executable_tool = register_executable
