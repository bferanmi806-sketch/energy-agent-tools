from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from .models import (
    Asset,
    ConnectedAccount,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Session,
    Site,
    Tool,
)
from .registry import Registry
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
    ):
        self.registry = registry
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
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._owns_http = http is None
        self.before: list[BeforeHook] = []
        self.after: list[AfterHook] = []
        self.schema_hooks: list[SchemaHook] = []

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def session(self, user_id: str, site_id: str | None = None, **kwargs: Any) -> Session:
        session = Session(user_id=user_id, site_id=site_id, **kwargs)
        self._scope(session)
        return session

    def _scope(self, session: Session) -> None:
        if session.site_id is not None:
            site = self.sites.get(session.site_id)
            if site is None or site.user_id != session.user_id:
                raise EnergyError("site_forbidden", "Site is outside this user's scope.")
        if session.toolkits is not None and not session.toolkits <= self.registry.toolkits.keys():
            raise EnergyError("unknown_toolkit", "Session includes an unknown toolkit.")

    def get_tool(self, session: Session, name: str) -> Json:
        self._scope(session)
        tool = self.registry.get(name)
        if session.toolkits is not None and tool.toolkit not in session.toolkits:
            raise EnergyError("tool_forbidden", "Tool is outside this session's toolkit scope.")
        data = tool.public()
        for hook in self.schema_hooks:
            data = hook(copy.deepcopy(data))
        secrets = [
            os.environ.get(a.auth.credential_env, "")
            for a in self.accounts.values()
            if a.auth.credential_env
        ]
        return self._redact(data, secrets)

    def search(self, session: Session, query: str, limit: int = 5) -> list[Json]:
        self._scope(session)
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 10:
            raise EnergyError("invalid_search", "Provide a query and limit between 1 and 10.")
        return [
            self.get_tool(session, t.name)
            for t in self.registry.search(query, session.toolkits, limit)
        ]

    def connections(self, session: Session) -> list[Json]:
        self._scope(session)
        return [
            a.public()
            for a in self.accounts.values()
            if a.user_id == session.user_id
            and (session.site_id is None or a.site_id == session.site_id)
            and (session.toolkits is None or a.toolkit in session.toolkits)
        ]

    def _account(
        self, session: Session, toolkit: str, account_id: str | None = None
    ) -> ConnectedAccount | None:
        selected = account_id or session.account_ids.get(toolkit)
        candidates = [
            a
            for a in self.accounts.values()
            if a.toolkit == toolkit
            and a.user_id == session.user_id
            and a.enabled
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
    ) -> Json:
        execution_id = uuid4().hex
        all_secrets = [
            os.environ.get(a.auth.credential_env, "")
            for a in self.accounts.values()
            if a.auth.credential_env
        ]
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
            account = self._account(session, tool.toolkit, account_id)
            credential = None
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
            context = ExecutionContext(session, account, credential, self.http, self.workbench)
            result = await self.registry.handlers[name](args, context)
            original_kind = result.kind
            for after in self.after:
                result = after(tool, result, session)
            if original_kind.value != "metered" and result.kind.value == "metered":
                raise EnergyError(
                    "invalid_data_kind", "Execution hooks cannot relabel derived data as metered."
                )
            # Boundary check after hooks, before persisting any result.
            result = EnergyResult.model_validate(result.model_dump())
            all_secrets = [
                os.environ.get(a.auth.credential_env, "")
                for a in self.accounts.values()
                if a.auth.credential_env
            ]
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
            return {"ok": True, "execution_id": execution_id, "result": data}
        except EnergyError as exc:
            return {
                "ok": False,
                "execution_id": execution_id,
                "error": {
                    "code": exc.code,
                    "message": self._redact(exc.message, all_secrets),
                    "retryable": exc.retryable,
                },
            }
        except httpx.TimeoutException:
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

    async def multi_execute(self, session: Session, calls: list[Json]) -> list[Json]:
        if not 1 <= len(calls) <= 20:
            raise EnergyError("invalid_batch", "A batch must contain between 1 and 20 calls.")
        # Ordered batches avoid racing local state. Each result carries its own failure.
        outputs = []
        for call in calls:
            outputs.append(
                await self.execute(
                    session,
                    call["tool"],
                    call.get("arguments", {}),
                    call.get("account_id"),
                    call.get("persist", False),
                    call.get("input_artifacts"),
                )
            )
        return outputs

    def catalogue(self, session: Session) -> list[Json]:
        self._scope(session)
        return [
            t.model_dump(mode="json")
            for t in self.registry.toolkits.values()
            if session.toolkits is None or t.id in session.toolkits
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
