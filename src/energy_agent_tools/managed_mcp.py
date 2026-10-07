"""Internal lifecycle management for workspace-owned MCP connections."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .auth import AuthStore
from .connectors.mcp_bridge import (
    MCPImportError,
    import_mcp,
    inspect_mcp,
    validate_mcp_connection_auth,
)
from .connectors.mcp_network import ApprovedMCPTarget, approve_mcp_target
from .models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    DataKind,
    EnergyError,
    Json,
    Site,
    ToolAccountScope,
)
from .registry import Registry

_DISCOVERY_TIMEOUT_SECONDS = 30.0
_DEFINITION_FORMAT = 1
_MAX_DEFINITION_BYTES = 128 * 1024
_MAX_SELECTED_TOOLS = 100
_MAX_TOOL_NAME_LENGTH = 256
_SUPPORTED_SCHEMES = frozenset({"none", "bearer", "basic", "api-key"})
_REVIEW_FIELDS = frozenset(
    {
        "action",
        "actions",
        "capabilities",
        "idempotent",
        "kind",
        "quality",
        "resolution",
        "reviewed",
        "source",
        "timezone",
        "unit",
        "assumptions",
    }
)
_SECRET_KEY_RE = re.compile(r"(?:credential|secret|token|password|authorization|api[_-]?key)", re.I)
_SAFE_IMPORT_CODES = frozenset(
    {
        "credential_missing",
        "credential_invalid",
        "mcp_account_profile_invalid",
        "mcp_discovery_failed",
        "mcp_review_required",
        "mcp_schema_digest_invalid",
        "mcp_schema_drift",
        "mcp_schema_invalid",
        "mcp_selection_invalid",
        "mcp_target_invalid",
        "mcp_timeout",
    }
)
_SAFE_ERROR_MESSAGES = {
    "credential_invalid": "MCP connection credential is invalid.",
    "credential_missing": "MCP connection credential is unavailable.",
    "mcp_account_profile_invalid": "MCP connection profile is invalid.",
    "mcp_discovery_failed": "MCP server discovery failed.",
    "mcp_review_required": "Every selected MCP tool requires complete reviewed metadata.",
    "mcp_schema_digest_invalid": "MCP schema digest is invalid.",
    "mcp_schema_drift": "MCP tool schemas changed since approval.",
    "mcp_schema_invalid": "MCP server returned an invalid tool schema.",
    "mcp_selection_invalid": "MCP tool selection is invalid.",
    "mcp_target_invalid": "MCP target is not approved.",
    "mcp_timeout": "MCP server operation timed out.",
}
_ACTION_ALIASES = {
    "read": Action.READ,
    "read-only": Action.READ,
    "readonly": Action.READ,
    "calculate": Action.CALCULATE,
    "calculation": Action.CALCULATE,
    "simulation": Action.SIMULATE,
    "simulate": Action.SIMULATE,
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

TargetApprover = Callable[[str, str], Awaitable[ApprovedMCPTarget | None]]


class ManagedMCPService:
    """Stage, map, recover, and publish explicitly reviewed managed MCP tools."""

    def __init__(
        self,
        auth_store: AuthStore,
        registry: Registry,
        *,
        approve_target: TargetApprover | None = None,
        authorize_write: Callable[[], None] | None = None,
    ) -> None:
        self.auth_store = auth_store
        self.registry = registry
        self._approve_target = approve_target
        self._authorize_write = authorize_write if authorize_write is not None else (lambda: None)

    async def inspect(
        self,
        *,
        user_id: str,
        workspace_id: str,
        url: str,
        auth: AuthConfig,
        credential: str | None,
    ) -> Json:
        _validate_identity(user_id, workspace_id)
        checked_auth = _validated_auth(auth, credential)
        _validate_connection_auth(checked_auth)
        _validate_url_credential(url, credential)
        target = await self._approved_target(workspace_id, url)
        _validate_url_credential(target.url, credential)
        try:
            async with asyncio.timeout(_DISCOVERY_TIMEOUT_SECONDS):
                return await inspect_mcp(
                    "managed-mcp-preview",
                    remote_url=target.url,
                    discovery_auth=_ephemeral_auth(checked_auth),
                    discovery_credential=credential,
                    approved_target=target,
                )
        except TimeoutError:
            raise EnergyError("mcp_timeout", _SAFE_ERROR_MESSAGES["mcp_timeout"]) from None
        except MCPImportError as exc:
            raise _public_import_error(exc) from None
        except Exception:
            raise EnergyError("mcp_unavailable", "MCP inspection is unavailable.") from None

    async def stage(
        self,
        *,
        user_id: str,
        workspace_id: str,
        url: str,
        display_name: str,
        auth: AuthConfig,
        credential: str | None,
        expected_schema_digest: str,
        selected_tools: frozenset[str],
        tool_metadata: Mapping[str, Mapping[str, Any]],
    ) -> Json:
        self._authorize_write()
        _validate_identity(user_id, workspace_id)
        checked_auth = _validated_auth(auth, credential)
        checked_display_name = _bounded_text(display_name, "display name", 512)
        _validate_connection_auth(checked_auth)
        _validate_url_credential(url, credential)
        if _contains_credential(checked_display_name, credential):
            raise EnergyError("mcp_review_invalid", "MCP display name contains a credential.")
        checked_digest = _validated_digest(expected_schema_digest)
        selected = _validated_selection(selected_tools)
        if any(_contains_credential(name, credential) for name in selected):
            raise EnergyError("mcp_review_invalid", "MCP review metadata is invalid.")
        toolkit_id = f"custom-mcp-{uuid4().hex}"
        target = await self._approved_target(workspace_id, url)
        _validate_url_credential(target.url, credential)
        reviews = _validated_reviews(
            selected,
            tool_metadata,
            toolkit_id=toolkit_id,
            credential=credential,
        )
        definition = _definition(target.url, checked_digest, reviews)
        if _contains_credential(definition, credential):
            raise EnergyError("mcp_review_invalid", "MCP review metadata is invalid.")
        temporary = await self._import_temporary(
            toolkit_id=toolkit_id,
            account_scope=ToolAccountScope(
                workspace_id=workspace_id, user_id=user_id, account_id=toolkit_id
            ),
            target=target,
            auth=checked_auth,
            credential=credential,
            expected_schema_digest=checked_digest,
            selected_tools=selected,
            reviews=reviews,
        )
        if len(temporary.tools) != len(selected):
            raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])

        account = ConnectedAccount(
            id=toolkit_id,
            user_id=user_id,
            workspace_id=workspace_id,
            toolkit=toolkit_id,
            display_name=checked_display_name,
            site_id=None,
            auth=checked_auth,
            settings={"managed_mcp": definition},
            enabled=False,
            state="pending_mapping",
            last_verified_at=datetime.now(UTC),
        )
        try:
            self._authorize_write()
            staged = self.auth_store.stage_managed(account, credential)
        except EnergyError:
            raise
        except Exception:
            raise EnergyError("mcp_stage_failed", "MCP connection could not be staged.") from None
        return _success_outcome(staged, checked_digest, len(selected))

    async def map(
        self,
        *,
        user_id: str,
        workspace_id: str,
        connection_id: str,
        site: Site,
    ) -> Json:
        self._authorize_write()
        _validate_identity(user_id, workspace_id)
        _validate_site_owner(site, user_id)
        try:
            account, revision = self.auth_store.managed_snapshot(
                user_id, workspace_id, connection_id
            )
        except EnergyError:
            raise
        _require_account_identity(account, user_id, workspace_id)
        definition, selected, reviews = _read_definition(account)
        pending = (
            account.state == "pending_mapping" and not account.enabled and account.site_id is None
        )
        active_retry = account.state == "active" and account.enabled and account.site_id == site.id
        if not pending and not active_retry:
            if account.state == "active" and account.site_id != site.id:
                raise EnergyError(
                    "connection_conflict", "Connection is already mapped to another site."
                )
            raise EnergyError("connection_not_pending", "MCP connection is not awaiting mapping.")

        scope = ToolAccountScope(
            workspace_id=workspace_id, user_id=user_id, account_id=connection_id
        )
        # A stale namespace must never survive for a pending mapping. During an
        # idempotent active retry, keep the currently working namespace in
        # place until discovery succeeds and the replacement can be atomic.
        if pending:
            self._remove_namespace(account.toolkit, scope)
        auth = _stored_auth(account)
        _validate_connection_auth(auth)
        _validate_account_credential_strings(account, credential=None)
        credential = self._connection_credential(account, pending=pending)
        _validate_account_credential_strings(account, credential=credential)
        target = await self._approved_target(workspace_id, str(definition["url"]))
        _validate_url_credential(target.url, credential)
        temporary = await self._import_temporary(
            toolkit_id=account.toolkit,
            account_scope=scope,
            target=target,
            auth=auth,
            credential=credential,
            expected_schema_digest=str(definition["schema_digest"]),
            selected_tools=selected,
            reviews=reviews,
        )
        if len(temporary.tools) != len(selected):
            raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])

        self._authorize_write()
        activated = self.auth_store.activate_managed(
            user_id,
            workspace_id,
            connection_id,
            site=site,
            expected_version=revision,
            verified_at=datetime.now(UTC),
        )
        self._require_matching_snapshot(activated, revision + (1 if pending else 0))
        try:
            self._authorize_write()
            self._publish(temporary, account.toolkit, scope)
        except Exception:
            self._remove_namespace(account.toolkit, scope)
            raise EnergyError("mcp_publish_failed", "MCP tools could not be published.") from None
        return _success_outcome(activated, str(definition["schema_digest"]), len(selected))

    async def recover(self, *, user_id: str, workspace_id: str) -> Json:
        self._authorize_write()
        _validate_identity(user_id, workspace_id)
        try:
            accounts = self.auth_store.workspace_accounts(user_id, workspace_id)
        except Exception:
            raise EnergyError(
                "managed_mcp_unavailable", "Managed MCP recovery is unavailable."
            ) from None
        self._remove_deleted_namespaces(user_id, workspace_id, {account.id for account in accounts})
        results: list[Json] = []
        for account in accounts:
            if not account.toolkit.startswith("custom-mcp-"):
                continue
            scope = ToolAccountScope(
                workspace_id=workspace_id, user_id=user_id, account_id=account.id
            )
            try:
                self._remove_namespace(account.toolkit, scope)
            except Exception:
                results.append(_recovery_result(account.id, "unavailable", "registry_unavailable"))
                continue
            if account.state != "active" or not account.enabled or account.site_id is None:
                results.append(_recovery_result(account.id, "skipped", "connection_inactive"))
                continue
            try:
                _require_account_identity(account, user_id, workspace_id)
                definition, selected, reviews = _read_definition(account)
                auth = _stored_auth(account)
                _validate_connection_auth(auth)
                _validate_account_credential_strings(account, credential=None)
                snapshot, revision = self.auth_store.managed_snapshot(
                    user_id, workspace_id, account.id
                )
                if snapshot != account:
                    raise EnergyError(
                        "connection_changed", "MCP connection changed during recovery."
                    )
                credential = self._connection_credential(account, pending=False)
                _validate_account_credential_strings(account, credential=credential)
                target = await self._approved_target(workspace_id, str(definition["url"]))
                _validate_url_credential(target.url, credential)
                temporary = await self._import_temporary(
                    toolkit_id=account.toolkit,
                    account_scope=scope,
                    target=target,
                    auth=auth,
                    credential=credential,
                    expected_schema_digest=str(definition["schema_digest"]),
                    selected_tools=selected,
                    reviews=reviews,
                )
                current, current_revision = self.auth_store.managed_snapshot(
                    user_id, workspace_id, account.id
                )
                if (
                    current_revision != revision
                    or current != account
                    or current.state != "active"
                    or not current.enabled
                ):
                    raise EnergyError(
                        "connection_changed", "MCP connection changed during recovery."
                    )
                self._authorize_write()
                self._publish(temporary, account.toolkit, scope)
                results.append(_recovery_result(account.id, "ready", None))
            except Exception as exc:
                try:
                    self._remove_namespace(account.toolkit, scope)
                except Exception:
                    results.append(
                        _recovery_result(account.id, "unavailable", "registry_unavailable")
                    )
                    continue
                results.append(_recovery_result(account.id, "unavailable", _safe_code(exc)))
        return {"ok": True, "connections": results}

    async def _approved_target(self, workspace_id: str, url: str) -> ApprovedMCPTarget:
        try:
            target = (
                await approve_mcp_target(url)
                if self._approve_target is None
                else await self._approve_target(workspace_id, url)
            )
        except Exception:
            raise EnergyError(
                "mcp_target_invalid", _SAFE_ERROR_MESSAGES["mcp_target_invalid"]
            ) from None
        if not isinstance(target, ApprovedMCPTarget):
            raise EnergyError("mcp_target_invalid", _SAFE_ERROR_MESSAGES["mcp_target_invalid"])
        return target

    async def _import_temporary(
        self,
        *,
        toolkit_id: str,
        account_scope: ToolAccountScope,
        target: ApprovedMCPTarget,
        auth: AuthConfig,
        credential: str | None,
        expected_schema_digest: str,
        selected_tools: frozenset[str],
        reviews: Mapping[str, Mapping[str, Any]],
    ) -> Registry:
        temporary = Registry()
        try:
            async with asyncio.timeout(_DISCOVERY_TIMEOUT_SECONDS):
                await import_mcp(
                    temporary,
                    toolkit_id,
                    remote_url=target.url,
                    discovery_auth=_ephemeral_auth(auth),
                    discovery_credential=credential,
                    approved_target=target,
                    account_scope=account_scope,
                    selected_tools=selected_tools,
                    tool_metadata=reviews,
                    auth_required=True,
                    expected_schema_digest=expected_schema_digest,
                )
        except TimeoutError:
            raise EnergyError("mcp_timeout", _SAFE_ERROR_MESSAGES["mcp_timeout"]) from None
        except MCPImportError as exc:
            raise _public_import_error(exc) from None
        except Exception:
            raise EnergyError(
                "mcp_discovery_failed", _SAFE_ERROR_MESSAGES["mcp_discovery_failed"]
            ) from None
        return temporary

    def _publish(self, temporary: Registry, toolkit_id: str, scope: ToolAccountScope) -> None:
        toolkit = temporary.toolkits.get(toolkit_id)
        if toolkit is None:
            raise EnergyError("mcp_publish_failed", "MCP tools could not be published.")
        tools = {
            name: (tool, temporary.handlers[name])
            for name, tool in temporary.tools.items()
            if tool.toolkit == toolkit_id
        }
        self.registry.replace_account_toolkit(toolkit, tools, account_scope=scope)

    def _remove_namespace(self, toolkit_id: str, scope: ToolAccountScope) -> None:
        self.registry.remove_account_toolkit(toolkit_id, account_scope=scope)

    def _connection_credential(self, account: ConnectedAccount, *, pending: bool) -> str | None:
        if account.auth.scheme == "none":
            return None
        if pending:
            return self.auth_store.pending_credential(
                account.user_id, str(account.workspace_id), account.id
            )
        return self.auth_store.credential(account.user_id, account.id, account.site_id)

    def _remove_deleted_namespaces(
        self, user_id: str, workspace_id: str, known_connection_ids: set[str]
    ) -> None:
        registered_toolkits = {
            toolkit_id
            for toolkit_id in self.registry.toolkits
            if toolkit_id.startswith("custom-mcp-")
        }
        for toolkit_id in registered_toolkits - known_connection_ids:
            scopes = [
                tool.account_scope
                for tool in self.registry.tools.values()
                if tool.toolkit == toolkit_id and tool.account_scope is not None
            ]
            for scope in scopes:
                if (
                    scope is not None
                    and scope.user_id == user_id
                    and scope.workspace_id == workspace_id
                    and scope.account_id == toolkit_id
                ):
                    try:
                        self._remove_namespace(toolkit_id, scope)
                    except Exception:
                        pass
                    break

    def _require_matching_snapshot(
        self, expected: ConnectedAccount, expected_revision: int
    ) -> None:
        current, revision = self.auth_store.managed_snapshot(
            expected.user_id, str(expected.workspace_id), expected.id
        )
        if current != expected or revision != expected_revision:
            raise EnergyError("connection_changed", "MCP connection changed during mapping.")


def _validate_identity(user_id: str, workspace_id: str) -> None:
    if any(
        type(value) is not str or not value.strip() or len(value) > 256
        for value in (user_id, workspace_id)
    ):
        raise EnergyError("mcp_scope_invalid", "MCP workspace identity is invalid.")


def _validate_site_owner(site: Site, user_id: str) -> None:
    if (
        not isinstance(site, Site)
        or type(site.id) is not str
        or not site.id
        or site.user_id != user_id
    ):
        raise EnergyError("account_forbidden", "Site is outside this user/workspace scope.")


def _validate_connection_auth(auth: AuthConfig) -> None:
    try:
        validate_mcp_connection_auth(auth)
    except MCPImportError as exc:
        raise _public_import_error(exc) from None


def _validate_account_credential_strings(
    account: ConnectedAccount, *, credential: str | None
) -> None:
    if credential is None:
        return
    if _contains_credential(account.display_name, credential) or _contains_credential(
        account.settings, credential
    ):
        raise EnergyError("mcp_definition_invalid", "MCP connection definition is invalid.")


def _validated_auth(auth: AuthConfig, credential: str | None) -> AuthConfig:
    try:
        value = auth if isinstance(auth, AuthConfig) else AuthConfig.model_validate(auth)
    except (TypeError, ValueError):
        raise EnergyError(
            "mcp_auth_invalid", "MCP authentication configuration is invalid."
        ) from None
    if (
        value.scheme not in _SUPPORTED_SCHEMES
        or value.credential_env is not None
        or value.secret_id is not None
    ):
        raise EnergyError("mcp_auth_invalid", "MCP authentication configuration is invalid.")
    if value.scheme == "none":
        if credential is not None:
            raise EnergyError(
                "credential_invalid", "No-auth MCP connections cannot store credentials."
            )
        return value
    if (
        type(credential) is not str
        or not credential.strip()
        or len(credential) > 4096
        or any(unicodedata.category(character) == "Cc" for character in credential)
    ):
        raise EnergyError("credential_invalid", "MCP connection credential is invalid.")
    return value


def _ephemeral_auth(auth: AuthConfig) -> AuthConfig:
    return AuthConfig(scheme=auth.scheme, header=auth.header)


def _validate_url_credential(url: str, credential: str | None) -> None:
    if type(url) is not str or not url or len(url.encode("utf-8")) > 2048:
        raise EnergyError("mcp_target_invalid", _SAFE_ERROR_MESSAGES["mcp_target_invalid"])
    if _contains_credential(url, credential):
        raise EnergyError("mcp_target_invalid", _SAFE_ERROR_MESSAGES["mcp_target_invalid"])


def _contains_credential(value: Any, credential: str | None) -> bool:
    if credential is None:
        return False
    if isinstance(value, str):
        if credential in value:
            return True
        try:
            from urllib.parse import unquote

            return credential in unquote(value)
        except Exception:
            return False
    if isinstance(value, Mapping):
        return any(
            _contains_credential(str(key), credential) or _contains_credential(nested, credential)
            for key, nested in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_credential(item, credential) for item in value)
    return False


def _validated_digest(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise EnergyError(
            "mcp_schema_digest_invalid", _SAFE_ERROR_MESSAGES["mcp_schema_digest_invalid"]
        )
    return value.lower()


def _validated_selection(value: frozenset[str]) -> frozenset[str]:
    if (
        type(value) is not frozenset
        or not 1 <= len(value) <= _MAX_SELECTED_TOOLS
        or any(
            type(name) is not str
            or not name.strip()
            or name != name.strip()
            or len(name) > _MAX_TOOL_NAME_LENGTH
            or any(unicodedata.category(char) == "Cc" for char in name)
            for name in value
        )
    ):
        raise EnergyError("mcp_selection_invalid", _SAFE_ERROR_MESSAGES["mcp_selection_invalid"])
    return value


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise EnergyError("mcp_review_invalid", f"MCP {field_name} is invalid.")
    return value


def _action(value: Any) -> Action:
    if isinstance(value, Action):
        return value
    if type(value) is not str:
        raise ValueError
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    try:
        return _ACTION_ALIASES[normalized]
    except KeyError:
        raise ValueError from None


def _string_list(value: Any, *, maximum_items: int, maximum_length: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
    else:
        raise ValueError
    if len(values) > maximum_items:
        raise ValueError
    return [_bounded_text(item, "review metadata", maximum_length) for item in values]


def _review_metadata(raw: Mapping[str, Any], *, toolkit_id: str, credential: str | None) -> Json:
    if set(raw) - _REVIEW_FIELDS or _contains_credential(raw, credential):
        raise EnergyError("mcp_review_invalid", "MCP review metadata is invalid.")
    if raw.get("reviewed") is not True or "kind" not in raw or "unit" not in raw:
        raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])
    if "actions" in raw:
        raw_actions = raw["actions"]
        if isinstance(raw_actions, (str, Action)):
            actions_input = [raw_actions]
        elif isinstance(raw_actions, Sequence) and not isinstance(
            raw_actions, (str, bytes, bytearray)
        ):
            actions_input = list(raw_actions)
        else:
            raise EnergyError("mcp_review_invalid", "MCP review metadata is invalid.")
    elif "action" in raw:
        actions_input = [raw["action"]]
    else:
        raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])
    try:
        actions = sorted({_action(action).value for action in actions_input})
        kind = DataKind(str(raw["kind"]).strip().lower().replace("_", "-")).value
        unit = _bounded_text(raw["unit"], "unit", 128)
        capabilities = _string_list(
            raw.get("capabilities", ()), maximum_items=32, maximum_length=128
        )
        assumptions = _string_list(raw.get("assumptions", ()), maximum_items=32, maximum_length=512)
        idempotent = raw.get("idempotent", True)
        if type(idempotent) is not bool:
            raise ValueError
        source = _bounded_text(raw.get("source", f"mcp:{toolkit_id}"), "source", 256)
        timezone = _bounded_text(raw.get("timezone", "UTC"), "timezone", 64)
        ZoneInfo(timezone)
        resolution_value = raw.get("resolution")
        resolution = (
            None if resolution_value is None else _bounded_text(resolution_value, "resolution", 128)
        )
        quality = _bounded_text(raw.get("quality", "unknown"), "quality", 128)
    except (TypeError, ValueError):
        raise EnergyError("mcp_review_invalid", "MCP review metadata is invalid.") from None
    if not actions:
        raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])
    return {
        "reviewed": True,
        "actions": actions,
        "kind": kind,
        "unit": unit,
        "capabilities": capabilities,
        "idempotent": idempotent,
        "source": source,
        "timezone": timezone,
        "resolution": resolution,
        "assumptions": assumptions,
        "quality": quality,
    }


def _validated_reviews(
    selected: frozenset[str],
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    toolkit_id: str,
    credential: str | None,
) -> dict[str, Json]:
    if not isinstance(metadata, Mapping):
        raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])
    reviews: dict[str, Json] = {}
    for name in sorted(selected):
        raw = metadata.get(name)
        if not isinstance(raw, Mapping):
            raise EnergyError("mcp_review_required", _SAFE_ERROR_MESSAGES["mcp_review_required"])
        reviews[name] = _review_metadata(raw, toolkit_id=toolkit_id, credential=credential)
    return reviews


def _definition(url: str, digest: str, reviews: Mapping[str, Json]) -> Json:
    value: Json = {
        "format_version": _DEFINITION_FORMAT,
        "url": url,
        "schema_digest": digest,
        "selected_reviews": [{"name": name, "metadata": reviews[name]} for name in sorted(reviews)],
    }
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    if len(encoded) > _MAX_DEFINITION_BYTES:
        raise EnergyError("mcp_definition_invalid", "MCP connection definition is too large.")
    return value


def _read_definition(
    account: ConnectedAccount,
) -> tuple[Json, frozenset[str], dict[str, Json]]:
    try:
        if set(account.settings) != {"managed_mcp"}:
            raise ValueError
        value = account.settings["managed_mcp"]
        if not isinstance(value, dict) or set(value) != {
            "format_version",
            "url",
            "schema_digest",
            "selected_reviews",
        }:
            raise ValueError
        if (
            type(value["format_version"]) is not int
            or value["format_version"] != _DEFINITION_FORMAT
        ):
            raise ValueError
        url = _bounded_text(value["url"], "URL", 2048)
        digest = _validated_digest(value["schema_digest"])
        selected_reviews = value["selected_reviews"]
        if (
            not isinstance(selected_reviews, list)
            or not 1 <= len(selected_reviews) <= _MAX_SELECTED_TOOLS
        ):
            raise ValueError
        selected_names: set[str] = set()
        reviews: dict[str, Json] = {}
        for entry in selected_reviews:
            if not isinstance(entry, dict) or set(entry) != {"name", "metadata"}:
                raise ValueError
            name = _bounded_text(entry["name"], "tool name", _MAX_TOOL_NAME_LENGTH)
            if name != name.strip() or name in selected_names:
                raise ValueError
            metadata = entry["metadata"]
            if not isinstance(metadata, dict):
                raise ValueError
            normalized = _review_metadata(metadata, toolkit_id=account.toolkit, credential=None)
            if normalized != metadata:
                raise ValueError
            selected_names.add(name)
            reviews[name] = normalized
        normalized_definition = _definition(url, digest, reviews)
        if normalized_definition != value:
            raise ValueError
    except (EnergyError, TypeError, ValueError):
        raise EnergyError(
            "mcp_definition_invalid", "MCP connection definition is invalid."
        ) from None
    return normalized_definition, frozenset(selected_names), reviews


def _stored_auth(account: ConnectedAccount) -> AuthConfig:
    if account.auth.secret_id not in {None, account.id} or account.auth.credential_env is not None:
        raise EnergyError("mcp_auth_invalid", "MCP authentication configuration is invalid.")
    auth = AuthConfig(scheme=account.auth.scheme, header=account.auth.header)
    if auth.scheme not in _SUPPORTED_SCHEMES:
        raise EnergyError("mcp_auth_invalid", "MCP authentication configuration is invalid.")
    return auth


def _require_account_identity(account: ConnectedAccount, user_id: str, workspace_id: str) -> None:
    if account.user_id != user_id or account.workspace_id != workspace_id:
        raise EnergyError("account_forbidden", "MCP connection is outside this workspace.")
    if (
        not re.fullmatch(r"custom-mcp-[0-9a-f]{32}", account.toolkit)
        or account.toolkit != account.id
    ):
        raise EnergyError("mcp_definition_invalid", "MCP connection identity is invalid.")


def _success_outcome(account: ConnectedAccount, digest: str, selected_count: int) -> Json:
    return {
        "ok": True,
        "account": account.public(),
        "schema_digest": digest,
        "selected_tool_count": selected_count,
    }


def _recovery_result(connection_id: str, status: str, error_code: str | None) -> Json:
    result: Json = {"connection_id": connection_id, "status": status}
    if error_code is not None:
        result["error_code"] = error_code
    return result


def _safe_code(error: Exception) -> str:
    if isinstance(error, (EnergyError, MCPImportError)) and error.code in _SAFE_IMPORT_CODES:
        return error.code
    if isinstance(error, EnergyError) and error.code in {
        "mcp_auth_invalid",
        "mcp_definition_invalid",
        "mcp_review_invalid",
    }:
        return error.code
    if isinstance(error, EnergyError) and error.code in {
        "account_forbidden",
        "connection_changed",
        "connection_disabled",
        "connection_not_found",
        "connection_not_pending",
        "credential_unavailable",
        "credential_expired",
        "credential_missing",
    }:
        return error.code
    return "managed_mcp_unavailable"


def _public_import_error(error: MCPImportError) -> EnergyError:
    code = error.code if error.code in _SAFE_IMPORT_CODES else "managed_mcp_unavailable"
    message = _SAFE_ERROR_MESSAGES.get(code, "Managed MCP operation is unavailable.")
    return EnergyError(code, message)
