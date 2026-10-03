"""Import and execute tools exposed by an MCP server.

The bridge deliberately treats an MCP server as an untrusted provider.  MCP
tool annotations are hints for a client and are never used to grant a tool an
energy action.  An operator can review a tool during import by supplying the
metadata in ``tool_metadata``; everything else is registered as a
``configuration-write`` action and is therefore rejected by a default
``Session``.

Connections are intentionally short lived in this first implementation.  A
fresh MCP client session is created for each call, initialized, used once and
closed by the SDK context managers.  This makes teardown deterministic and
avoids leaking credentials or transport state between sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Session,
    Tool,
    Toolkit,
)
from ..registry import Registry

__all__ = [
    "MCPImportError",
    "inspect_mcp",
    "inspect_mcp_manifest",
    "inspect_mcp_manifest_from_server",
    "import_mcp",
    "mcp_schema_digest",
]


_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "auth",
    "authorization",
    "credential",
    "key",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|credential)", re.I
)
_DEFAULT_UNVERIFIED_WARNING = (
    "MCP output is unverified by Energy Agent Tools; do not treat it as measured "
    "data without operator-reviewed metadata."
)
_DEFAULT_ESTIMATED_WARNING = (
    "No operator-reviewed data kind was supplied; this imported result is marked estimated."
)


class MCPImportError(EnergyError):
    """An MCP server could not be discovered or executed safely."""


_DEFAULT_VERSION = "1.0.0"
_REVIEW_POLICY = (
    "Only explicit operator metadata with reviewed=true, kind, unit, and action/actions "
    "marks an imported tool reviewed; upstream MCP annotations never grant permission."
)


@dataclass(frozen=True)
class _Transport:
    kind: Literal["stdio", "streamable-http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    url: str | None = None
    headers: Mapping[str, str] | None = None
    timeout: float = 30.0
    sse_read_timeout: float = 300.0


@dataclass(frozen=True)
class _ImportedToolMetadata:
    """Operator-owned execution and result metadata for one imported tool."""

    actions: frozenset[Action]
    capabilities: tuple[str, ...]
    unit: str
    kind: DataKind
    kind_reviewed: bool
    idempotent: bool
    source: str
    timezone: str
    resolution: str | None
    assumptions: tuple[str, ...]
    quality: str
    credential_env: str | None
    reviewed: bool


def _validated_version(value: str | None) -> str:
    """Validate an operator-facing MCP version without imposing a semver dialect.

    MCP servers use a range of version schemes.  Keeping the value opaque makes
    the bridge compatible with calendar versions and provider build IDs while
    still preventing whitespace or an empty value from changing a namespace or
    appearing ambiguously in a manifest.
    """

    version = _DEFAULT_VERSION if value is None else str(value).strip()
    if not version or any(char.isspace() for char in version):
        raise ValueError("MCP version must be a non-empty identifier without whitespace")
    if len(version) > 128:
        raise ValueError("MCP version is too long")
    return version


def _as_action(value: Any) -> Action:
    if isinstance(value, Action):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Invalid MCP action metadata: {value!r}")
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
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
        "config-write": Action.WRITE,
        "control": Action.CONTROL,
        "physical-control": Action.CONTROL,
        "critical": Action.CRITICAL,
        "safety-critical": Action.CRITICAL,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown MCP action metadata: {value!r}") from exc


def _as_kind(value: Any) -> DataKind:
    if isinstance(value, DataKind):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Invalid MCP data kind metadata: {value!r}")
    try:
        return DataKind(value.strip().lower().replace("_", "-"))
    except ValueError as exc:
        raise ValueError(f"Unknown MCP data kind metadata: {value!r}") from exc


def _metadata_value(
    metadata: Mapping[str, Mapping[str, Any]], raw_name: str, namespaced_name: str
) -> Mapping[str, Any]:
    """Find metadata by raw MCP name, namespaced name, or nested ``tools`` map."""

    candidates: list[Any] = [metadata.get(raw_name), metadata.get(namespaced_name)]
    nested = metadata.get("tools")
    if isinstance(nested, Mapping):
        candidates.extend((nested.get(raw_name), nested.get(namespaced_name)))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _metadata_for(
    metadata: Mapping[str, Mapping[str, Any]] | None,
    raw_name: str,
    namespaced_name: str,
    *,
    default_source: str,
    default_credential_env: str | None,
) -> _ImportedToolMetadata:
    values = _metadata_value(metadata or {}, raw_name, namespaced_name)

    raw_actions = values.get("actions", values.get("action", Action.WRITE))
    if isinstance(raw_actions, (str, Action)):
        actions = frozenset({_as_action(raw_actions)})
    elif isinstance(raw_actions, Sequence):
        actions = frozenset(_as_action(action) for action in raw_actions)
    else:
        raise ValueError(f"Invalid action metadata for MCP tool {raw_name!r}")
    if not actions:
        raise ValueError(f"At least one action is required for MCP tool {raw_name!r}")

    raw_capabilities = values.get("capabilities", ())
    if isinstance(raw_capabilities, str):
        capabilities: tuple[str, ...] = (raw_capabilities,)
    elif isinstance(raw_capabilities, Sequence):
        capabilities = tuple(str(value) for value in raw_capabilities)
    else:
        raise ValueError(f"Invalid capabilities metadata for MCP tool {raw_name!r}")

    raw_kind = values.get("kind", DataKind.ESTIMATED)
    kind_reviewed = "kind" in values
    raw_assumptions = values.get("assumptions", ())
    if isinstance(raw_assumptions, str):
        assumptions: tuple[str, ...] = (raw_assumptions,)
    elif isinstance(raw_assumptions, Sequence):
        assumptions = tuple(str(value) for value in raw_assumptions)
    else:
        raise ValueError(f"Invalid assumptions metadata for MCP tool {raw_name!r}")

    raw_resolution = values.get("resolution")
    resolution = None if raw_resolution is None else str(raw_resolution)
    source = str(values.get("source", default_source))
    return _ImportedToolMetadata(
        actions=actions,
        capabilities=capabilities,
        unit=str(values.get("unit", "unknown")),
        kind=_as_kind(raw_kind),
        kind_reviewed=kind_reviewed,
        idempotent=bool(values.get("idempotent", True)),
        source=source,
        timezone=str(values.get("timezone", "UTC")),
        resolution=resolution,
        assumptions=assumptions,
        quality=str(values.get("quality", "unknown")),
        credential_env=values.get("credential_env", default_credential_env),
        reviewed=(
            values.get("reviewed") is True
            and "kind" in values
            and "unit" in values
            and ("action" in values or "actions" in values)
        ),
    )


def _input_schema(remote_tool: Any) -> Json:
    """Extract and validate one upstream MCP input schema.

    The official SDK exposes ``inputSchema`` on a tool object.  Accepting a
    mapping here as well keeps the inspection helpers useful with captured
    manifests and makes the digest independent of the SDK's model class.
    """

    if isinstance(remote_tool, Mapping):
        raw_schema = remote_tool.get("inputSchema", remote_tool.get("input_schema"))
    else:
        raw_schema = getattr(remote_tool, "inputSchema", None)
        if raw_schema is None:
            raw_schema = getattr(remote_tool, "input_schema", None)
    if not isinstance(raw_schema, Mapping):
        raise MCPImportError(
            "mcp_schema_invalid", "MCP tool input schema is missing or is not an object."
        )
    # Round-tripping through JSON rejects SDK objects or non-JSON values before
    # they can influence registration or a schema digest.
    try:
        normalized = json.loads(
            json.dumps(raw_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise MCPImportError(
            "mcp_schema_invalid", "MCP tool input schema is not valid JSON."
        ) from exc
    if not isinstance(normalized, dict):
        raise MCPImportError("mcp_schema_invalid", "MCP tool input schema must be a JSON object.")
    return cast(Json, normalized)


def _remote_tool_name(remote_tool: Any) -> str:
    raw_name = (
        remote_tool.get("name")
        if isinstance(remote_tool, Mapping)
        else getattr(remote_tool, "name", None)
    )
    name = str(raw_name or "").strip()
    if not name:
        raise MCPImportError("mcp_schema_invalid", "MCP server returned a tool without a name.")
    return name


def _schema_records(discovered: Sequence[Any] | Mapping[str, Any]) -> list[Json]:
    """Return canonical, sorted schema records for hashing and inspection."""

    if isinstance(discovered, Mapping):
        candidates: list[Any] = [
            {"name": str(name), "inputSchema": schema} for name, schema in discovered.items()
        ]
    else:
        candidates = list(discovered)
    records: list[Json] = []
    seen: set[str] = set()
    for remote_tool in candidates:
        name = _remote_tool_name(remote_tool)
        if name in seen:
            raise MCPImportError(
                "mcp_schema_invalid", f"MCP server returned duplicate tool name {name!r}."
            )
        seen.add(name)
        records.append({"name": name, "inputSchema": _input_schema(remote_tool)})
    records.sort(key=lambda record: str(record["name"]))
    return records


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MCPImportError(
            "mcp_schema_invalid", "MCP schema cannot be represented as JSON."
        ) from exc


def mcp_schema_digest(discovered: Sequence[Any] | Mapping[str, Any]) -> str:
    """Return the deterministic SHA-256 digest for an MCP tool schema set.

    The digest covers sorted ``name``/``inputSchema`` records only.  Tool
    descriptions and annotations are deliberately excluded: descriptions are
    presentation text and annotations are untrusted hints.  Callers can pass
    SDK tool objects, a mapping of upstream names to schemas, or captured
    ``{"name", "inputSchema"}`` records.
    """

    return hashlib.sha256(_canonical_json(_schema_records(discovered))).hexdigest()


def _reviewed_metadata(
    metadata: Mapping[str, Mapping[str, Any]] | None,
    raw_name: str,
    namespaced_name: str,
) -> _ImportedToolMetadata:
    return _metadata_for(
        metadata,
        raw_name,
        namespaced_name,
        default_source="mcp:inspection",
        default_credential_env=None,
    )


def inspect_mcp_manifest(
    discovered: Sequence[Any] | Mapping[str, Any],
    *,
    toolkit_id: str = "mcp",
    version: str | None = None,
    tool_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Mapping[str, Any]] | None = None,
    secrets: Sequence[str] = (),
) -> Json:
    """Build a safe, deterministic inspection manifest for discovered tools.

    The manifest contains names, per-tool schema hashes and the aggregate
    digest, but never copies upstream schemas, descriptions, URLs or
    annotations.  This makes it safe to persist for drift review.  ``secrets``
    is accepted for callers that also render operator metadata; all rendered
    string fields pass through the bridge redactor.
    """

    version_value = _validated_version(version)
    records = _schema_records(discovered)
    all_metadata = tool_metadata if tool_metadata is not None else metadata
    tools: list[Json] = []
    for record in records:
        raw_name = str(record["name"])
        namespaced_name = f"{toolkit_id}.{raw_name}"
        reviewed = _reviewed_metadata(all_metadata, raw_name, namespaced_name)
        schema_hash = hashlib.sha256(_canonical_json(record["inputSchema"])).hexdigest()
        tools.append(
            {
                "name": _redact(raw_name, secrets),
                "schema_hash": schema_hash,
                "reviewed": reviewed.reviewed,
                "actions": sorted(action.value for action in reviewed.actions),
                "result_kind": reviewed.kind.value if reviewed.reviewed else None,
                "result_unit": _redact(reviewed.unit, secrets) if reviewed.reviewed else None,
                "annotations_untrusted": True,
            }
        )
    digest = hashlib.sha256(_canonical_json(records)).hexdigest()
    return {
        "toolkit": _redact(toolkit_id, secrets),
        "version": _redact(version_value, secrets),
        "upstream_names": [str(tool["name"]) for tool in tools],
        "schema_hashes": {str(tool["name"]): tool["schema_hash"] for tool in tools},
        "schema_digest": digest,
        # ``digest`` is a short compatibility alias for integrations that
        # store inspection records under a generic digest field.
        "digest": digest,
        "schema_algorithm": "sha256",
        "annotations_untrusted": True,
        "reviewed_semantics": _REVIEW_POLICY,
        "tools": tools,
    }


def _safe_url(value: str) -> str:
    """Remove URL credentials and sensitive query values before any diagnostics."""

    try:
        split = urlsplit(value)
    except ValueError:
        return "[redacted-url]"
    if not split.scheme or not split.netloc:
        return "[redacted-url]"
    host = split.hostname or ""
    try:
        port = f":{split.port}" if split.port is not None else ""
    except ValueError:
        port = ""
    netloc = f"{host}{port}"
    clean_query = [
        (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
    ]
    return urlunsplit((split.scheme, netloc, split.path, urlencode(clean_query), ""))


def _redact(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Return JSON-safe output with credential values and sensitive URLs removed."""

    secret_values = tuple(secret for secret in secrets if secret)
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            result = result.replace(secret, "[REDACTED]")
        if "://" in result:
            result = _safe_url(result)
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact(item, secret_values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, bytes):
        return _redact(value.decode("utf-8", errors="replace"), secret_values)
    if hasattr(value, "model_dump"):
        return _redact(value.model_dump(mode="json"), secret_values)
    return value


def _result_content(result: Any, secrets: Sequence[str]) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _redact(structured, secrets)
    content = getattr(result, "content", [])
    values: list[Any] = []
    for item in content:
        dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if isinstance(dumped, Mapping) and dumped.get("type") == "text":
            text = dumped.get("text", "")
            try:
                values.append(_redact(json.loads(text), secrets))
            except (TypeError, ValueError):
                values.append(_redact(text, secrets))
        else:
            values.append(_redact(dumped, secrets))
    if len(values) == 1:
        return values[0]
    return values


def _credential_headers(context: ExecutionContext | None) -> dict[str, str]:
    """Build transport headers from execution context without persisting them."""

    if context is None or not context.credential:
        return {}
    account = context.account
    auth = account.auth if account is not None else None
    if auth is None or auth.scheme in {"none", "local"}:
        return {}
    header = auth.header or "Authorization"
    if auth.scheme in {"bearer", "oauth"}:
        return {header: f"Bearer {context.credential}"}
    if auth.scheme == "basic":
        return {header: f"Basic {context.credential}"}
    return {header: context.credential}


def _transport_secrets(transport: _Transport, context: ExecutionContext | None) -> tuple[str, ...]:
    """Collect only values that are credentials for result redaction."""

    values: list[str] = []
    if context is not None and context.credential:
        values.append(context.credential)
    if transport.headers:
        values.extend(str(value) for value in transport.headers.values())
    if transport.env:
        values.extend(
            str(value) for key, value in transport.env.items() if _SENSITIVE_KEY_RE.search(str(key))
        )
    return tuple(value for value in values if value)


def _stdio_environment(
    transport: _Transport, context: ExecutionContext | None, credential_env: str | None
) -> dict[str, str] | None:
    # The MCP SDK itself only inherits a small, safe allowlist when ``env`` is
    # omitted.  Preserve that behavior and add only explicit operator values
    # and the per-execution credential.
    env = None if transport.env is None else {str(k): str(v) for k, v in transport.env.items()}
    if context is not None and context.credential and credential_env:
        if env is None:
            env = {}
        env[credential_env] = context.credential
    return env


@asynccontextmanager
async def _client_session(
    transport: _Transport,
    *,
    context: ExecutionContext | None,
    credential_env: str | None,
):
    """Open, initialize, and close one official MCP SDK client session."""

    if transport.kind == "stdio":
        assert transport.command is not None
        params = StdioServerParameters(
            command=transport.command,
            args=list(transport.args),
            cwd=transport.cwd,
            env=_stdio_environment(transport, context, credential_env),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
        return

    assert transport.url is not None
    headers = dict(transport.headers or {})
    headers.update(_credential_headers(context))
    async with httpx.AsyncClient(
        headers=headers or None,
        timeout=httpx.Timeout(transport.timeout, read=transport.sse_read_timeout),
        follow_redirects=False,
    ) as http_client:
        async with streamable_http_client(transport.url, http_client=http_client) as (
            read_stream,
            write_stream,
            _session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def _list_tools(session: Any) -> list[Any]:
    """List all tools in one initialized session under bounded pagination."""

    result = await session.list_tools()
    discovered = list(result.tools)
    cursor = result.nextCursor
    seen: set[str] = set()
    while cursor:
        if cursor in seen or len(discovered) > 500 or len(seen) >= 30:
            raise MCPImportError(
                "mcp_discovery_limit",
                "MCP server returned repeated pagination or too many tools.",
            )
        seen.add(cursor)
        result = await session.list_tools(cursor=cursor)
        discovered.extend(result.tools)
        cursor = result.nextCursor
    if len(discovered) > 500:
        raise MCPImportError(
            "mcp_discovery_limit", "MCP server exceeded the 500-tool import limit."
        )
    return discovered


async def _discover_tools(
    transport: _Transport,
    context: ExecutionContext | None = None,
    credential_env: str | None = None,
) -> list[Any]:
    async with _client_session(
        transport, context=context, credential_env=credential_env
    ) as session:
        return await _list_tools(session)


def _transport_from_arguments(
    *,
    command: str | Sequence[str] | None,
    args: Sequence[str] | None,
    stdio_command: str | Sequence[str] | None,
    stdio_args: Sequence[str] | None,
    url: str | None,
    remote_url: str | None,
    cwd: str | Path | None,
    env: Mapping[str, str] | None,
    headers: Mapping[str, str] | None,
    timeout: float,
    sse_read_timeout: float,
) -> _Transport:
    selected_command = stdio_command if stdio_command is not None else command
    selected_url = remote_url if remote_url is not None else url
    if selected_command is not None and selected_url is not None:
        raise ValueError("Configure either an MCP stdio command or a remote URL, not both")
    if selected_command is None and selected_url is None:
        raise ValueError("An MCP stdio command or remote streamable-http URL is required")
    if timeout <= 0 or sse_read_timeout <= 0:
        raise ValueError("MCP timeouts must be positive")
    if selected_url is not None:
        if args or stdio_args:
            raise ValueError("MCP args are valid only for stdio transport")
        return _Transport(
            kind="streamable-http",
            url=str(selected_url),
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )

    assert selected_command is not None
    if isinstance(selected_command, str):
        actual_command = selected_command
        supplied_args = stdio_args if stdio_command is not None else args
        command_args = list(supplied_args or ())
    else:
        command_parts = [str(part) for part in selected_command]
        if not command_parts:
            raise ValueError("MCP stdio command cannot be empty")
        actual_command, command_parts_args = command_parts[0], command_parts[1:]
        supplied_args = stdio_args if stdio_command is not None else args
        command_args = command_parts_args + list(supplied_args or ())
    if not actual_command:
        raise ValueError("MCP stdio command cannot be empty")
    return _Transport(
        kind="stdio",
        command=actual_command,
        args=tuple(command_args),
        cwd=cwd,
        env=env,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
    )


async def inspect_mcp(
    toolkit_id: str = "mcp",
    *,
    command: str | Sequence[str] | None = None,
    args: Sequence[str] | None = None,
    stdio_command: str | Sequence[str] | None = None,
    stdio_args: Sequence[str] | None = None,
    url: str | None = None,
    remote_url: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    sse_read_timeout: float = 300.0,
    tool_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Mapping[str, Any]] | None = None,
    credential_env: str | None = None,
    discovery_auth: AuthConfig | Json | None = None,
    version: str | None = None,
) -> Json:
    """Discover an MCP server and return a credential-safe review manifest.

    Inspection opens the same short-lived official MCP client session used by
    :func:`import_mcp`, but never mutates a registry.  Persist the returned
    ``schema_digest`` and provide it as ``expected_schema_digest`` on import to
    reject unreviewed upstream schema drift.
    """

    if not toolkit_id or any(char.isspace() for char in toolkit_id):
        raise ValueError("toolkit_id must be a non-empty identifier without whitespace")
    version_value = _validated_version(version)
    transport = _transport_from_arguments(
        command=command,
        args=args,
        stdio_command=stdio_command,
        stdio_args=stdio_args,
        url=url,
        remote_url=remote_url,
        cwd=cwd,
        env=env,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
    )
    discovery_context = None
    if discovery_auth is not None:
        auth = (
            discovery_auth
            if isinstance(discovery_auth, AuthConfig)
            else AuthConfig.model_validate(discovery_auth)
        )
        secret = os.environ.get(auth.credential_env or "")
        if auth.scheme not in {"none", "local"} and not secret:
            raise MCPImportError(
                "credential_missing", "MCP discovery credential is absent from the environment."
            )
        discovery_context = ExecutionContext(
            Session(user_id="operator-discovery"),
            ConnectedAccount(
                id="discovery", user_id="operator-discovery", toolkit=toolkit_id, auth=auth
            ),
            secret,
            cast(httpx.AsyncClient, None),
            None,
        )
    try:
        with anyio.fail_after(transport.timeout):
            discovered = await _discover_tools(transport, discovery_context, credential_env)
    except TimeoutError as exc:
        raise MCPImportError(
            "mcp_timeout",
            "MCP initialization or tool discovery exceeded its timeout.",
            retryable=True,
        ) from exc
    except MCPImportError:
        raise
    except Exception as exc:
        raise MCPImportError(
            "mcp_discovery_failed",
            "MCP initialization or discovery failed; check local transport and authentication configuration.",
            retryable=True,
        ) from exc
    return inspect_mcp_manifest(
        discovered,
        toolkit_id=toolkit_id,
        version=version_value,
        tool_metadata=tool_metadata,
        metadata=metadata,
        secrets=_transport_secrets(transport, discovery_context),
    )


# A descriptive alias for callers that prefer the artifact name in their code.
inspect_mcp_manifest_from_server = inspect_mcp


async def import_mcp(
    registry: Registry,
    toolkit_id: str,
    *,
    command: str | Sequence[str] | None = None,
    args: Sequence[str] | None = None,
    stdio_command: str | Sequence[str] | None = None,
    stdio_args: Sequence[str] | None = None,
    url: str | None = None,
    remote_url: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    sse_read_timeout: float = 300.0,
    name: str | None = None,
    description: str | None = None,
    tool_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Mapping[str, Any]] | None = None,
    credential_env: str | None = None,
    discovery_auth: AuthConfig | Json | None = None,
    status: Literal[
        "stable", "experimental", "requires credentials", "unavailable"
    ] = "experimental",
    auth_required: bool | None = None,
    docs_url: str | None = None,
    version: str | None = None,
    expected_schema_digest: str | None = None,
) -> Toolkit:
    """Discover and register an MCP server's tools.

    ``tool_metadata`` is intentionally operator supplied.  Its per-tool
    entries may contain ``action``/``actions``, ``capabilities``, ``unit``,
    ``kind``, ``reviewed``, ``idempotent``, ``timezone``, ``resolution``,
    ``assumptions``, ``quality``, ``source`` and ``credential_env``.  An
    imported tool is marked reviewed only when ``reviewed`` is explicitly
    ``True`` and ``kind``, ``unit`` and ``action``/``actions`` are all present.
    Other tools default to ``configuration-write`` and ``estimated``.

    Every imported MCP tool is classified as operator-scoped. This includes
    imports with no configured credentials: the server may still expose
    operator-selected data or capabilities. Hosted sessions therefore cannot
    call imported tools until a separate reviewed contract supports public or
    tenant-owned MCP resources. Local sessions retain the existing behavior.

    The returned toolkit contains namespaced tools (``<toolkit_id>.<name>``).
    Names remain stable across versions; the toolkit and tools carry the
    operator supplied ``version`` metadata.  Set ``expected_schema_digest`` to
    reject an upstream schema drift before anything is committed to the
    registry.  The remote URL and credentials are never placed in result
    provenance.
    """

    if not toolkit_id or any(char.isspace() for char in toolkit_id):
        raise ValueError("toolkit_id must be a non-empty identifier without whitespace")
    if toolkit_id in registry.toolkits:
        raise ValueError(f"Duplicate toolkit: {toolkit_id}")
    version_value = _validated_version(version)

    transport = _transport_from_arguments(
        command=command,
        args=args,
        stdio_command=stdio_command,
        stdio_args=stdio_args,
        url=url,
        remote_url=remote_url,
        cwd=cwd,
        env=env,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
    )
    discovery_context = None
    if discovery_auth is not None:
        auth = (
            discovery_auth
            if isinstance(discovery_auth, AuthConfig)
            else AuthConfig.model_validate(discovery_auth)
        )
        secret = os.environ.get(auth.credential_env or "")
        if auth.scheme not in {"none", "local"} and not secret:
            raise MCPImportError(
                "credential_missing", "MCP discovery credential is absent from the environment."
            )
        discovery_context = ExecutionContext(
            Session(user_id="operator-discovery"),
            ConnectedAccount(
                id="discovery", user_id="operator-discovery", toolkit=toolkit_id, auth=auth
            ),
            secret,
            cast(httpx.AsyncClient, None),
            None,
        )
    try:
        with anyio.fail_after(transport.timeout):
            discovered = await _discover_tools(transport, discovery_context, credential_env)
    except TimeoutError as exc:
        raise MCPImportError(
            "mcp_timeout",
            "MCP initialization or tool discovery exceeded its timeout.",
            retryable=True,
        ) from exc
    except Exception as exc:
        raise MCPImportError(
            "mcp_discovery_failed",
            "MCP initialization or discovery failed; check local transport and authentication configuration.",
            retryable=True,
        ) from exc

    actual_schema_digest = mcp_schema_digest(discovered)
    if expected_schema_digest is not None:
        expected = str(expected_schema_digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MCPImportError(
                "mcp_schema_digest_invalid",
                "Expected MCP schema digest must be a 64-character SHA-256 hex digest.",
            )
        if expected != actual_schema_digest:
            raise MCPImportError(
                "mcp_schema_drift",
                "MCP tool schemas changed since the approved schema digest was recorded.",
            )

    toolkit = Toolkit(
        id=toolkit_id,
        name=name or toolkit_id,
        description=description or f"Imported MCP toolkit {toolkit_id}",
        runtime="mcp-local" if transport.kind == "stdio" else "mcp-remote",
        status=status,
        # Static headers are already operator-provided transport credentials;
        # only a per-execution credential environment reference requires an
        # Energy ConnectedAccount at runtime.
        auth_required=bool(credential_env) if auth_required is None else auth_required,
        # A remote endpoint can contain credentials in its query string.  Do
        # not publish that URL as docs/provenance; callers can set an explicit
        # safe docs_url when they want one.
        docs_url=_safe_url(docs_url) if docs_url else None,
        version=version_value,
    )
    destination = registry
    registry = Registry()
    registry.add_toolkit(toolkit)

    all_metadata = tool_metadata if tool_metadata is not None else metadata
    discovery_secrets = _transport_secrets(transport, discovery_context)
    for remote_tool in discovered:
        raw_name = _remote_tool_name(remote_tool)
        namespaced_name = f"{toolkit_id}.{raw_name}"
        try:
            imported_metadata = _metadata_for(
                all_metadata,
                raw_name,
                namespaced_name,
                default_source=f"mcp:{toolkit_id}",
                default_credential_env=credential_env,
            )
            raw_input_schema = _input_schema(remote_tool)
            input_schema = _redact(raw_input_schema, discovery_secrets)
            schema_hash = hashlib.sha256(_canonical_json(raw_input_schema)).hexdigest()
        except Exception as exc:
            # Discovery succeeded, but a malformed schema or operator metadata
            # must not leave a half-registered tool behind.
            registry.toolkits.pop(toolkit_id, None)
            raise MCPImportError(
                "mcp_registration_failed", f"Cannot register MCP tool {raw_name!r}: {exc}"
            ) from exc

        remote_description = _redact(
            (
                remote_tool.get("description") or remote_tool.get("title") or raw_name
                if isinstance(remote_tool, Mapping)
                else getattr(remote_tool, "description", None)
                or getattr(remote_tool, "title", None)
                or raw_name
            ),
            discovery_secrets,
        )

        # Capture immutable values only.  In particular, never capture an
        # ExecutionContext or credential at import time.
        async def handler(
            arguments: Json,
            context: ExecutionContext,
            *,
            _raw_name: str = raw_name,
            _tool_metadata: _ImportedToolMetadata = imported_metadata,
            _schema_hash: str = schema_hash,
        ) -> EnergyResult:
            if not _tool_metadata.actions.issubset(context.session.allowed_actions):
                allowed = ", ".join(
                    sorted(action.value for action in context.session.allowed_actions)
                )
                required = ", ".join(sorted(action.value for action in _tool_metadata.actions))
                raise EnergyError(
                    "action_not_allowed",
                    f"MCP tool {_raw_name!r} requires [{required}]; session allows [{allowed}].",
                )
            secrets = _transport_secrets(transport, context)
            try:
                with anyio.fail_after(transport.timeout):
                    async with _client_session(
                        transport,
                        context=context,
                        credential_env=_tool_metadata.credential_env,
                    ) as session:
                        try:
                            current_tools = await _list_tools(session)
                        except MCPImportError as exc:
                            raise EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} could not be revalidated before execution.",
                            ) from exc
                        try:
                            matching_tools = [
                                tool
                                for tool in current_tools
                                if _remote_tool_name(tool) == _raw_name
                            ]
                        except MCPImportError as exc:
                            raise EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} could not be revalidated before execution.",
                            ) from exc
                        if len(matching_tools) != 1:
                            raise EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} is no longer the approved tool.",
                            )
                        try:
                            current_schema_hash = hashlib.sha256(
                                _canonical_json(_input_schema(matching_tools[0]))
                            ).hexdigest()
                        except MCPImportError as exc:
                            raise EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} schema is no longer valid.",
                            ) from exc
                        if current_schema_hash != _schema_hash:
                            raise EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} schema changed after approval.",
                            )
                        result = await session.call_tool(_raw_name, arguments)
            except TimeoutError as exc:
                raise EnergyError(
                    "mcp_timeout", f"MCP tool {_raw_name!r} exceeded its timeout.", retryable=True
                ) from exc
            except EnergyError:
                raise
            except Exception as exc:
                raise EnergyError(
                    "mcp_execution_failed",
                    f"MCP tool {_raw_name!r} failed: {_redact(str(exc), secrets)}",
                    retryable=True,
                ) from exc

            if getattr(result, "isError", False):
                detail = _result_content(result, secrets)
                if isinstance(detail, (dict, list)):
                    detail = json.dumps(detail, ensure_ascii=False, default=str)
                raise EnergyError(
                    "mcp_tool_error",
                    f"MCP tool {_raw_name!r} returned an error: {str(detail)[:2000]}",
                    retryable=False,
                )

            warnings = [_DEFAULT_UNVERIFIED_WARNING]
            if not _tool_metadata.reviewed:
                warnings.append(_DEFAULT_ESTIMATED_WARNING)
            data = _result_content(result, secrets)
            return EnergyResult(
                data=data,
                kind=_tool_metadata.kind,
                unit=_tool_metadata.unit,
                source=_redact(_tool_metadata.source, secrets),
                timezone=_tool_metadata.timezone,
                resolution=_tool_metadata.resolution,
                assumptions=list(_tool_metadata.assumptions),
                warnings=warnings,
                quality=_tool_metadata.quality,
                # Keep provenance local and non-secret.  Do not include the
                # remote URL, credential headers, or server supplied links.
                provenance=[{"toolkit": toolkit_id, "tool": _raw_name}],
            )

        try:
            registry.add(
                Tool(
                    name=namespaced_name,
                    toolkit=toolkit_id,
                    resource_scope="operator",
                    description=remote_description,
                    input_schema=input_schema,
                    capabilities=list(imported_metadata.capabilities),
                    actions=set(imported_metadata.actions),
                    idempotent=imported_metadata.idempotent,
                    version=version_value,
                    reviewed=imported_metadata.reviewed,
                    result_kind=imported_metadata.kind if imported_metadata.reviewed else None,
                    result_unit=imported_metadata.unit if imported_metadata.reviewed else None,
                ),
                handler,
            )
        except Exception as exc:
            registry.toolkits.pop(toolkit_id, None)
            for registered_name in [
                registered.name
                for registered in registry.tools.values()
                if registered.toolkit == toolkit_id
            ]:
                registry.tools.pop(registered_name, None)
                registry.handlers.pop(registered_name, None)
            raise MCPImportError(
                "mcp_registration_failed", f"Cannot register MCP tool {raw_name!r}: {exc}"
            ) from exc
    destination.add_toolkit(toolkit)
    for tool_name, imported_tool in registry.tools.items():
        destination.add(imported_tool, registry.handlers[tool_name])
    return toolkit
