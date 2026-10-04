from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from .activity import (
    ExecutionEntry,
    ExecutionFailure,
    ExecutionLogPage,
    ExecutionLogScope,
    ExecutionSuccess,
)
from .execution_log_store import ExecutionLogStore
from .job_contracts import JobListQuery, JobMetadata, JobMetadataPage, JobReadScope
from .jobs import JobError, JobManager, SimulationOperation
from .models import (
    Action,
    Asset,
    ConnectedAccount,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Session,
    Site,
    Tool,
)
from .registry import Registry
from .resilience import ReadTransport
from .workbench import Workbench

BeforeHook = Callable[[Tool, Json, Session], Json]
AfterHook = Callable[[Tool, EnergyResult, Session], EnergyResult]
SchemaHook = Callable[[Json], Json]


class EnergyAgent:
    def __init__(
        self,
        registry: Registry,
        root: Path,
        *,
        accounts: list[ConnectedAccount] | None = None,
        sites: list[Site] | None = None,
        assets: list[Asset] | None = None,
        http: httpx.AsyncClient | None = None,
        auth_store: Any = None,
        bindings: list[Any] | None = None,
        defer_unknown_bindings: bool = False,
        calendar_clock: Callable[[], datetime] | None = None,
    ):
        self._jobs: JobManager | None = None
        self._job_task: asyncio.Task[Any] | None = None
        self._job_root = root / "jobs"
        self._activity_root = root / "activity"
        self._activity_store: ExecutionLogStore | None = None
        self._activity_recording_failed = False
        self.registry = registry
        self.auth_store = auth_store
        self._closed = False
        self.calendar_clock = calendar_clock or (lambda: datetime.now(UTC))
        self.events: list[Json] = []
        self.event_sink: Callable[[Json], None] | None = None
        self.workspace_authorizer: Callable[[Session], None] | None = None
        self.workbench = Workbench(root)
        self.accounts = {a.id: a for a in accounts or []}
        self.sites = {s.id: s for s in sites or []}
        self.assets = {a.id: a for a in assets or []}
        for collection, items in (
            (self.accounts, accounts or []),
            (self.sites, sites or []),
            (self.assets, assets or []),
        ):
            if len(collection) != len(items):
                raise ValueError("Duplicate account, site or asset identifier.")
        for account in self.accounts.values():
            if account.site_id is not None:
                site = self.sites.get(account.site_id)
                if site is None or site.user_id != account.user_id:
                    raise ValueError("Account must belong to an existing site of the same user.")
        if any(a.site_id not in self.sites for a in self.assets.values()):
            raise ValueError("Asset must belong to an existing site.")
        self.read_transport = None if http else ReadTransport()
        self.http = http or httpx.AsyncClient(
            timeout=30, follow_redirects=False, transport=self.read_transport
        )
        self._owns_http = http is None
        self.before: list[BeforeHook] = []
        self.after: list[AfterHook] = []
        self.schema_hooks: list[SchemaHook] = []
        for asset in self.assets.values():
            if asset.parent_id and (
                asset.parent_id not in self.assets
                or self.assets[asset.parent_id].site_id != asset.site_id
            ):
                raise ValueError("Asset parent must belong to the same site.")
            visited = {asset.id}
            parent_id = asset.parent_id
            while parent_id:
                if parent_id in visited:
                    raise ValueError("Asset parent graph contains a cycle.")
                visited.add(parent_id)
                parent = self.assets.get(parent_id)
                if parent is None or parent.site_id != asset.site_id:
                    raise ValueError("Asset parent must belong to the same site.")
                parent_id = parent.parent_id
            for account_id in asset.account_ids:
                asset_account = self.accounts.get(account_id)
                if asset_account is None or asset_account.site_id != asset.site_id:
                    raise ValueError("Asset account must belong to the same site.")
        from .capabilities import CapabilityResolver

        self.resolver = CapabilityResolver(
            self, bindings, defer_unknown_tools=defer_unknown_bindings
        )

    @staticmethod
    def oauth_configuration_available(session: Session, account: ConnectedAccount) -> bool:
        if account.workspace_id is None or account.auth.scheme != "oauth":
            return True
        configuration_id = account.settings.get("managed_oauth_configuration_id")
        digest = account.settings.get("managed_oauth_configuration_digest")
        return bool(
            isinstance(configuration_id, str)
            and isinstance(digest, str)
            and session.managed_oauth_configurations.get(configuration_id) == digest
        )

    def credential_available(self, account: ConnectedAccount, session: Session) -> bool:
        if not self.oauth_configuration_available(session, account):
            return False
        if account.auth.scheme in {"none", "local"}:
            return True
        if account.auth.secret_id:
            try:
                return bool(
                    self.auth_store
                    and self.auth_store.credential(account.user_id, account.id, account.site_id)
                )
            except EnergyError:
                return bool(
                    self.auth_store
                    and account.auth.scheme == "oauth"
                    and self.auth_store.can_refresh(account.user_id, account.id, account.site_id)
                )
        return bool(account.auth.credential_env and os.environ.get(account.auth.credential_env))

    def _secrets(self, user_id: str | None = None) -> list[str]:
        values = [
            os.environ.get(a.auth.credential_env, "")
            for a in self.accounts.values()
            if a.auth.credential_env and (user_id is None or a.user_id == user_id)
        ]
        if self.auth_store:
            values.extend(self.auth_store.redaction_values(user_id))
        return values

    def _event(self, event: Json) -> None:
        safe = self._redact(event, self._secrets(event.get("user_id")))
        self.events.append(safe)
        del self.events[:-1000]
        if self.event_sink:
            try:
                self.event_sink(safe)
            except Exception:
                pass

    def execution_activity(
        self, scope: ExecutionLogScope, *, limit: int = 50, before: int | None = None
    ) -> ExecutionLogPage:
        if self._closed:
            raise EnergyError("activity_unavailable", "Execution history is unavailable.")
        try:
            if self._activity_store is None:
                self._activity_store = ExecutionLogStore(self._activity_root)
            page = self._activity_store.read(scope, limit=limit, before=before)
        except Exception as exc:
            raise EnergyError("activity_unavailable", "Execution history is unavailable.") from exc
        if self._activity_recording_failed:
            return page.model_copy(update={"recording_status": "unavailable"})
        return page

    def job_history(
        self, scope: JobReadScope, query: JobListQuery, *, adopt_managed_legacy: bool = False
    ) -> JobMetadataPage:
        if self._closed:
            raise EnergyError("job_history_unavailable", "Job history is unavailable.")
        try:
            if self._jobs is None:
                self._jobs = JobManager(self._job_root)
            if (
                adopt_managed_legacy
                and scope.workspace_id is not None
                and scope.access_mode == "hosted"
                and self.workspace_authorizer is not None
            ):
                for site_id in scope.site_ids:
                    if site_id is not None:
                        self._jobs.adopt_legacy_hosted_workspace(site_id, scope.workspace_id)
            return self._jobs.metadata_page(scope, query)
        except JobError as exc:
            if exc.code == "invalid_cursor":
                raise EnergyError(exc.code, exc.message) from exc
            raise EnergyError("job_history_unavailable", "Job history is unavailable.") from exc
        except Exception as exc:
            raise EnergyError("job_history_unavailable", "Job history is unavailable.") from exc

    def job_metadata(self, job_id: str, scope: JobReadScope) -> JobMetadata:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise EnergyError("job_forbidden", "Job is outside the current scope.")
        if self._closed:
            raise EnergyError("job_history_unavailable", "Job history is unavailable.")
        try:
            if self._jobs is None:
                self._jobs = JobManager(self._job_root)
            return self._jobs.metadata(job_id, scope)
        except JobError as exc:
            if exc.code == "job_history_unavailable":
                raise EnergyError(exc.code, exc.message) from exc
            raise EnergyError("job_forbidden", "Job is outside the current scope.") from exc
        except Exception as exc:
            raise EnergyError("job_history_unavailable", "Job history is unavailable.") from exc

    def _record_execution(self, session: Session, event: Json) -> None:
        # Only this allowlist reaches disk; arguments, results and messages do not.
        try:
            safe = self._redact(event, self._session_secrets(session))
            outcome = (
                ExecutionSuccess(kind="success", data_kind=DataKind(safe["kind"]))
                if safe["ok"]
                else ExecutionFailure(
                    kind="failure",
                    error_code=safe["error_code"]
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", safe["error_code"])
                    else "execution_failed",
                )
            )
            entry = ExecutionEntry(
                execution_id=safe["execution_id"],
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                key_id=session.workspace_key_id,
                session_id=session.id,
                site_id=session.site_id,
                account_id=safe.get("account_id"),
                access_mode=session.access_mode,
                recorded_at=datetime.now(UTC),
                tool=(safe["tool"] if safe["tool"] in self.registry.tools else "unknown_tool"),
                duration_ms=safe["latency_ms"],
                outcome=outcome,
            )
            if self._activity_store is None:
                self._activity_store = ExecutionLogStore(self._activity_root)
            entry = ExecutionEntry.model_validate_json(
                json.dumps(
                    self._redact(entry.model_dump(mode="json"), self._session_secrets(session))
                )
            )
            self._activity_store.append(entry)
        except Exception:
            self._activity_recording_failed = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._jobs:
            await self._jobs.aclose()
        if self._job_task:
            await asyncio.gather(self._job_task, return_exceptions=True)
        if self._owns_http:
            await self.http.aclose()
        if self._activity_store:
            self._activity_store.close()
        if self.auth_store:
            self.auth_store.close()

    async def job(
        self,
        session: Session,
        operation: str,
        *,
        job_id: str | None = None,
        simulation: str | None = None,
        arguments: Json | None = None,
    ) -> Json:
        """Use bounded numerical jobs under the current gateway scope and policy."""
        tools = {
            "heat_loss": "engineering.calculate_heat_loss",
            "power_flow": "engineering.run_power_flow",
            "battery": "engineering.schedule_battery_charging",
            "solar": "engineering.estimate_solar_generation",
            "network_power_flow": "pypsa.power_flow",
            "network_dispatch": "pypsa.optimize_dispatch",
        }
        try:
            self._scope(session)
            if self._closed:
                raise EnergyError("agent_closed", "The agent is closed.")
            if operation not in {
                "submit",
                "list",
                "status",
                "result",
                "cancel",
                "delete",
                "resume",
            }:
                raise EnergyError("invalid_operation", "Unknown job operation.")
            if operation != "submit" and Action.READ not in session.allowed_actions:
                raise EnergyError("policy_denied", "Reading job state requires read permission.")
            if operation == "submit":
                if simulation not in tools:
                    raise EnergyError(
                        "invalid_operation", "Select an available numerical simulation."
                    )
                name = tools[simulation]
                self.get_tool(session, name)
                tool = self.registry.get(name)
                if not tool.actions <= session.allowed_actions:
                    raise EnergyError(
                        "policy_denied", "Simulation is outside the session action policy."
                    )
                if self.before or self.after:
                    raise EnergyError(
                        "job_hooks_unsupported", "Jobs cannot bypass configured execution hooks."
                    )
                self._validate(tool, arguments or {})
                if any(find_spec(dep) is None for dep in tool.dependencies):
                    raise EnergyError(
                        "dependency_missing", "The simulation dependency is unavailable."
                    )
            if self._jobs is None:
                self._jobs = JobManager(self._job_root)
            manager = self._jobs
            if (
                session.workspace_id is not None
                and session.site_id is not None
                and session.access_mode == "hosted"
                and self.workspace_authorizer is not None
            ):
                manager.adopt_legacy_hosted_workspace(session.site_id, session.workspace_id)
            if operation == "submit":
                record = manager.submit(
                    session.user_id,
                    session.id,
                    SimulationOperation(simulation or ""),
                    arguments or {},
                    site_id=session.site_id,
                    access_mode=session.access_mode,
                    workspace_id=session.workspace_id,
                )
                if self._job_task is None or self._job_task.done():
                    self._job_task = asyncio.create_task(manager.run_pending())
                result: Json = {"job": record.as_dict()}
            elif operation == "list":
                result = {
                    "jobs": [
                        item.as_dict()
                        for item in manager.list(session.user_id, session.id)
                        if item.site_id == session.site_id
                        and item.workspace_id == session.workspace_id
                        and item.access_mode == session.access_mode
                        and (
                            session.toolkits is None
                            or self.registry.get(tools[item.operation.value]).toolkit
                            in session.toolkits
                        )
                    ]
                }
            else:
                if not job_id:
                    raise EnergyError("job_required", "Provide a job identifier.")
                scope = manager.resume_scope(
                    job_id, session.user_id, access_mode=session.access_mode
                )
                if scope["site_id"] != session.site_id:
                    raise EnergyError("site_forbidden", "Job belongs to a different site.")
                record = manager.status(job_id, session.user_id, scope["session_id"] or "")
                if record.workspace_id != session.workspace_id:
                    raise EnergyError(
                        "workspace_forbidden", "Job belongs to a different workspace."
                    )
                self.get_tool(session, tools[record.operation.value])
                if operation == "resume":
                    result = {"scope": scope}
                elif operation == "result":
                    result = manager.result(job_id, session.user_id, session.id)
                elif operation == "delete":
                    manager.delete(job_id, session.user_id, session.id)
                    result = {"deleted": True}
                else:
                    method = manager.cancel if operation == "cancel" else manager.status
                    result = {"job": method(job_id, session.user_id, session.id).as_dict()}
            self._event(
                {
                    "event": "job",
                    "user_id": session.user_id,
                    "session_id": session.id,
                    "operation": operation,
                    "ok": True,
                }
            )
            return self._redact({"ok": True, **result}, self._session_secrets(session))
        except (EnergyError, JobError) as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    def session(self, user_id: str, site_id: str | None = None, **kwargs: Any) -> Session:
        session = Session(user_id=user_id, site_id=site_id, **kwargs)
        self._scope(session)
        return session

    def _scope(self, session: Session) -> None:
        if session.workspace_key_id is not None:
            if self.workspace_authorizer is None:
                raise EnergyError("workspace_forbidden", "Workspace authorization is unavailable.")
            self.workspace_authorizer(session)
        if session.site_id is not None:
            site = self.sites.get(session.site_id)
            if site is None or site.user_id != session.resource_user_id:
                raise EnergyError("site_forbidden", "Site is outside this user's scope.")
        if session.toolkits is not None and not session.toolkits <= self.registry.toolkits.keys():
            raise EnergyError("unknown_toolkit", "Session includes an unknown toolkit.")

    def _session_secrets(self, session: Session) -> list[str]:
        values = self._secrets(session.user_id)
        if session.resource_user_id != session.user_id:
            values.extend(self._secrets(session.resource_user_id))
        return values

    @staticmethod
    def account_granted(session: Session, account: ConnectedAccount) -> bool:
        return session.connection_grants is None or account.id in session.connection_grants

    @staticmethod
    def _tool_visible(session: Session, tool: Tool) -> bool:
        return (session.toolkits is None or tool.toolkit in session.toolkits) and (
            session.access_mode == "local"
            or tool.resource_scope in {"public", "account", "session"}
        )

    def get_tool(self, session: Session, name: str) -> Json:
        self._scope(session)
        tool = self.registry.get(name)
        if not self._tool_visible(session, tool):
            raise EnergyError("tool_forbidden", "Tool is outside this session's resource scope.")
        data = tool.public()
        data["capabilities"] = sorted(
            set(data["capabilities"])
            | set(self.resolver.scoped_capabilities(session).get(name, []))
        )
        for hook in self.schema_hooks:
            data = hook(copy.deepcopy(data))
        secrets = self._session_secrets(session)
        return self._redact(data, secrets)

    def search(self, session: Session, query: str, limit: int = 5) -> list[Json]:
        self._scope(session)
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 10:
            raise EnergyError("invalid_search", "Provide a query and limit between 1 and 10.")
        matches = self.registry.search(
            query,
            session.toolkits,
            max(limit, 10),
            scoped_capabilities=self.resolver.scoped_capabilities(session),
            allowed_tool_names={
                tool.name
                for tool in self.registry.tools.values()
                if self._tool_visible(session, tool)
            },
        )

        def availability(tool: Tool) -> int:
            if any(find_spec(dependency) is None for dependency in tool.dependencies):
                return 0
            try:
                account = self._account(session, tool.toolkit)
                if account is None and tool.resource_scope == "account":
                    return 0
                return 1 if account is None or self.credential_available(account, session) else 0
            except EnergyError:
                return 0

        matches.sort(key=lambda tool: -availability(tool))
        results = [
            {**self.get_tool(session, t.name), "connection_available": bool(availability(t))}
            for t in matches[:limit]
        ]
        self._event(
            {
                "event": "search",
                "session_id": session.id,
                "user_id": session.user_id,
                "tools": [t["name"] for t in results],
            }
        )
        return results

    def _sync_connections(self, user_id: str, workspace_id: str | None = None) -> None:
        if self.auth_store:
            changed = False
            current = [
                account
                for account in self.auth_store.workspace_accounts(user_id, workspace_id)
                if account.workspace_id is None or account.site_id is not None
            ]
            current_ids = {account.id for account in current}
            for account_id, account in list(self.accounts.items()):
                if (
                    account.user_id == user_id
                    and account.workspace_id == workspace_id
                    and account.auth.secret_id
                    and account_id not in current_ids
                ):
                    del self.accounts[account_id]
                    changed = True
            for account in current:
                if account.site_id is not None:
                    site = self.sites.get(account.site_id)
                    if site is None or site.user_id != user_id:
                        raise EnergyError(
                            "site_forbidden", "Stored connection has an invalid site scope."
                        )
                if self.accounts.get(account.id) != account:
                    changed = True
                self.accounts[account.id] = account
            if changed:
                self.resolver.refresh_account_bindings()

    def connections(self, session: Session) -> list[Json]:
        self._scope(session)
        self._sync_connections(session.resource_user_id, session.workspace_id)
        visible_toolkits = {item["id"] for item in self.catalogue(session)}
        return [
            a.public()
            for a in self.accounts.values()
            if a.user_id == session.resource_user_id
            and a.workspace_id == session.workspace_id
            and self.account_granted(session, a)
            and (session.site_id is None or a.site_id == session.site_id)
            and (session.toolkits is None or a.toolkit in session.toolkits)
            and (session.access_mode == "local" or a.toolkit in visible_toolkits)
        ]

    def _account(
        self, session: Session, toolkit: str, account_id: str | None = None
    ) -> ConnectedAccount | None:
        self._scope(session)
        self._sync_connections(session.resource_user_id, session.workspace_id)
        selected = account_id or session.account_ids.get(toolkit)
        candidates = [
            a
            for a in self.accounts.values()
            if a.toolkit == toolkit
            and a.user_id == session.resource_user_id
            and a.workspace_id == session.workspace_id
            and self.account_granted(session, a)
            and a.enabled
            and a.state == "active"
            and self.oauth_configuration_available(session, a)
            and (session.site_id is None or a.site_id == session.site_id)
        ]
        if selected:
            account = next((a for a in candidates if a.id == selected), None)
            if account is None:
                raise EnergyError(
                    "account_forbidden", "Account is unavailable in this user/site scope."
                )
            return account
        if len(candidates) > 1:
            raise EnergyError("ambiguous_account", "Select an account ID; multiple accounts match.")
        if candidates:
            return candidates[0]
        if self.registry.toolkits[toolkit].auth_required:
            raise EnergyError(
                "connection_required", "Configure an account locally before executing."
            )
        return None

    def select_account(self, session: Session, toolkit: str, account_id: str) -> Json:
        if toolkit not in self.registry.toolkits:
            raise EnergyError("unknown_toolkit", "Toolkit does not exist.")
        if session.toolkits is not None and toolkit not in session.toolkits:
            raise EnergyError("tool_forbidden", "Toolkit is outside this session.")
        if session.access_mode == "hosted" and not any(
            tool.toolkit == toolkit and self._tool_visible(session, tool)
            for tool in self.registry.tools.values()
        ):
            raise EnergyError("tool_forbidden", "Toolkit is outside this session's resource scope.")
        account = self._account(session, toolkit, account_id)
        assert account is not None
        session.account_ids[toolkit] = account.id
        return account.public()

    @staticmethod
    def _validate(tool: Tool, arguments: Json) -> None:
        errors = sorted(
            Draft202012Validator(tool.input_schema, format_checker=FormatChecker()).iter_errors(
                arguments
            ),
            key=lambda e: str(e.path),
        )
        if errors:
            # Never echo argument values, which may contain accidental credentials.
            path = ".".join(str(p) for p in errors[0].path) or "arguments"
            raise EnergyError(
                "invalid_arguments", f"Schema validation failed at {path} ({errors[0].validator})."
            )

    @staticmethod
    def _redact(value: Any, secrets: list[str]) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [EnergyAgent._redact(v, secrets) for v in value]
        if isinstance(value, dict):
            return {
                EnergyAgent._redact(k, secrets): EnergyAgent._redact(v, secrets)
                for k, v in value.items()
            }
        return value

    async def execute(
        self,
        session: Session,
        name: str,
        arguments: Json,
        account_id: str | None = None,
        persist: bool = False,
        input_artifacts: list[str] | None = None,
        expected_kind: Any = None,
        expected_unit: str | None = None,
        asset_id: str | None = None,
        expected_resolution: str | None = None,
        expected_quantity_shape: str | None = None,
        max_age_seconds: int | None = None,
        expected_arguments: Json | None = None,
    ) -> Json:
        execution_id = uuid4().hex
        started = time.monotonic()
        all_secrets = self._session_secrets(session)
        event: Json = {
            "event": "execution",
            "execution_id": execution_id,
            "user_id": session.user_id,
            "workspace_id": session.workspace_id,
            "key_id": session.workspace_key_id,
            "session_id": session.id,
            "tool": name,
            "ok": False,
        }
        try:
            self.get_tool(session, name)
            tool = self.registry.get(name)
            if not tool.actions <= session.allowed_actions:
                raise EnergyError(
                    "policy_denied", "Action requires explicit operator policy enablement."
                )
            args = copy.deepcopy(arguments)
            self._validate(tool, args)
            for before in self.before:
                args = before(tool, args, session)
            self._validate(tool, args)
            if any(args.get(key) != value for key, value in (expected_arguments or {}).items()):
                raise EnergyError(
                    "binding_arguments_changed",
                    "Execution hooks changed a fixed capability argument.",
                )
            account = self._account(session, tool.toolkit, account_id)
            event["account_id"] = account.id if account else None
            if tool.resource_scope == "account" and account is None:
                raise EnergyError("connection_required", "An owned connection is required.")
            if account and account.workspace_id is not None:
                for argument, setting in tool.account_argument_settings.items():
                    if (
                        setting not in account.settings
                        or args.get(argument) != account.settings[setting]
                    ):
                        raise EnergyError(
                            "account_resource_forbidden",
                            "Requested resource is outside the mapped connection.",
                        )
            self._scope(session)
            credential = None
            if account and account.auth.secret_id:
                if not self.auth_store:
                    raise EnergyError(
                        "credential_missing", "Encrypted credential store is not configured."
                    )
                if (
                    account.auth.scheme == "oauth"
                    and account.expires_at
                    and account.expires_at <= datetime.now(UTC) + timedelta(seconds=60)
                ):
                    account = (
                        await self.auth_store.refresh_managed(
                            account.user_id, session.workspace_id, account.id
                        )
                        if session.workspace_id is not None
                        else await self.auth_store.refresh(account.user_id, account.id)
                    )
                    self.accounts[account.id] = account
                self._scope(session)
                credential = self.auth_store.credential(
                    account.user_id, account.id, session.site_id
                )
            if account and account.auth.credential_env:
                credential = os.environ.get(account.auth.credential_env)
                if not credential:
                    raise EnergyError(
                        "credential_missing", "Configured secret is absent from the environment."
                    )
            if account and account.auth.scheme not in {"none", "local"} and not credential:
                raise EnergyError(
                    "credential_missing", "This account requires a local credential reference."
                )
            if len(input_artifacts or []) > 10:
                raise EnergyError("too_many_inputs", "At most ten input artifacts may be linked.")
            inputs = [(a, self.workbench.read(session, a)) for a in input_artifacts or []]
            if asset_id:
                asset = self.assets.get(asset_id)
                if (
                    asset is None
                    or self.sites[asset.site_id].user_id != session.resource_user_id
                    or (session.site_id and asset.site_id != session.site_id)
                ):
                    raise EnergyError("asset_forbidden", "Asset is outside this session scope.")
                if account and (
                    account.site_id != asset.site_id
                    or (asset.account_ids and account.id not in asset.account_ids)
                ):
                    raise EnergyError("asset_forbidden", "Account is not connected to this asset.")
            context = ExecutionContext(
                session,
                account,
                credential,
                self.http,
                self.workbench,
                asset_id=asset_id,
                site_id=session.site_id
                or (
                    self.assets[asset_id].site_id
                    if asset_id
                    else account.site_id
                    if account
                    else None
                ),
            )
            self._scope(session)
            result = await self.registry.handlers[name](args, context)
            if (
                expected_quantity_shape is not None
                and result.quantity_shape != expected_quantity_shape
            ):
                raise EnergyError(
                    "binding_semantics_changed",
                    "Telemetry quantity shape differs from its reviewed binding.",
                )
            if max_age_seconds is not None:
                observed_at = result.time_end or result.time_start
                if observed_at is None:
                    raise EnergyError(
                        "freshness_unavailable",
                        "Provider supplied no observation timestamp; current telemetry is unavailable.",
                    )
                age = (self.calendar_clock() - observed_at).total_seconds()
                if age > max_age_seconds or age < -120:
                    raise EnergyError(
                        "stale_telemetry", "Observation is outside the allowed freshness window."
                    )
                result.provenance.append(
                    {"observation_age_seconds": max(0, age), "max_age_seconds": max_age_seconds}
                )
            if expected_kind is not None and result.kind != expected_kind:
                raise EnergyError(
                    "binding_semantics_changed",
                    "Provider returned a different measurement kind from the reviewed binding.",
                )
            if expected_unit is not None and result.unit != expected_unit:
                raise EnergyError(
                    "binding_semantics_changed",
                    "Provider returned a different unit from the reviewed binding.",
                )
            if asset_id:
                result.asset_id = asset_id
            result.provider = result.provider or tool.toolkit
            result.site_id = session.site_id or (
                self.assets[asset_id].site_id
                if asset_id
                else account.site_id
                if account
                else result.site_id
            )
            result.original_unit = result.original_unit or result.unit
            if len(inputs) == 1:
                input_result = inputs[0][1]
                result.site_id = result.site_id or input_result.site_id
                result.asset_id = result.asset_id or input_result.asset_id
                result.time_start = result.time_start or input_result.time_start
                result.time_end = result.time_end or input_result.time_end
            result.provenance.append(
                {"site_id": session.site_id, "provider": tool.toolkit, "tool_version": tool.version}
            )
            original_kind = result.kind
            original_scope = (
                result.site_id,
                result.asset_id,
                result.original_unit,
                result.quantity_shape,
                result.time_start,
                result.time_end,
            )
            original_provenance = copy.deepcopy(result.provenance)
            for after in self.after:
                result = after(tool, result, session)
            if original_kind.value != "metered" and result.kind.value == "metered":
                raise EnergyError(
                    "invalid_data_kind", "Execution hooks cannot relabel derived data as metered."
                )
            # Boundary check after hooks, before persisting any result.
            result = EnergyResult.model_validate(result.model_dump())
            if (
                result.site_id,
                result.asset_id,
                result.original_unit,
                result.quantity_shape,
                result.time_start,
                result.time_end,
            ) != original_scope:
                raise EnergyError(
                    "result_scope_changed",
                    "Execution hooks changed source scope, units, quantity shape or observation timestamps.",
                )
            if result.provenance[: len(original_provenance)] != original_provenance:
                raise EnergyError(
                    "provenance_changed",
                    "Execution hooks removed or changed original source evidence.",
                )
            if (expected_kind is not None and result.kind != expected_kind) or (
                expected_unit is not None and result.unit != expected_unit
            ):
                raise EnergyError(
                    "binding_semantics_changed",
                    "Execution hooks changed the reviewed measurement kind or unit.",
                )
            if expected_resolution is not None:
                import pandas as pd

                try:
                    matches_resolution = result.resolution is not None and (
                        pd.to_timedelta(result.resolution) == pd.to_timedelta(expected_resolution)
                    )
                except (TypeError, ValueError):
                    matches_resolution = result.resolution == expected_resolution
                if not matches_resolution:
                    raise EnergyError(
                        "binding_semantics_changed",
                        "Provider returned a different resolution from the reviewed binding.",
                    )
            all_secrets = self._session_secrets(session)
            result = EnergyResult.model_validate(
                self._redact(result.model_dump(mode="json"), all_secrets)
            )
            result.provenance.extend(
                {
                    "artifact_id": a,
                    "input_kind": r.kind.value,
                    "source": r.source,
                    "unit": r.unit,
                    "provenance": r.provenance,
                }
                for a, r in inputs
            )
            result.provenance.append(
                {
                    "execution_id": execution_id,
                    "tool": name,
                    "toolkit": tool.toolkit,
                    "session_id": session.id,
                    "account_id": account.id if account else None,
                }
            )
            data = result.model_dump(mode="json")
            if persist:
                data["data"] = self.workbench.persist(session, result)
            else:
                data = self.workbench.compact(session, result)
            event.update(
                ok=True, account_id=account.id if account else None, kind=result.kind.value
            )
            return {"ok": True, "execution_id": execution_id, "result": data}
        except EnergyError as exc:
            event["error_code"] = exc.code
            return {
                "ok": False,
                "execution_id": execution_id,
                "error": {
                    "code": exc.code,
                    "message": self._redact(exc.message, all_secrets),
                    "retryable": exc.retryable,
                },
            }
        except asyncio.CancelledError:
            event["error_code"] = "cancelled"
            raise
        except httpx.TimeoutException:
            event["error_code"] = "timeout"
            return {
                "ok": False,
                "execution_id": execution_id,
                "error": {
                    "code": "timeout",
                    "message": "Provider request timed out.",
                    "retryable": True,
                },
            }
        except httpx.HTTPStatusError as exc:
            event["error_code"] = "provider_http_error"
            code = exc.response.status_code
            return {
                "ok": False,
                "execution_id": execution_id,
                "error": {
                    "code": "provider_http_error",
                    "message": f"Provider returned HTTP {code}.",
                    "retryable": code == 429 or code >= 500,
                },
            }
        except Exception:
            # Raw exception strings often contain authenticated URLs or process stderr.
            return {
                "ok": False,
                "execution_id": execution_id,
                "error": {
                    "code": "execution_failed",
                    "message": "Provider returned invalid data or execution failed. Check local configuration and provider availability.",
                    "retryable": False,
                },
            }
        finally:
            if not event["ok"]:
                event.setdefault("error_code", "execution_failed")
            event["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._event(self._redact(event, self._session_secrets(session)))
            self._record_execution(session, event)

    async def multi_execute(self, session: Session, calls: list[Json]) -> list[Json]:
        if not 1 <= len(calls) <= 20:
            raise EnergyError("invalid_batch", "A batch must contain between 1 and 20 calls.")
        # Ordered batches avoid racing local state. Each result carries its own failure.
        outputs = []
        from .server import ExecutionCall

        for raw in calls:
            try:
                call = ExecutionCall.model_validate(raw).model_dump()
            except ValueError:
                raise EnergyError(
                    "invalid_batch", "Batch calls must match the execution contract."
                ) from None
            outputs.append(
                await self.execute(
                    session,
                    call["tool"],
                    call.get("arguments", {}),
                    call.get("account_id"),
                    call.get("persist", False),
                    call.get("input_artifacts"),
                    asset_id=call.get("asset_id"),
                )
            )
        return outputs

    def catalogue(self, session: Session) -> list[Json]:
        self._scope(session)
        visible_toolkits = {
            tool.toolkit
            for tool in self.registry.tools.values()
            if self._tool_visible(session, tool)
        }
        return [
            t.model_dump(mode="json")
            for t in self.registry.toolkits.values()
            if (session.toolkits is None or t.id in session.toolkits)
            and (session.access_mode == "local" or t.id in visible_toolkits)
        ]

    def write_manifests(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for toolkit in self.registry.toolkits.values():
            manifest = {
                "toolkit": toolkit.model_dump(mode="json"),
                "tools": [
                    t.public() for t in self.registry.tools.values() if t.toolkit == toolkit.id
                ],
            }
            (path / f"{toolkit.id}.json").write_text(json.dumps(manifest, indent=2) + "\n")
