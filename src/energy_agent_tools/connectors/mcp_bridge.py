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
from dataclasses import dataclass, replace
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
    ToolAccountScope,
    Toolkit,
)
from ..registry import Registry
from .mcp_network import ApprovedMCPTarget, approved_mcp_client

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
    approved_target: ApprovedMCPTarget | None = None


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


@dataclass(frozen=True)
class _PreparedMCPTool:
    raw_name: str
    namespaced_name: str
    description: str
    input_schema: Json
    schema_hash: str
    metadata: _ImportedToolMetadata


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


def _validated_selected_tools(
    selected_tools: frozenset[str] | None, *, required: bool
) -> frozenset[str] | None:
    if selected_tools is None:
        if required:
            raise MCPImportError(
                "mcp_selection_invalid", "Account-scoped MCP imports require selected tools."
            )
        return None
    if (
        type(selected_tools) is not frozenset
        or not 1 <= len(selected_tools) <= 100
        or any(
            type(name) is not str or not name or name != name.strip() or len(name) > 256
            for name in selected_tools
        )
    ):
        raise MCPImportError("mcp_selection_invalid", "Selected MCP tools are invalid.")
    return selected_tools


def _validated_account_scope(value: ToolAccountScope | None) -> ToolAccountScope | None:
    if value is None or isinstance(value, ToolAccountScope):
        return value
    try:
        return ToolAccountScope.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise MCPImportError("mcp_account_scope_invalid", "MCP account scope is invalid.") from exc


def _validate_account_auth(auth: AuthConfig) -> None:
    forbidden_headers = {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "proxy-authorization",
        "proxy-connection",
        "upgrade",
        "keep-alive",
        "te",
        "trailer",
        "accept-encoding",
    }
    if (
        auth.credential_env is not None
        or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", auth.header)
        or auth.header.lower() in forbidden_headers
    ):
        raise MCPImportError(
            "mcp_account_profile_invalid", "MCP connection auth must use a safe credential header."
        )


def _account_import_transport(
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
    credential_env: str | None,
    discovery_auth: AuthConfig | Json | None,
    discovery_credential: str | None,
    expected_schema_digest: str | None,
    auth_required: bool | None,
    approved_target: ApprovedMCPTarget | None,
) -> None:
    if discovery_credential is not None and type(discovery_credential) is not str:
        raise MCPImportError("credential_invalid", "MCP discovery credential is invalid.")
    selected_command = stdio_command if stdio_command is not None else command
    if (
        selected_command is not None
        or args is not None
        or stdio_args is not None
        or cwd is not None
        or env is not None
        or headers is not None
        or credential_env is not None
        or (url is None) == (remote_url is None)
        or expected_schema_digest is None
        or auth_required is False
        or not isinstance(approved_target, ApprovedMCPTarget)
    ):
        raise MCPImportError(
            "mcp_account_profile_invalid",
            "Account-scoped MCP imports require reviewed remote HTTP configuration.",
        )
    if discovery_auth is not None:
        try:
            auth = (
                discovery_auth
                if isinstance(discovery_auth, AuthConfig)
                else AuthConfig.model_validate(discovery_auth)
            )
        except (TypeError, ValueError) as exc:
            raise MCPImportError(
                "mcp_account_profile_invalid", "Account-scoped MCP discovery auth is invalid."
            ) from exc
        _validate_account_auth(auth)
    endpoint = remote_url if remote_url is not None else url
    if discovery_credential and endpoint and discovery_credential in endpoint:
        raise MCPImportError(
            "mcp_account_profile_invalid",
            "MCP discovery credentials cannot be part of the retained endpoint.",
        )


def _discovery_context(
    toolkit_id: str,
    discovery_auth: AuthConfig | Json | None,
    discovery_credential: str | None,
) -> ExecutionContext | None:
    if discovery_credential is not None and type(discovery_credential) is not str:
        raise MCPImportError("credential_invalid", "MCP discovery credential is invalid.")
    if discovery_auth is None and discovery_credential is None:
        return None
    if discovery_auth is None:
        auth = AuthConfig(scheme="bearer")
    else:
        try:
            auth = (
                discovery_auth
                if isinstance(discovery_auth, AuthConfig)
                else AuthConfig.model_validate(discovery_auth)
            )
        except (TypeError, ValueError) as exc:
            raise MCPImportError("credential_invalid", "MCP discovery auth is invalid.") from exc
    secret = (
        discovery_credential
        if discovery_credential is not None
        else os.environ.get(auth.credential_env or "")
    )
    if auth.scheme not in {"none", "local"} and not secret:
        raise MCPImportError("credential_missing", "MCP discovery credential is unavailable.")
    return ExecutionContext(
        Session(user_id="operator-discovery"),
        ConnectedAccount(
            id="discovery", user_id="operator-discovery", toolkit=toolkit_id, auth=auth
        ),
        secret,
        cast(httpx.AsyncClient, None),
        None,
    )


def _sanitize_metadata(
    metadata: _ImportedToolMetadata, secrets: Sequence[str]
) -> _ImportedToolMetadata:
    return replace(
        metadata,
        capabilities=tuple(str(_redact(value, secrets)) for value in metadata.capabilities),
        unit=str(_redact(metadata.unit, secrets)),
        source=str(_redact(metadata.source, secrets)),
        timezone=str(_redact(metadata.timezone, secrets)),
        resolution=(
            None if metadata.resolution is None else str(_redact(metadata.resolution, secrets))
        ),
        assumptions=tuple(str(_redact(value, secrets)) for value in metadata.assumptions),
        quality=str(_redact(metadata.quality, secrets)),
        credential_env=(
            None
            if metadata.credential_env is None
            else str(_redact(metadata.credential_env, secrets))
        ),
    )


def _validate_account_context(
    scope: ToolAccountScope, toolkit_id: str, context: ExecutionContext
) -> None:
    account = context.account
    session = context.session
    if (
        account is None
        or session.workspace_id != scope.workspace_id
        or session.resource_user_id != scope.user_id
        or account.id != scope.account_id
        or account.user_id != scope.user_id
        or account.workspace_id != scope.workspace_id
        or account.toolkit != toolkit_id
        or account.state != "active"
        or account.enabled is not True
        or (session.site_id is not None and account.site_id != session.site_id)
        or (context.site_id is not None and account.site_id != context.site_id)
        or (session.connection_grants is not None and account.id not in session.connection_grants)
        or (session.toolkits is not None and toolkit_id not in session.toolkits)
    ):
        raise EnergyError("account_forbidden", "MCP tool is outside this account scope.")
    _validate_account_auth(account.auth)


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
        return {
            str(_redact(str(key), secret_values)): _redact(item, secret_values)
            for key, item in value.items()
        }
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


def _has_secret_key(value: Any, secrets: Sequence[str]) -> bool:
    secret_values = tuple(secret for secret in secrets if secret)
    if isinstance(value, Mapping):
        return any(
            any(secret in str(key) for secret in secret_values)
            or _has_secret_key(item, secret_values)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_secret_key(item, secret_values) for item in value)
    return False


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
    if transport.approved_target is not None:
        client = approved_mcp_client(transport.approved_target, timeout=transport.timeout)
        client.headers.update(headers)
        client.timeout = httpx.Timeout(transport.timeout, read=transport.sse_read_timeout)
    else:
        client = httpx.AsyncClient(
            headers=headers or None,
            timeout=httpx.Timeout(transport.timeout, read=transport.sse_read_timeout),
            follow_redirects=False,
        )
    async with client as http_client:
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
    approved_target: ApprovedMCPTarget | None = None,
) -> _Transport:
    selected_command = stdio_command if stdio_command is not None else command
    selected_url = remote_url if remote_url is not None else url
    if approved_target is not None and (
        not isinstance(approved_target, ApprovedMCPTarget)
        or selected_command is not None
        or selected_url != approved_target.url
    ):
        raise MCPImportError(
            "mcp_target_invalid", "MCP transport differs from its approved target."
        )
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
            approved_target=approved_target,
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
    discovery_credential: str | None = None,
    approved_target: ApprovedMCPTarget | None = None,
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
        approved_target=approved_target,
    )
    discovery_context = _discovery_context(toolkit_id, discovery_auth, discovery_credential)
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
    discovery_credential: str | None = None,
    approved_target: ApprovedMCPTarget | None = None,
    account_scope: ToolAccountScope | None = None,
    selected_tools: frozenset[str] | None = None,
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

    Default imports remain operator-scoped, including servers with no
    credential declaration. A trusted integration may supply account_scope,
    selected_tools, an approved_target and the reviewed schema digest to bind
    explicitly reviewed HTTP tools to one managed connection. Discovery and
    execution then use the approved, pinned transport and per-call credentials.
    This is an internal integration contract, not a hosted URL approval API.

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

    account_scope = _validated_account_scope(account_scope)
    selected_tools = _validated_selected_tools(selected_tools, required=account_scope is not None)
    if account_scope is not None:
        _account_import_transport(
            command=command,
            args=args,
            stdio_command=stdio_command,
            stdio_args=stdio_args,
            url=url,
            remote_url=remote_url,
            cwd=cwd,
            env=env,
            headers=headers,
            credential_env=credential_env,
            discovery_auth=discovery_auth,
            discovery_credential=discovery_credential,
            expected_schema_digest=expected_schema_digest,
            auth_required=auth_required,
            approved_target=approved_target,
        )

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
        approved_target=approved_target,
    )
    discovery_context = _discovery_context(toolkit_id, discovery_auth, discovery_credential)
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

    try:
        actual_schema_digest = mcp_schema_digest(discovered)
    except MCPImportError:
        if account_scope is not None:
            raise MCPImportError(
                "mcp_schema_invalid", "Selected MCP schemas could not be safely reviewed."
            ) from None
        raise
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

    all_metadata = tool_metadata if tool_metadata is not None else metadata
    discovery_secrets = _transport_secrets(transport, discovery_context)
    discovered_names = {_remote_tool_name(remote_tool) for remote_tool in discovered}
    if selected_tools is not None and not selected_tools <= discovered_names:
        raise MCPImportError("mcp_selection_invalid", "Selected MCP tools were not discovered.")
    selected_remote_tools = [
        remote_tool
        for remote_tool in discovered
        if selected_tools is None or _remote_tool_name(remote_tool) in selected_tools
    ]
    if account_scope is not None and any(secret in toolkit_id for secret in discovery_secrets):
        raise MCPImportError(
            "mcp_account_profile_invalid", "MCP identity overlaps a discovery credential."
        )

    prepared_tools: list[_PreparedMCPTool] = []
    for remote_tool in selected_remote_tools:
        raw_name = _remote_tool_name(remote_tool)
        if account_scope is not None and any(secret in raw_name for secret in discovery_secrets):
            raise MCPImportError(
                "mcp_schema_invalid", "Selected MCP names overlap a discovery credential."
            )
        namespaced_name = f"{toolkit_id}.{raw_name}"
        try:
            if account_scope is not None:
                review = _metadata_value(all_metadata or {}, raw_name, namespaced_name)
                unit = review.get("unit")
                if type(unit) is not str or not unit.strip() or len(unit) > 128:
                    raise MCPImportError(
                        "mcp_review_required",
                        "Reviewed MCP units must be bounded nonblank strings.",
                    )
            imported_metadata = _metadata_for(
                all_metadata,
                raw_name,
                namespaced_name,
                default_source=f"mcp:{toolkit_id}",
                default_credential_env=credential_env,
            )
            if account_scope is not None and not imported_metadata.reviewed:
                raise MCPImportError(
                    "mcp_review_required",
                    "Every account-scoped MCP tool requires complete reviewed metadata.",
                )
            if account_scope is not None and imported_metadata.credential_env is not None:
                raise MCPImportError(
                    "mcp_account_profile_invalid",
                    "Account-scoped MCP tools cannot use credential environment variables.",
                )
            imported_metadata = _sanitize_metadata(imported_metadata, discovery_secrets)
            raw_input_schema = _input_schema(remote_tool)
            if account_scope is not None and _has_secret_key(raw_input_schema, discovery_secrets):
                raise MCPImportError(
                    "mcp_schema_invalid",
                    "Selected MCP schemas cannot safely retain credential-bearing field names.",
                )
            input_schema = _redact(raw_input_schema, discovery_secrets)
            schema_hash = hashlib.sha256(_canonical_json(raw_input_schema)).hexdigest()
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
        except Exception as exc:
            if account_scope is not None:
                if isinstance(exc, MCPImportError):
                    raise
                raise MCPImportError(
                    "mcp_review_required", "Selected MCP tools could not be reviewed safely."
                ) from None
            raise MCPImportError(
                "mcp_registration_failed", f"Cannot register MCP tool {raw_name!r}: {exc}"
            ) from exc
        prepared_tools.append(
            _PreparedMCPTool(
                raw_name=raw_name,
                namespaced_name=namespaced_name,
                description=str(remote_description),
                input_schema=cast(Json, input_schema),
                schema_hash=schema_hash,
                metadata=imported_metadata,
            )
        )

    try:
        toolkit = Toolkit(
            id=toolkit_id,
            name=str(_redact(name or toolkit_id, discovery_secrets)),
            description=str(
                _redact(description or f"Imported MCP toolkit {toolkit_id}", discovery_secrets)
            ),
            runtime="mcp-local" if transport.kind == "stdio" else "mcp-remote",
            status=status,
            auth_required=(
                True
                if account_scope is not None
                else bool(credential_env)
                if auth_required is None
                else auth_required
            ),
            docs_url=(str(_redact(_safe_url(docs_url), discovery_secrets)) if docs_url else None),
            version=str(_redact(version_value, discovery_secrets)),
        )
        prepared_with_models = [
            (
                item,
                Tool(
                    name=item.namespaced_name,
                    toolkit=toolkit_id,
                    resource_scope="account" if account_scope is not None else "operator",
                    description=item.description,
                    input_schema=item.input_schema,
                    account_scope=account_scope,
                    capabilities=list(item.metadata.capabilities),
                    actions=set(item.metadata.actions),
                    idempotent=item.metadata.idempotent,
                    version=toolkit.version,
                    reviewed=item.metadata.reviewed,
                    result_kind=item.metadata.kind if item.metadata.reviewed else None,
                    result_unit=item.metadata.unit if item.metadata.reviewed else None,
                ),
            )
            for item in prepared_tools
        ]
    except Exception as exc:
        if account_scope is not None:
            raise MCPImportError(
                "mcp_registration_failed", "Reviewed MCP tools could not be registered safely."
            ) from None
        raise MCPImportError(
            "mcp_registration_failed", "MCP toolkit could not be registered."
        ) from exc

    destination = registry
    registry = Registry()
    registry.add_toolkit(toolkit)

    # Capture immutable values only. In particular, the discovery context and
    # its credential never enter a registered handler's closure.
    for item, imported_tool in prepared_with_models:
        raw_name = item.raw_name
        imported_metadata = item.metadata
        schema_hash = item.schema_hash

        async def handler(
            arguments: Json,
            context: ExecutionContext,
            *,
            _raw_name: str = raw_name,
            _tool_metadata: _ImportedToolMetadata = imported_metadata,
            _schema_hash: str = schema_hash,
            _account_scope: ToolAccountScope | None = account_scope,
        ) -> EnergyResult:
            if _account_scope is not None:
                _validate_account_context(_account_scope, toolkit_id, context)
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
            schema_error: EnergyError | None = None
            result: Any = None
            try:
                with anyio.fail_after(transport.timeout):
                    async with _client_session(
                        transport,
                        context=context,
                        credential_env=_tool_metadata.credential_env,
                    ) as session:
                        try:
                            current_tools = await _list_tools(session)
                        except MCPImportError:
                            schema_error = EnergyError(
                                "mcp_schema_drift",
                                f"MCP tool {_raw_name!r} could not be revalidated before execution.",
                            )
                        if schema_error is None:
                            try:
                                matching_tools = [
                                    tool
                                    for tool in current_tools
                                    if _remote_tool_name(tool) == _raw_name
                                ]
                            except MCPImportError:
                                schema_error = EnergyError(
                                    "mcp_schema_drift",
                                    f"MCP tool {_raw_name!r} could not be revalidated before execution.",
                                )
                            if schema_error is None and len(matching_tools) != 1:
                                schema_error = EnergyError(
                                    "mcp_schema_drift",
                                    f"MCP tool {_raw_name!r} is no longer the approved tool.",
                                )
                            if schema_error is None:
                                try:
                                    current_schema_hash = hashlib.sha256(
                                        _canonical_json(_input_schema(matching_tools[0]))
                                    ).hexdigest()
                                except MCPImportError:
                                    schema_error = EnergyError(
                                        "mcp_schema_drift",
                                        f"MCP tool {_raw_name!r} schema is no longer valid.",
                                    )
                                if schema_error is None and current_schema_hash != _schema_hash:
                                    schema_error = EnergyError(
                                        "mcp_schema_drift",
                                        f"MCP tool {_raw_name!r} schema changed after approval.",
                                    )
                            if schema_error is None:
                                result = await session.call_tool(_raw_name, arguments)
                if schema_error is not None:
                    raise schema_error
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
            registry.add(imported_tool, handler)
        except Exception as exc:
            if account_scope is not None:
                raise MCPImportError(
                    "mcp_registration_failed", "Reviewed MCP tools could not be registered safely."
                ) from None
            raise MCPImportError(
                "mcp_registration_failed", "MCP tool could not be registered."
            ) from exc

    colliding_names = set(registry.tools) & set(destination.tools)
    if colliding_names:
        raise MCPImportError(
            "mcp_registration_failed", "MCP tool names conflict with the registry."
        )
    destination.add_toolkit(toolkit)
    for tool_name, imported_tool in registry.tools.items():
        destination.add(imported_tool, registry.handlers[tool_name])
    return toolkit
