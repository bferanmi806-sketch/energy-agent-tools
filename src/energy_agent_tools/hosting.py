"""Authenticated, self-hosted HTTP and MCP transport for :class:`EnergyAgent`.

The application in this module is deliberately small at the HTTP boundary.  It
derives identity from an operator-provisioned bearer-token digest, creates
server-owned sessions, and only then delegates to the same scoped agent methods
used by the in-process SDK.  A client can choose a site, but only from the
sites attached to its authenticated principal.

``create_server`` predates this host and binds a fixed session into FastMCP
tools.  We reuse those reviewed tools behind one fixed-site mount per principal
and pass an agent proxy whose ``close`` method is a no-op.  That prevents one
MCP disconnect from closing the shared agent used by other principals and by
the REST API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AuthStore
from .capabilities import CapabilityRequest
from .connection_contracts import ConnectionSetupsResponse, OctopusConnectionRequest, octopus_setup
from .control_contracts import (
    AgentKeyAccess,
    KeyAccess,
    ManageKeyAccess,
    WorkspaceAgentKeyRequest,
    WorkspaceAssetRequest,
    WorkspaceAuthorization,
    WorkspaceAuthorizationResponse,
    WorkspaceDetails,
    WorkspaceHomeAssistantAuthorizationRequest,
    WorkspaceMapRequest,
    WorkspaceMode,
    WorkspaceOAuthCleanupRequest,
    WorkspaceOAuthCleanupResponse,
    WorkspaceOAuthCompleteRequest,
    WorkspaceSiteRequest,
)
from .control_store import ControlStore
from .managed_oauth import HomeAssistantOAuthConfiguration, ManagedHomeAssistantOAuth
from .models import ConnectedAccount, EnergyError, Json, Session, Site
from .onboarding import ConnectionHealth, probe_provider
from .runtime import EnergyAgent
from .server import create_server
from .skills import SKILLS, search_skills
from .workflows import run_skill
from .workspace_access import WorkspaceKeyScope, WorkspaceMemberGrants, WorkspaceMemberRequest

_TOKEN_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_TOKEN_BYTES = 4096


@dataclass(frozen=True)
class Principal:
    """An operator-provisioned identity accepted by an authenticated host.

    ``token_digest`` is the hexadecimal SHA-256 digest of the bearer token.
    The raw token is intentionally absent from this type and from the host
    configuration.  ``allowed_site_ids`` is copied when the host is created so
    a caller cannot mutate authorization by changing its original set.
    """

    user_id: str
    allowed_site_ids: set[str]
    token_digest: str
    expires_at: datetime | None = None
    revoked: bool = False
    token_id: str | None = None
    workspace_id: str | None = None
    key_access: KeyAccess | None = None
    workspace_mode: WorkspaceMode = "operator"
    workspace_scope: WorkspaceKeyScope | None = None

    @property
    def resource_user_id(self) -> str:
        return self.workspace_scope.resource_owner_id if self.workspace_scope else self.user_id

    def __post_init__(self) -> None:
        if not self.user_id or len(self.user_id) > 256:
            raise ValueError("Principal user_id must be non-empty and bounded.")
        if self.workspace_mode not in {"operator", "managed"}:
            raise ValueError("Principal workspace mode is invalid.")
        if self.workspace_mode == "managed" and not self.workspace_id:
            raise ValueError("Managed principals require a workspace.")
        if not _TOKEN_DIGEST.fullmatch(self.token_digest):
            raise ValueError("Principal token_digest must be a SHA-256 hexadecimal digest.")
        if not isinstance(self.revoked, bool):
            raise ValueError("Principal revoked must be a boolean.")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("Principal expires_at must be timezone-aware.")
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        if self.token_id is not None and (not self.token_id or len(self.token_id) > 256):
            raise ValueError("Principal token_id must be non-empty and bounded.")
        sites = frozenset(site for site in self.allowed_site_ids if site)
        if len(sites) != len(self.allowed_site_ids):
            raise ValueError("Principal site IDs must be non-empty.")
        if self.key_access is not None:
            access: KeyAccess = TypeAdapter(KeyAccess).validate_python(self.key_access.model_dump())
            object.__setattr__(self, "key_access", access)
            if isinstance(access, AgentKeyAccess):
                sites = sites.intersection(access.site_ids)
        object.__setattr__(self, "allowed_site_ids", sites)


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ConnectionActionRequest(_RequestModel):
    pass


class _SessionCreate(_RequestModel):
    site_id: StrictStr | None = None
    resume_job_id: StrictStr | None = None


class IdentityAsset(_RequestModel):
    id: StrictStr
    site_id: StrictStr
    name: StrictStr
    kind: StrictStr
    parent_id: StrictStr | None
    account_ids: list[StrictStr]


class IdentityResponse(_RequestModel):
    can_manage_connections: StrictBool = False
    can_manage_workspace: StrictBool = False
    workspace: WorkspaceDetails | None = None
    user_id: StrictStr
    sites: list[Site]
    assets: list[IdentityAsset]


class _JobRequest(_RequestModel):
    operation: StrictStr
    job_id: StrictStr | None = None
    simulation: StrictStr | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class _SearchRequest(_RequestModel):
    query: StrictStr = Field(min_length=1, max_length=2000)
    limit: StrictInt = Field(default=5, ge=1, le=10)


class _ExecuteRequest(_RequestModel):
    tool: StrictStr = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    account_id: StrictStr | None = None
    persist: StrictBool = False
    input_artifacts: list[StrictStr] = Field(default_factory=list, max_length=10)


class _SkillExecutionRequest(_RequestModel):
    skill_id: StrictStr = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)


class _CapabilityExecutionRequest(CapabilityRequest):
    model_config = ConfigDict(extra="forbid", strict=True)
    persist: StrictBool = False


@dataclass
class _MCPMount:
    app: ASGIApp
    manager: Any
    session: Session


class _MCPOwner:
    """Enter and exit the MCP manager's AnyIO scope in one owning task."""

    def __init__(self, manager: Any):
        self._manager = manager
        self.started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with self._manager.run():
                self.started.set_result(None)
                await self.stop.wait()
        except BaseException as exc:
            if not self.started.done():
                self.started.set_exception(exc)
            raise

    async def close(self) -> None:
        self.stop.set()
        await self.task


@dataclass
class _StoredSession:
    session: Session
    user_id: str
    site_id: str | None
    created_at: float
    last_used_at: float


@dataclass(frozen=True)
class _MCPAdmission:
    user_id: str
    site_id: str | None
    workspace_id: str | None = None
    key_id: str | None = None
    policy_revision: int | None = None


@dataclass
class _MCPResponse:
    status_code: int | None = None
    session_id: str | None = None
    registered: bool = False


class _NonClosingAgent:
    """Delegate all agent operations while making FastMCP lifespan harmless."""

    def __init__(self, agent: EnergyAgent):
        self._agent = agent

    async def close(self) -> None:
        # The host shares this agent with all mounts and REST requests.  Its
        # owner closes it after the host lifecycle ends.
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


class _BodyTooLarge(Exception):
    pass


class _BoundedReceive:
    def __init__(self, receive: Receive, max_body_bytes: int):
        self._receive = receive
        self._max_body_bytes = max_body_bytes
        self._seen = 0

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            self._seen += len(body)
            if self._seen > self._max_body_bytes:
                raise _BodyTooLarge
        return message


def _json_response(
    payload: Json, status_code: int = 200, headers: Mapping[str, str] | None = None
) -> Response:
    return JSONResponse(payload, status_code=status_code, headers=dict(headers or {}))


def _error(code: str, message: str, status_code: int) -> Response:
    # All messages here are fixed strings.  Provider exception text and request
    # values must never become an HTTP response by accident.
    return _json_response({"error": {"code": code, "message": message}}, status_code)


class _MCPDispatcher:
    def __init__(self, host: AuthenticatedHost, *, path_site: bool):
        self.host = host
        self.path_site = path_site

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        principal = self.host._scope_principal(scope)
        if principal is None:
            await _error("unauthorized", "Authentication is required.", 401)(scope, receive, send)
            return
        site_id = scope.get("path_params", {}).get("site_id") if self.path_site else None
        if site_id is None:
            site_ids = self.host._available_sites(principal)
            if len(site_ids) != 1:
                await _error("site_required", "Select one allowed site for MCP.", 409)(
                    scope, receive, send
                )
                return
            site_id = site_ids[0]
        if site_id is not None:
            site = self.host.agent.sites.get(site_id)
            if (
                site_id not in principal.allowed_site_ids
                or site is None
                or site.user_id != principal.resource_user_id
            ):
                await _error("site_forbidden", "Site is outside this user's scope.", 403)(
                    scope, receive, send
                )
                return
        mount = self.host._mounts.get((principal.user_id, site_id))
        if principal.workspace_mode == "managed":
            try:
                mount = await self.host._managed_mcp_mount(principal, site_id)
            except EnergyError as exc:
                await self.host._energy_error(exc)(scope, receive, send)
                return
        if mount is None:
            await _error("site_forbidden", "Site is outside this user's scope.", 403)(
                scope, receive, send
            )
            return
        session_headers = self.host._header_values(scope, b"mcp-session-id")
        if len(session_headers) > 1 or (session_headers and not session_headers[0]):
            await _error("invalid_mcp_session", "MCP session headers are invalid.", 400)(
                scope, receive, send
            )
            return
        request_session_id = session_headers[0] if session_headers else None
        admission, admission_error = await self.host._admit_mcp(
            principal, site_id, request_session_id
        )
        if admission is None:
            status = 404 if admission_error == "mcp_session_not_found" else 429
            message = (
                "MCP session is unavailable." if status == 404 else "Maximum MCP sessions reached."
            )
            await _error(admission_error or "mcp_session_limit", message, status)(
                scope, receive, send
            )
            return
        # FastMCP's route is /mcp.  The outer path carries the authenticated
        # site selector, which is rewritten only for the inner fixed-site app.
        inner_scope = dict(scope)
        inner_scope["path"] = "/mcp"
        inner_scope["raw_path"] = b"/mcp"
        inner_scope["root_path"] = ""
        inner_scope.pop("path_params", None)
        result = _MCPResponse()

        async def tracked_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                result.status_code = int(message["status"])
                for key, value in message.get("headers", []):
                    if key.lower() == b"mcp-session-id":
                        result.session_id = value.decode("latin-1")
                        break
                if request_session_id is None and result.session_id and result.status_code < 400:
                    result.registered = await self.host._register_mcp(admission, result.session_id)
            await send(message)

        try:
            await mount.app(inner_scope, receive, tracked_send)
        finally:
            await self.host._finish_mcp(admission, request_session_id, result, scope["method"])


class AuthenticatedHost:
    """ASGI application returned by :func:`create_host`.

    The object is an ASGI callable and exposes ``lifespan`` through its inner
    Starlette app.  Uvicorn and other ASGI servers will enter it automatically;
    tests or embedded callers should send normal ASGI lifespan events.
    """

    def __init__(
        self,
        agent: EnergyAgent,
        principals: Mapping[str, Principal],
        *,
        max_requests_per_minute: int,
        max_body_bytes: int,
        max_sessions_per_user: int,
        session_idle_timeout: float,
        max_sessions_global: int,
        close_agent_on_shutdown: bool,
        control_store: ControlStore | None = None,
        managed_workspaces: bool = False,
        managed_oauth_configurations: tuple[HomeAssistantOAuthConfiguration, ...] = (),
    ) -> None:
        if max_requests_per_minute <= 0:
            raise ValueError("max_requests_per_minute must be positive.")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive.")
        if max_sessions_per_user <= 0:
            raise ValueError("max_sessions_per_user must be positive.")
        if not isfinite(session_idle_timeout) or session_idle_timeout <= 0:
            raise ValueError("session_idle_timeout must be positive and finite.")
        if max_sessions_global <= 0:
            raise ValueError("max_sessions_global must be positive.")
        self.agent = agent
        self.max_requests_per_minute = max_requests_per_minute
        self.max_body_bytes = max_body_bytes
        self.max_sessions_per_user = max_sessions_per_user
        self.session_idle_timeout = session_idle_timeout
        self.max_sessions_global = max_sessions_global
        if managed_workspaces and (
            control_store is None or not isinstance(agent.auth_store, AuthStore)
        ):
            raise ValueError("Managed workspaces require control and encrypted auth stores.")
        if managed_workspaces:
            agent.auth_store.validate_encryption_key()
        self._managed_workspaces = managed_workspaces
        if managed_oauth_configurations and not managed_workspaces:
            raise ValueError("Managed OAuth configurations require managed hosting.")
        self._managed_oauth: ManagedHomeAssistantOAuth | None = None
        if managed_workspaces:
            assert isinstance(agent.auth_store, AuthStore)
            self._managed_oauth = ManagedHomeAssistantOAuth(
                agent.auth_store, agent.http, managed_oauth_configurations
            )
        self._operator_site_ids = frozenset(agent.sites)
        self._operator_asset_ids = frozenset(agent.assets)
        self._managed_sites: dict[str, str] = {}
        self._managed_assets: dict[str, str] = {}
        self._principals = self._validate_principals(principals, allow_empty=managed_workspaces)
        self._control_store = control_store
        if managed_workspaces:
            self.agent.workspace_authorizer = self._authorize_workspace_session
        self._sessions: dict[str, _StoredSession] = {}
        self._rate_events: dict[str, deque[float]] = {}
        self._mounts: dict[tuple[str, str | None], _MCPMount] = {}
        self._managed_mounts: dict[tuple[str, str, str, str, int], _MCPMount] = {}
        self._apps: list[Any] = []
        self._mcp_sessions: dict[str, _MCPAdmission] = {}
        self._mcp_pending: dict[str, int] = {}
        self._mcp_lock = asyncio.Lock()
        self._mount_lock = asyncio.Lock()
        self._mcp_owners: dict[tuple[str, str, str, str, int], _MCPOwner] = {}
        self._lifespan_active = False
        self._build_mcp_mounts()
        routes = [
            Route("/workspace", self._workspace, methods=["GET"]),
            Route("/workspace/members", self._workspace_members, methods=["GET", "POST"]),
            Route(
                "/workspace/members/{member_user_id}",
                self._workspace_member,
                methods=["PATCH", "DELETE"],
            ),
            Route(
                "/workspace/members/{member_user_id}/keys",
                self._workspace_member_key,
                methods=["POST"],
            ),
            Route(
                "/workspace/auth-configurations",
                self._workspace_auth_configurations,
                methods=["GET"],
            ),
            Route("/workspace/authorizations", self._workspace_authorize, methods=["POST"]),
            Route(
                "/workspace/authorizations/complete",
                self._workspace_complete_authorization,
                methods=["POST"],
            ),
            Route(
                "/workspace/authorizations/cleanup",
                self._workspace_authorization_cleanup,
                methods=["POST"],
            ),
            Route("/workspace/sites", self._workspace_sites, methods=["GET", "POST"]),
            Route("/workspace/assets", self._workspace_assets, methods=["GET", "POST"]),
            Route("/workspace/keys", self._workspace_keys, methods=["GET", "POST"]),
            Route("/workspace/keys/{key_id}", self._workspace_revoke_key, methods=["DELETE"]),
            Route("/workspace/toolkits", self._workspace_toolkits, methods=["GET"]),
            Route("/workspace/connection-setups", self._workspace_setups, methods=["GET"]),
            Route("/workspace/connections", self._workspace_connections, methods=["GET", "POST"]),
            Route(
                "/workspace/connections/{connection_id}/map", self._workspace_map, methods=["POST"]
            ),
            Route(
                "/workspace/connections/{connection_id}/verify",
                self._workspace_verify,
                methods=["POST"],
            ),
            Route(
                "/workspace/connections/{connection_id}/disconnect",
                self._workspace_disconnect,
                methods=["POST"],
            ),
            Route("/me", self._identity, methods=["GET"]),
            Route("/sessions", self._create_session, methods=["POST"]),
            Route("/sessions/{session_id}", self._delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/search", self._search, methods=["POST"]),
            Route("/sessions/{session_id}/execute", self._execute, methods=["POST"]),
            Route("/sessions/{session_id}/resolve", self._resolve, methods=["POST"]),
            Route("/sessions/{session_id}/capability", self._capability, methods=["POST"]),
            Route("/sessions/{session_id}/skills", self._skills, methods=["GET", "POST"]),
            Route("/sessions/{session_id}/skills/run", self._run_skill, methods=["POST"]),
            Route("/sessions/{session_id}/jobs", self._jobs, methods=["POST"]),
            Route("/sessions/{session_id}/connections", self._connections, methods=["GET"]),
            Route("/sessions/{session_id}/connections", self._connect_account, methods=["POST"]),
            Route(
                "/sessions/{session_id}/connections/{connection_id}/verify",
                self._verify_connection,
                methods=["POST"],
            ),
            Route(
                "/sessions/{session_id}/connections/{connection_id}/disconnect",
                self._disconnect_connection,
                methods=["POST"],
            ),
            Route(
                "/sessions/{session_id}/connection-setup", self._connection_setups, methods=["GET"]
            ),
            Route("/sessions/{session_id}/toolkits", self._toolkits, methods=["GET"]),
            Route("/sessions/{session_id}/artifacts", self._artifacts, methods=["GET"]),
            Route(
                "/sessions/{session_id}/artifacts/{artifact_id}",
                self._delete_artifact,
                methods=["DELETE"],
            ),
            Route("/mcp/{site_id:path}", _MCPDispatcher(self, path_site=True)),
            Route("/mcp", _MCPDispatcher(self, path_site=False)),
        ]

        @asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            async with AsyncExitStack() as stack:
                for mount in self._mounts.values():
                    await stack.enter_async_context(mount.manager.run())
                self._lifespan_active = True
                try:
                    yield None
                finally:
                    async with self._mount_lock:
                        self._lifespan_active = False
                        owners = list(self._mcp_owners.values())
                        self._managed_mounts.clear()
                        self._mcp_owners.clear()
                    owner_results = await asyncio.gather(
                        *(owner.close() for owner in owners), return_exceptions=True
                    )
                    async with self._mcp_lock:
                        self._mcp_sessions.clear()
                        self._mcp_pending.clear()
                    if close_agent_on_shutdown:
                        await self.agent.close()
                    for result in owner_results:
                        if isinstance(result, BaseException):
                            raise result

        self._app = Starlette(routes=routes, lifespan=lifespan)

    @staticmethod
    def _validate_principals(
        principals: Mapping[str, Principal], *, allow_empty: bool = False
    ) -> dict[str, Principal]:
        if not principals and not allow_empty:
            raise ValueError("At least one principal is required.")
        result: dict[str, Principal] = {}
        digests: set[str] = set()
        token_ids: set[str] = set()
        for key, principal in principals.items():
            if principal.workspace_mode != "operator":
                raise ValueError("Static principals must use operator mode.")
            if key != principal.user_id:
                raise ValueError("Principal mapping keys must equal user_id.")
            if principal.user_id in result:
                raise ValueError("Duplicate principal user_id.")
            if principal.token_digest.lower() in digests:
                raise ValueError("Principal token digests must be unique.")
            digests.add(principal.token_digest.lower())
            if principal.token_id is not None:
                if principal.token_id in token_ids:
                    raise ValueError("Principal token IDs must be unique.")
                token_ids.add(principal.token_id)
            result[principal.user_id] = Principal(
                user_id=principal.user_id,
                allowed_site_ids=set(principal.allowed_site_ids),
                token_digest=principal.token_digest.lower(),
                expires_at=principal.expires_at,
                revoked=principal.revoked,
                token_id=principal.token_id,
                workspace_id=principal.workspace_id,
                key_access=principal.key_access.model_copy(deep=True)
                if principal.key_access is not None
                else None,
            )
        return result

    @staticmethod
    def _principal_active(principal: Principal, *, now: datetime | None = None) -> bool:
        if principal.revoked:
            return False
        current = now or datetime.now(UTC)
        return principal.expires_at is None or current < principal.expires_at

    def _validate_mount_configuration(
        self, principals: Mapping[str, Principal]
    ) -> set[tuple[str, str | None]]:
        expected: set[tuple[str, str | None]] = set()
        for principal in principals.values():
            available = self._available_sites(principal)
            if principal.allowed_site_ids and len(available) != len(principal.allowed_site_ids):
                raise ValueError("Principal includes a site outside the agent's ownership map.")
            expected.update((principal.user_id, site_id) for site_id in available)
        return expected

    def rotate_principals(self, principals: Mapping[str, Principal]) -> None:
        """Atomically reload operator-managed principal metadata and digests.

        Rotation deliberately keeps the existing user/site mount topology.  A
        deployment can add or remove sites by restarting the host, while token
        replacement, expiry, revocation, and metadata changes take effect
        immediately without rebuilding live MCP managers.
        """

        candidate = self._validate_principals(principals, allow_empty=self._managed_workspaces)
        fixed_mounts = {
            key for key, mount in self._mounts.items() if mount.session.workspace_id is None
        }
        if self._validate_mount_configuration(candidate) != fixed_mounts:
            raise ValueError("Principal rotation cannot change user/site mounts while running.")
        self._principals = candidate
        now = datetime.now(UTC)
        active_users = {
            user_id
            for user_id, principal in candidate.items()
            if self._principal_active(principal, now=now)
        }
        self._sessions = {
            session_id: stored
            for session_id, stored in self._sessions.items()
            if stored.user_id in active_users or stored.session.workspace_id is not None
        }
        self._mcp_sessions = {
            session_id: admission
            for session_id, admission in self._mcp_sessions.items()
            if admission.user_id in active_users or admission.site_id in self._managed_sites
        }

    def _available_sites(self, principal: Principal) -> list[str | None]:
        sites = [
            site_id
            for site_id in principal.allowed_site_ids
            if site_id in self.agent.sites
            and self.agent.sites[site_id].user_id == principal.resource_user_id
        ]
        if not sites:
            owned = any(
                self.agent.sites[site_id].user_id == principal.resource_user_id
                for site_id in self._operator_site_ids
            )
            if not owned and not principal.allowed_site_ids and principal.key_access is None:
                return [None]
        return cast(list[str | None], sorted(sites))

    def _build_mcp_mounts(self) -> None:
        proxy = _NonClosingAgent(self.agent)
        for principal in self._principals.values():
            available = self._available_sites(principal)
            if principal.allowed_site_ids and len(available) != len(principal.allowed_site_ids):
                raise ValueError("Principal includes a site outside the agent's ownership map.")
            for site_id in available:
                session = self.agent.session(principal.user_id, site_id, access_mode="hosted")
                server = create_server(cast(EnergyAgent, proxy), session)
                # Bound the official MCP manager as well as the outer HTTP
                # application.  These settings are read when the app is built.
                server.settings.max_request_body_size = self.max_body_bytes
                server.settings.max_sessions = self.max_sessions_per_user
                server.settings.session_idle_timeout = self.session_idle_timeout
                app = server.streamable_http_app()
                manager = server.session_manager
                self._mounts[(principal.user_id, site_id)] = _MCPMount(app, manager, session)
                self._apps.append(server)

    async def _managed_mcp_mount(self, principal: Principal, site_id: str | None) -> _MCPMount:
        if site_id is None or site_id not in principal.allowed_site_ids:
            raise EnergyError("site_forbidden", "Select an owned site for MCP.")
        authorization = principal.workspace_scope
        if authorization is None:
            raise EnergyError("workspace_forbidden", "Workspace authorization is unavailable.")
        key = (
            principal.user_id,
            authorization.workspace_id,
            authorization.key_id,
            site_id,
            authorization.revision,
        )
        async with self._mount_lock:
            if not self._lifespan_active:
                raise EnergyError("mcp_unavailable", "MCP transport is unavailable.")
            await self._cleanup_managed_mounts()
            mount = self._managed_mounts.get(key)
            if mount is not None:
                owner = self._mcp_owners.get(key)
                if (
                    mount.session.workspace_id != principal.workspace_id
                    or owner is None
                    or owner.task.done()
                ):
                    raise EnergyError("mcp_unavailable", "MCP transport is unavailable.")
                return mount
            if len(self._mounts) + len(self._managed_mounts) >= self.max_sessions_global:
                raise EnergyError("mcp_mount_limit", "Maximum MCP sites reached.")
            session = self.agent.session(
                principal.user_id,
                site_id,
                access_mode="hosted",
                **self._runtime_authorization(principal),
            )
            server = create_server(cast(EnergyAgent, _NonClosingAgent(self.agent)), session)
            server.settings.max_request_body_size = self.max_body_bytes
            server.settings.max_sessions = self.max_sessions_per_user
            server.settings.session_idle_timeout = self.session_idle_timeout
            app = server.streamable_http_app()
            owner = _MCPOwner(server.session_manager)
            try:
                await asyncio.shield(owner.started)
            except asyncio.CancelledError:
                owner.stop.set()
                await asyncio.gather(owner.task, return_exceptions=True)
                raise
            except Exception:
                owner.stop.set()
                await asyncio.gather(owner.task, return_exceptions=True)
                raise EnergyError("mcp_unavailable", "MCP transport is unavailable.") from None
            mount = _MCPMount(app, server.session_manager, session)
            self._mcp_owners[key] = owner
            self._managed_mounts[key] = mount
            return mount

    def _scope_principal(self, scope: Scope) -> Principal | None:
        value = scope.get("state", {}).get("principal")
        return value if isinstance(value, Principal) else None

    @staticmethod
    def _header_values(scope: Scope, name: bytes) -> list[str]:
        return [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == name
        ]

    async def _admit_mcp(
        self,
        principal: Principal,
        site_id: str | None,
        request_session_id: str | None,
    ) -> tuple[_MCPAdmission | None, str | None]:
        admission = _MCPAdmission(
            principal.user_id,
            site_id,
            principal.workspace_id,
            principal.token_id,
            principal.workspace_scope.revision if principal.workspace_scope else None,
        )
        async with self._mcp_lock:
            if request_session_id is not None:
                current = self._mcp_sessions.get(request_session_id)
                if current is None:
                    # Let FastMCP produce its normal 404 for an unknown ID.
                    return admission, None
                if current != admission:
                    return None, "mcp_session_not_found"
                return current, None
            user_count = sum(
                1 for current in self._mcp_sessions.values() if current.user_id == principal.user_id
            )
            user_count += self._mcp_pending.get(principal.user_id, 0)
            if user_count >= self.max_sessions_per_user:
                return None, "mcp_session_limit"
            global_count = len(self._mcp_sessions) + sum(self._mcp_pending.values())
            if global_count >= self.max_sessions_global:
                return None, "mcp_global_session_limit"
            self._mcp_pending[principal.user_id] = self._mcp_pending.get(principal.user_id, 0) + 1
            return admission, None

    async def _register_mcp(self, admission: _MCPAdmission, session_id: str) -> bool:
        async with self._mcp_lock:
            current = self._mcp_sessions.get(session_id)
            if current is not None:
                return current == admission
            self._mcp_sessions[session_id] = admission
            return True

    async def _finish_mcp(
        self,
        admission: _MCPAdmission,
        request_session_id: str | None,
        response: _MCPResponse,
        method: str,
    ) -> None:
        async with self._mcp_lock:
            if request_session_id is None:
                pending = self._mcp_pending.get(admission.user_id, 0)
                if pending <= 1:
                    self._mcp_pending.pop(admission.user_id, None)
                else:
                    self._mcp_pending[admission.user_id] = pending - 1
                if response.registered and response.session_id and response.status_code is not None:
                    if response.status_code >= 400:
                        self._mcp_sessions.pop(response.session_id, None)
            elif method.upper() == "DELETE" and response.status_code is not None:
                if response.status_code < 400:
                    current = self._mcp_sessions.get(request_session_id)
                    if current == admission:
                        self._mcp_sessions.pop(request_session_id, None)

    def _authenticate(self, scope: Scope) -> Principal | None:
        headers = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == b"authorization"
        ]
        if len(headers) != 1:
            return None
        header = headers[0]
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        if not token or len(token.encode("utf-8", "ignore")) > _MAX_TOKEN_BYTES:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: Principal | None = None
        for principal in self._principals.values():
            if hmac.compare_digest(digest, principal.token_digest):
                matched = principal
        if matched is not None and not (
            self._control_store is not None and token.startswith("eat_")
        ):
            return matched if self._principal_active(matched) else None
        if self._control_store is None:
            return None
        identity = self._control_store.authenticate(token)
        if identity is None:
            return None
        if identity.workspace_mode == "managed":
            if not self._managed_workspaces or not isinstance(
                identity.access, (ManageKeyAccess, AgentKeyAccess)
            ):
                return None
            try:
                if identity.scope is None:
                    return None
                self._project_workspace(identity.scope.resource_owner_id, identity.workspace_id)
                sites = set(identity.scope.site_ids)
            except EnergyError:
                return None
            if isinstance(identity.access, AgentKeyAccess) and not sites:
                return None
            return Principal(
                identity.user_id,
                sites,
                digest,
                token_id=identity.key_id,
                workspace_id=identity.workspace_id,
                key_access=identity.access,
                workspace_mode="managed",
                workspace_scope=identity.scope,
            )
        policy = self._principals.get(identity.user_id)
        if policy is None or not self._principal_active(policy):
            return None
        sites = {
            site.id
            for site in self._control_store.sites(identity.user_id, identity.workspace_id)
            if (
                not isinstance(identity.access, AgentKeyAccess)
                or site.id in identity.access.site_ids
            )
            and site.id in policy.allowed_site_ids
            and site.id in self.agent.sites
            and self.agent.sites[site.id].user_id == identity.user_id
        }
        # Persisted keys cannot acquire a site-free mount shared by workspaces.
        # Provisioning and dynamic topology remain operator-controlled here.
        if not sites:
            return None
        return Principal(
            identity.user_id,
            sites,
            digest,
            token_id=identity.key_id,
            workspace_id=identity.workspace_id,
            key_access=identity.access,
        )

    def _oauth_configuration_digests(self) -> dict[str, str]:
        if self._managed_oauth is None:
            return {}
        return {item.id: item.fingerprint() for item in self._managed_oauth.configurations.values()}

    def _runtime_authorization(self, principal: Principal) -> Json:
        arguments: Json = {
            "workspace_id": self._runtime_workspace(principal),
            "managed_oauth_configurations": self._oauth_configuration_digests(),
        }
        if principal.workspace_scope is not None:
            scope = principal.workspace_scope
            arguments.update(
                resource_owner_id=scope.resource_owner_id,
                workspace_key_id=scope.key_id,
                workspace_policy_revision=scope.revision,
                connection_grants=set(scope.connection_ids)
                if scope.connection_ids is not None
                else None,
            )
        return arguments

    def _authorize_workspace_session(self, session: Session) -> None:
        scope = (
            self._control_store.key_scope(session.workspace_key_id)
            if self._control_store is not None and session.workspace_key_id is not None
            else None
        )
        if (
            scope is None
            or scope.actor_user_id != session.user_id
            or scope.resource_owner_id != session.resource_owner_id
            or scope.workspace_id != session.workspace_id
            or scope.revision != session.workspace_policy_revision
            or session.site_id not in scope.site_ids
            or session.connection_grants
            != (set(scope.connection_ids) if scope.connection_ids is not None else None)
        ):
            raise EnergyError("workspace_forbidden", "Workspace authorization changed.")

    async def _cleanup_managed_mounts(self) -> None:
        stale = []
        for key, mount in self._managed_mounts.items():
            try:
                self.agent._scope(mount.session)
            except EnergyError:
                stale.append(key)
        owners = []
        for key in stale:
            self._managed_mounts.pop(key, None)
            if owner := self._mcp_owners.pop(key, None):
                owners.append(owner)
        if stale:
            async with self._mcp_lock:
                for session_id, admission in list(self._mcp_sessions.items()):
                    if (
                        admission.user_id,
                        admission.workspace_id,
                        admission.key_id,
                        admission.site_id,
                        admission.policy_revision,
                    ) in stale:
                        self._mcp_sessions.pop(session_id, None)
        await asyncio.gather(*(owner.close() for owner in owners), return_exceptions=True)

    @staticmethod
    def _runtime_workspace(principal: Principal) -> str | None:
        return principal.workspace_id if principal.workspace_mode == "managed" else None

    def _project_workspace(self, user_id: str, workspace_id: str) -> None:
        assert self._control_store is not None
        workspace = self._control_store.workspace(user_id, workspace_id)
        if workspace.mode != "managed":
            raise EnergyError("workspace_forbidden", "Workspace is unavailable.")
        sites = self._control_store.sites(user_id, workspace_id)
        assets = self._control_store.assets(user_id, workspace_id)
        site_ids = {site.id for site in sites}
        for site in sites:
            if (
                site.user_id != user_id
                or site.id in self._operator_site_ids
                or self._managed_sites.get(site.id, workspace_id) != workspace_id
            ):
                raise EnergyError("workspace_forbidden", "Workspace topology is unavailable.")
        for asset in assets:
            if (
                asset.site_id not in site_ids
                or asset.id in self._operator_asset_ids
                or self._managed_assets.get(asset.id, workspace_id) != workspace_id
            ):
                raise EnergyError("workspace_forbidden", "Workspace topology is unavailable.")
        # Validate the whole projection before publishing any runtime records.
        for site in sites:
            self.agent.sites[site.id] = site
            self._managed_sites[site.id] = workspace_id
        for asset in assets:
            self.agent.assets[asset.id] = asset
            self._managed_assets[asset.id] = workspace_id

    def _allow_rate(self, principal: Principal) -> bool:
        now = time.monotonic()
        events = self._rate_events.setdefault(principal.user_id, deque())
        cutoff = now - 60.0
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.max_requests_per_minute:
            return False
        events.append(now)
        return True

    def cleanup_sessions(self, *, now: float | None = None) -> int:
        """Remove REST sessions idle longer than the configured TTL.

        Cleanup runs automatically before session creation and use.  Operators
        may also call this method from a lightweight maintenance task without
        exposing another network endpoint.
        """

        current = time.monotonic() if now is None else now
        stale = [
            session_id
            for session_id, stored in self._sessions.items()
            if current - stored.last_used_at >= self.session_idle_timeout
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)
        return len(stale)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _error("unsupported_transport", "Only HTTP transport is available.", 400)(
                scope, receive, send
            )
            return
        principal = self._authenticate(scope)
        if principal is None:
            await _error(
                "unauthorized",
                "Authentication is required.",
                401,
            )(scope, receive, send)
            return
        if not self._allow_rate(principal):
            await _error("rate_limited", "Request rate limit exceeded.", 429)(scope, receive, send)
            return
        content_lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        content_length: int | None = None
        if content_lengths:
            if len(content_lengths) != 1:
                await _error("invalid_content_length", "Request headers are invalid.", 400)(
                    scope, receive, send
                )
                return
            try:
                content_length = int(content_lengths[0])
            except (TypeError, ValueError, OverflowError):
                await _error("invalid_content_length", "Request headers are invalid.", 400)(
                    scope, receive, send
                )
                return
            if content_length < 0:
                await _error("invalid_content_length", "Request headers are invalid.", 400)(
                    scope, receive, send
                )
                return
        if content_length is not None and content_length > self.max_body_bytes:
            await _error("body_too_large", "Request body exceeds the configured limit.", 413)(
                scope, receive, send
            )
            return
        scoped = dict(scope)
        state = dict(scope.get("state", {}))
        state["principal"] = principal
        scoped["state"] = state
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scoped, _BoundedReceive(receive, self.max_body_bytes), tracked_send)
        except _BodyTooLarge:
            if not response_started:
                await _error("body_too_large", "Request body exceeds the configured limit.", 413)(
                    scope, receive, send
                )
        except Exception:
            # Never turn a provider or adapter exception into an HTTP body.  A
            # deployment may log the exception at its ASGI boundary, while
            # clients receive one stable, non-sensitive error shape.
            if not response_started:
                await _error("internal_error", "The request could not be completed.", 500)(
                    scope, receive, send
                )

    @staticmethod
    async def _parse_json(request: Request, model: type[BaseModel]) -> BaseModel | Response:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            return _error("invalid_content_type", "Use application/json for this request.", 415)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            return model.model_validate(payload)
        except (ValueError, TypeError, json.JSONDecodeError, ValidationError):
            return _error("invalid_request", "Request shape is invalid.", 400)

    def _principal_from_request(self, request: Request) -> Principal | Response:
        principal = self._scope_principal(request.scope)
        return principal or _error("unauthorized", "Authentication is required.", 401)

    def _session_for(self, request: Request) -> tuple[Principal, Session] | Response:
        self.cleanup_sessions()
        principal_value = self._principal_from_request(request)
        if isinstance(principal_value, Response):
            return principal_value
        session_id = request.path_params.get("session_id", "")
        stored = self._sessions.get(session_id)
        if stored is None or stored.user_id != principal_value.user_id:
            return _error("session_not_found", "Session is unavailable.", 404)
        if stored.session.workspace_id != self._runtime_workspace(principal_value):
            return _error("session_not_found", "Session is unavailable.", 404)
        if stored.site_id is None:
            if self._available_sites(principal_value) != [None]:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
        else:
            if stored.site_id not in principal_value.allowed_site_ids:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
            site = self.agent.sites.get(stored.site_id)
            if site is None or site.user_id != principal_value.resource_user_id:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
        if principal_value.workspace_scope is not None:
            if stored.session.workspace_key_id != principal_value.token_id:
                return _error("session_not_found", "Session is unavailable.", 404)
            try:
                self.agent._scope(stored.session)
            except EnergyError as exc:
                return self._energy_error(exc)
        stored.last_used_at = time.monotonic()
        return principal_value, stored.session

    @staticmethod
    def _energy_error(exc: EnergyError) -> Response:
        status = (
            403
            if exc.code.endswith("forbidden")
            else 503
            if exc.code == "mcp_unavailable"
            else 429
            if exc.code == "mcp_mount_limit"
            else 409
            if exc.code.startswith("ambiguous") or exc.code.endswith("conflict")
            else 400
        )
        return _error(exc.code, exc.message, status)

    def _workspace_details(self, principal: Principal) -> WorkspaceDetails | None:
        if principal.workspace_mode != "managed":
            return None
        assert self._control_store is not None and principal.workspace_id is not None
        record = self._control_store.workspace(principal.resource_user_id, principal.workspace_id)
        return WorkspaceDetails(**record.model_dump())

    def _workspace_manager(self, request: Request) -> Principal | Response:
        principal = self._principal_from_request(request)
        if isinstance(principal, Response):
            return principal
        if principal.workspace_mode != "managed" or not isinstance(
            principal.key_access, ManageKeyAccess
        ):
            return _error(
                "workspace_management_forbidden", "Use a managed workspace management key.", 403
            )
        return principal

    async def _workspace(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        details = self._workspace_details(principal)
        assert details is not None
        return _json_response(
            {"workspace": details.model_dump(mode="json")}, headers={"Cache-Control": "no-store"}
        )

    async def _workspace_sites(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        try:
            if request.method == "GET":
                sites = self._control_store.sites(principal.user_id, principal.workspace_id)
                return _json_response({"sites": [site.model_dump(mode="json") for site in sites]})
            parsed = await self._parse_json(request, WorkspaceSiteRequest)
            if isinstance(parsed, Response):
                return parsed
            data = cast(WorkspaceSiteRequest, parsed)
            site = self._control_store.create_site(
                principal.user_id, principal.workspace_id, **data.model_dump()
            )
            self._project_workspace(principal.user_id, principal.workspace_id)
            return _json_response({"site": site.model_dump(mode="json")}, status_code=201)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_toolkits(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        # Catalogue metadata has no execution session identifier or site-free mount.
        session = Session(
            user_id=principal.user_id,
            access_mode="hosted",
            workspace_id=principal.workspace_id,
        )
        return _json_response({"toolkits": self.agent.catalogue(session)})

    async def _workspace_assets(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        try:
            if request.method == "GET":
                return _json_response(
                    {
                        "assets": [
                            asset.model_dump(mode="json")
                            for asset in self._control_store.assets(
                                principal.user_id, principal.workspace_id
                            )
                        ]
                    }
                )
            parsed = await self._parse_json(request, WorkspaceAssetRequest)
            if isinstance(parsed, Response):
                return parsed
            data = cast(WorkspaceAssetRequest, parsed)
            store = self.agent.auth_store
            assert isinstance(store, AuthStore)
            for account_id in data.account_ids:
                account, _ = store.managed_snapshot(
                    principal.user_id, principal.workspace_id, account_id
                )
                if account.site_id != data.site_id or account.state != "active":
                    return _error(
                        "account_forbidden", "Select active connections at this site.", 403
                    )
            asset = self._control_store.create_asset(
                principal.user_id, principal.workspace_id, **data.model_dump()
            )
            self._project_workspace(principal.user_id, principal.workspace_id)
            return _json_response({"asset": asset.model_dump(mode="json")}, status_code=201)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_keys(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        try:
            if request.method == "GET":
                return _json_response(
                    {
                        "keys": [
                            key.model_dump(mode="json")
                            for key in self._control_store.keys(
                                principal.user_id, principal.workspace_id
                            )
                        ]
                    },
                    headers={"Cache-Control": "no-store"},
                )
            parsed = await self._parse_json(request, WorkspaceAgentKeyRequest)
            if isinstance(parsed, Response):
                return parsed
            data = cast(WorkspaceAgentKeyRequest, parsed)
            issued = self._control_store.create_key(
                principal.user_id,
                principal.workspace_id,
                data.name,
                access=AgentKeyAccess(site_ids=data.site_ids),
            )
            return _json_response(
                issued.model_dump(mode="json"),
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_members(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        try:
            if request.method == "GET":
                members = self._control_store.members(principal.user_id, principal.workspace_id)
                return _json_response(
                    {"members": [member.model_dump(mode="json") for member in members]},
                    headers={"Cache-Control": "no-store"},
                )
            parsed = await self._parse_json(request, WorkspaceMemberRequest)
            if isinstance(parsed, Response):
                return parsed
            member = self._control_store.add_member(
                principal.user_id,
                principal.workspace_id,
                cast(WorkspaceMemberRequest, parsed).user_id,
            )
            return _json_response(
                {"member": member.model_dump(mode="json")},
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_member(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        member_id = request.path_params["member_user_id"]
        try:
            if request.method == "DELETE":
                self._control_store.remove_member(
                    principal.user_id, principal.workspace_id, member_id
                )
                return _json_response({"removed": True}, headers={"Cache-Control": "no-store"})
            parsed = await self._parse_json(request, WorkspaceMemberGrants)
            if isinstance(parsed, Response):
                return parsed
            grants = cast(WorkspaceMemberGrants, parsed)
            assert isinstance(self.agent.auth_store, AuthStore)
            for connection_id in grants.connection_ids:
                account, _ = self.agent.auth_store.managed_snapshot(
                    principal.user_id, principal.workspace_id, connection_id
                )
                self._require_managed_oauth_approval(account)
                if (
                    account.state != "active"
                    or not account.enabled
                    or account.site_id not in grants.site_ids
                ):
                    raise EnergyError(
                        "account_forbidden", "Share an active connection in a granted site."
                    )
            member = self._control_store.set_member_grants(
                principal.user_id, principal.workspace_id, member_id, grants
            )
            return _json_response(
                {"member": member.model_dump(mode="json")},
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_member_key(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        parsed = await self._parse_json(request, WorkspaceAgentKeyRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(WorkspaceAgentKeyRequest, parsed)
        try:
            issued = self._control_store.create_member_key(
                principal.user_id,
                principal.workspace_id,
                request.path_params["member_user_id"],
                data.name,
                access=AgentKeyAccess(site_ids=data.site_ids),
            )
            return _json_response(
                issued.model_dump(mode="json"),
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_revoke_key(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        try:
            self._control_store.revoke_key(
                principal.user_id, principal.workspace_id, request.path_params["key_id"]
            )
            return _json_response({"revoked": True}, headers={"Cache-Control": "no-store"})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_setups(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        visible = {
            item["id"]
            for item in self.agent.catalogue(
                Session(
                    user_id=principal.user_id,
                    access_mode="hosted",
                    workspace_id=principal.workspace_id,
                )
            )
        }
        setups = [octopus_setup(enabled=True)] if "octopus-energy-account" in visible else []
        for setup in setups:
            setup.description = (
                "Verify your Octopus API key and meter, then map the connection to a site."
            )
        return _json_response(ConnectionSetupsResponse(setups=setups).model_dump(mode="json"))

    async def _workspace_auth_configurations(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._managed_oauth is not None and principal.workspace_id is not None
        store = self.agent.auth_store
        assert isinstance(store, AuthStore)
        return _json_response(
            {
                "configurations": [
                    {
                        **configuration.public(),
                        "pending_cleanup": store.pending_managed_oauth_cleanup(
                            principal.user_id, principal.workspace_id, configuration.id
                        ),
                    }
                    for configuration in self._managed_oauth.configurations.values()
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _workspace_authorize(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert principal.workspace_id is not None and self._managed_oauth is not None
        parsed = await self._parse_json(request, WorkspaceHomeAssistantAuthorizationRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(WorkspaceHomeAssistantAuthorizationRequest, parsed)
        try:
            await self._managed_oauth.cleanup(
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                configuration_id=data.configuration_id,
            )
            authorization = self._managed_oauth.begin(
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                configuration_id=data.configuration_id,
                entity_id=data.entity_id,
                telemetry=data.mapping.model_dump() if data.mapping else None,
            )
            response = WorkspaceAuthorizationResponse(
                authorization=WorkspaceAuthorization(
                    connection_id=authorization.connection_id,
                    authorization_url=authorization.authorization_url,
                    state=authorization.state,
                    expires_at=authorization.expires_at,
                )
            )
            return _json_response(
                response.model_dump(mode="json"),
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_authorization_cleanup(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert principal.workspace_id is not None and self._managed_oauth is not None
        parsed = await self._parse_json(request, WorkspaceOAuthCleanupRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(WorkspaceOAuthCleanupRequest, parsed)
        try:
            cleanup = await self._managed_oauth.cleanup(
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                configuration_id=data.configuration_id,
            )
            response = WorkspaceOAuthCleanupResponse.model_validate({"cleanup": cleanup})
            return _json_response(
                response.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_complete_authorization(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert principal.workspace_id is not None and self._managed_oauth is not None
        parsed = await self._parse_json(request, WorkspaceOAuthCompleteRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(WorkspaceOAuthCompleteRequest, parsed)
        try:
            account = await self._managed_oauth.complete(
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                configuration_id=data.configuration_id,
                state=data.state,
                code=data.code,
            )
            self.agent._sync_connections(principal.user_id, principal.workspace_id)
            from .connection_onboarding import _managed_outcome

            assert account.last_verified_at is not None
            return _json_response(
                _managed_outcome(account, account.last_verified_at),
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_connections(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert principal.workspace_id is not None
        store = self.agent.auth_store
        assert isinstance(store, AuthStore)
        try:
            if request.method == "GET":
                return _json_response(
                    {
                        "connections": [
                            account.public()
                            for account in store.workspace_accounts(
                                principal.user_id, principal.workspace_id
                            )
                        ]
                    },
                    headers={"Cache-Control": "no-store"},
                )
            visible = {
                item["id"]
                for item in self.agent.catalogue(
                    Session(
                        user_id=principal.user_id,
                        access_mode="hosted",
                        workspace_id=principal.workspace_id,
                    )
                )
            }
            if "octopus-energy-account" not in visible:
                return _error("toolkit_forbidden", "Toolkit is unavailable.", 403)
            parsed = await self._parse_json(request, OctopusConnectionRequest)
            if isinstance(parsed, Response):
                return parsed
            data = cast(OctopusConnectionRequest, parsed)
            from .connection_onboarding import OctopusConnectionService

            result = await OctopusConnectionService(store, self.agent.http).stage_managed(
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                credential=data.credential,
                mpan=data.mpan,
                serial_number=data.serial_number,
            )
            return _json_response(result, status_code=201, headers={"Cache-Control": "no-store"})
        except EnergyError as exc:
            return self._energy_error(exc)

    def _managed_oauth_approved(self, account: ConnectedAccount) -> bool:
        configuration_id = account.settings.get("managed_oauth_configuration_id")
        digest = account.settings.get("managed_oauth_configuration_digest")
        return (
            isinstance(configuration_id, str)
            and isinstance(digest, str)
            and self._oauth_configuration_digests().get(configuration_id) == digest
        )

    def _require_managed_oauth_approval(self, account: ConnectedAccount) -> None:
        if account.auth.scheme == "oauth" and not self._managed_oauth_approved(account):
            raise EnergyError(
                "oauth_configuration_unavailable", "Connection configuration is unavailable."
            )

    async def _workspace_map(self, request: Request) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert self._control_store is not None and principal.workspace_id is not None
        parsed = await self._parse_json(request, WorkspaceMapRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(WorkspaceMapRequest, parsed)
        site = next(
            (
                item
                for item in self._control_store.sites(principal.user_id, principal.workspace_id)
                if item.id == data.site_id
            ),
            None,
        )
        if site is None:
            return _error("site_forbidden", "Select a site in this workspace.", 403)
        store = self.agent.auth_store
        assert isinstance(store, AuthStore)
        from .connection_onboarding import map_managed_connection

        try:
            account, _ = store.managed_snapshot(
                principal.user_id, principal.workspace_id, request.path_params["connection_id"]
            )
            self._require_managed_oauth_approval(account)
            result = await map_managed_connection(
                store,
                self.agent.http,
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                connection_id=request.path_params["connection_id"],
                site=site,
            )
            self.agent._sync_connections(principal.user_id, principal.workspace_id)
            return _json_response(result, headers={"Cache-Control": "no-store"})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _workspace_verify(self, request: Request) -> Response:
        return await self._workspace_connection_action(request, verify=True)

    async def _workspace_disconnect(self, request: Request) -> Response:
        return await self._workspace_connection_action(request, verify=False)

    async def _workspace_connection_action(self, request: Request, *, verify: bool) -> Response:
        principal = self._workspace_manager(request)
        if isinstance(principal, Response):
            return principal
        assert principal.workspace_id is not None
        parsed = await self._parse_json(request, _ConnectionActionRequest)
        if isinstance(parsed, Response):
            return parsed
        store = self.agent.auth_store
        assert isinstance(store, AuthStore)
        from .connection_lifecycle import OctopusConnectionLifecycle

        try:
            account, _ = store.managed_snapshot(
                principal.user_id, principal.workspace_id, request.path_params["connection_id"]
            )
            if account.toolkit not in {"octopus-energy-account", "home-assistant"}:
                return _error(
                    "unsupported_provider", "This connection provider is unsupported.", 400
                )
            if verify:
                if account.site_id is None or account.site_id not in principal.allowed_site_ids:
                    return _error(
                        "connection_mapping_required",
                        "Map the connection before checking its health.",
                        409,
                    )
                self._require_managed_oauth_approval(account)
                if account.toolkit == "octopus-energy-account":
                    result = await OctopusConnectionLifecycle(store, self.agent.http).verify(
                        user_id=principal.user_id, site_id=account.site_id, connection_id=account.id
                    )
                else:
                    if account.auth.scheme == "oauth":
                        account = await store.refresh_managed(
                            principal.user_id, principal.workspace_id, account.id
                        )

                    async def home_probe(checked: ConnectedAccount, credential: str) -> bool:
                        self._require_managed_oauth_approval(checked)
                        return await probe_provider(self.agent.http, checked, credential)

                    status: Literal["healthy", "unhealthy"] = "healthy"
                    message = "Provider read succeeded."
                    try:
                        account = await store.verify_provider(
                            principal.user_id, account.id, home_probe, account.site_id
                        )
                    except EnergyError as exc:
                        if exc.code != "provider_verification_failed":
                            raise
                        account, _ = store.managed_snapshot(
                            principal.user_id, principal.workspace_id, account.id
                        )
                        status = "unhealthy"
                        message = "Provider verification failed."
                    health = ConnectionHealth(
                        connection_id=account.id,
                        provider="home_assistant",
                        status=status,
                        checked_at=datetime.now(UTC),
                        probe="provider-read",
                        message=message,
                    )
                    result = {"ok": True, "account": account.public(), "health": health.public()}
            else:
                upstream_revoked = None
                if account.auth.scheme == "oauth" and self._managed_oauth_approved(account):
                    assert self._managed_oauth is not None
                    configuration = self._managed_oauth.configurations[
                        account.settings["managed_oauth_configuration_id"]
                    ]
                    revoked, upstream_revoked = await store.revoke_managed(
                        principal.user_id,
                        principal.workspace_id,
                        account.id,
                        expected_provider=configuration.provider(),
                    )
                else:
                    revoked = store.revoke(principal.user_id, account.id, account.site_id)
                result = {
                    "ok": True,
                    "account": revoked.public(),
                    "upstream_revoked": upstream_revoked,
                }
            self.agent._sync_connections(principal.user_id, principal.workspace_id)
            return _json_response(result, headers={"Cache-Control": "no-store"})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _identity(self, request: Request) -> Response:
        principal = self._principal_from_request(request)
        if isinstance(principal, Response):
            return principal
        allowed = set(self._available_sites(principal))
        self.agent._sync_connections(principal.resource_user_id, self._runtime_workspace(principal))
        visible_assets = {
            asset.id: asset for asset in self.agent.assets.values() if asset.site_id in allowed
        }
        result = IdentityResponse(
            user_id=principal.user_id,
            can_manage_connections=self._can_manage_connections(principal),
            can_manage_workspace=principal.workspace_mode == "managed"
            and isinstance(principal.key_access, ManageKeyAccess),
            workspace=self._workspace_details(principal),
            sites=[
                self.agent.sites[site_id]
                for site_id in sorted(principal.allowed_site_ids)
                if site_id in allowed
            ],
            assets=[
                IdentityAsset(
                    id=asset.id,
                    site_id=asset.site_id,
                    name=asset.name,
                    kind=asset.kind,
                    parent_id=asset.parent_id if asset.parent_id in visible_assets else None,
                    account_ids=[
                        account_id
                        for account_id in asset.account_ids
                        if (account := self.agent.accounts.get(account_id)) is not None
                        and account.user_id == principal.resource_user_id
                        and (
                            principal.workspace_scope is None
                            or principal.workspace_scope.connection_ids is None
                            or account.id in principal.workspace_scope.connection_ids
                        )
                        and account.workspace_id == self._runtime_workspace(principal)
                        and account.site_id in {None, asset.site_id}
                    ],
                )
                for asset in sorted(visible_assets.values(), key=lambda asset: asset.id)
            ],
        )
        return _json_response(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    async def _create_session(self, request: Request) -> Response:
        self.cleanup_sessions()
        principal_value = self._principal_from_request(request)
        if isinstance(principal_value, Response):
            return principal_value
        parsed = await self._parse_json(request, _SessionCreate)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_SessionCreate, parsed)
        site_id = data.site_id
        if site_id is not None:
            if site_id not in principal_value.allowed_site_ids:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
            site = self.agent.sites.get(site_id)
            if site is None or site.user_id != principal_value.resource_user_id:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
        elif self._available_sites(principal_value) != [None]:
            return _error("site_required", "Select one allowed site.", 400)
        count = sum(
            1 for item in self._sessions.values() if item.user_id == principal_value.user_id
        )
        if count >= self.max_sessions_per_user:
            return _error("session_limit", "Maximum sessions for this user reached.", 429)
        if len(self._sessions) >= self.max_sessions_global:
            return _error("global_session_limit", "Maximum sessions reached.", 429)
        try:
            session = self.agent.session(
                principal_value.user_id,
                site_id,
                access_mode="hosted",
                **self._runtime_authorization(principal_value),
            )
            if data.resume_job_id:
                resumed = await self.agent.job(session, "resume", job_id=data.resume_job_id)
                if not resumed["ok"]:
                    return _json_response(resumed, 403)
                session.id = resumed["scope"]["session_id"]
        except EnergyError as exc:
            return self._energy_error(exc)
        self._sessions[session.id] = _StoredSession(
            session=session,
            user_id=principal_value.user_id,
            site_id=site_id,
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
        )
        return _json_response({"session_id": session.id, "site_id": site_id})

    async def _delete_session(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        session_id = request.path_params.get("session_id", "")
        for artifact in self.agent.workbench.list_artifacts(scope[1]):
            artifact_id = artifact.get("artifact_id")
            if isinstance(artifact_id, str):
                self.agent.workbench.delete(scope[1], artifact_id)
        self._sessions.pop(session_id, None)
        return _json_response({"deleted": True, "session_id": session_id})

    async def _search(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _SearchRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_SearchRequest, parsed)
        try:
            return _json_response({"tools": self.agent.search(scope[1], data.query, data.limit)})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _execute(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _ExecuteRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_ExecuteRequest, parsed)
        try:
            result = await self.agent.execute(
                scope[1],
                data.tool,
                data.arguments,
                data.account_id,
                data.persist,
                data.input_artifacts,
            )
            return _json_response(result)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _resolve(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, CapabilityRequest)
        if isinstance(parsed, Response):
            return parsed
        try:
            return _json_response(
                self.agent.resolver.resolve(scope[1], cast(CapabilityRequest, parsed))
            )
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _capability(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _CapabilityExecutionRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_CapabilityExecutionRequest, parsed)
        try:
            result = await self.agent.resolver.execute(scope[1], data, data.persist)
            return _json_response(result)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _skills(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        if request.method == "GET":
            return _json_response({"skills": SKILLS})
        parsed = await self._parse_json(request, _SearchRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_SearchRequest, parsed)
        return _json_response({"skills": search_skills(data.query)[: data.limit]})

    async def _run_skill(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _SkillExecutionRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_SkillExecutionRequest, parsed)
        return _json_response(await run_skill(self.agent, scope[1], data.skill_id, data.parameters))

    async def _jobs(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _JobRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_JobRequest, parsed)
        return _json_response(await self.agent.job(scope[1], **data.model_dump()))

    async def _toolkits(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        try:
            return _json_response({"toolkits": self.agent.catalogue(scope[1])})
        except EnergyError as exc:
            return self._energy_error(exc)

    @staticmethod
    def _can_manage_connections(principal: Principal) -> bool:
        return principal.key_access is None or isinstance(principal.key_access, ManageKeyAccess)

    async def _connection_setups(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        session = scope[1]
        visible = {toolkit["id"] for toolkit in self.agent.catalogue(session)}
        setups = []
        if "octopus-energy-account" in visible:
            can_manage = self._can_manage_connections(scope[0])
            has_storage = isinstance(self.agent.auth_store, AuthStore)
            setups.append(
                octopus_setup(
                    enabled=can_manage and has_storage,
                    unavailable_reason=None
                    if can_manage and has_storage
                    else "management_key_required"
                    if not can_manage
                    else "storage_unavailable",
                )
            )
        response = ConnectionSetupsResponse(setups=setups)
        return _json_response(response.model_dump(mode="json"))

    async def _connect_account(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        principal, session = scope
        if principal.workspace_mode == "managed":
            return _error(
                "connection_management_forbidden", "Use the workspace connection flow.", 403
            )
        if not self._can_manage_connections(principal):
            return _error(
                "connection_management_forbidden",
                "Use a workspace management key to connect accounts.",
                403,
            )
        visible = {toolkit["id"] for toolkit in self.agent.catalogue(session)}
        if "octopus-energy-account" not in visible:
            return _error("toolkit_forbidden", "Toolkit is outside this session scope.", 403)
        site = self.agent.sites.get(session.site_id or "")
        if site is None or site.user_id != session.user_id:
            return _error("site_forbidden", "Select an owned site before connecting.", 403)
        store = self.agent.auth_store
        if not isinstance(store, AuthStore):
            return _error(
                "connection_storage_unavailable",
                "Encrypted connection storage is unavailable.",
                503,
            )
        parsed = await self._parse_json(request, OctopusConnectionRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(OctopusConnectionRequest, parsed)
        from .connection_onboarding import OctopusConnectionService

        try:
            result = await OctopusConnectionService(store, self.agent.http).connect(
                user_id=session.user_id,
                site=site,
                credential=data.credential,
                mpan=data.mpan,
                serial_number=data.serial_number,
            )
            self.agent._sync_connections(session.user_id)
            return _json_response(result, status_code=201)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _verify_connection(self, request: Request) -> Response:
        return await self._connection_action(request, verify=True)

    async def _disconnect_connection(self, request: Request) -> Response:
        return await self._connection_action(request, verify=False)

    async def _connection_action(self, request: Request, *, verify: bool) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        principal, session = scope
        if not verify and not self._can_manage_connections(principal):
            return _error(
                "connection_management_forbidden",
                "Use a workspace management key to disconnect accounts.",
                403,
            )
        site = self.agent.sites.get(session.site_id or "")
        if site is None or site.user_id != session.user_id:
            return _error(
                "site_forbidden", "Select an owned site before managing a connection.", 403
            )
        if "octopus-energy-account" not in {item["id"] for item in self.agent.catalogue(session)}:
            return _error("toolkit_forbidden", "Toolkit is outside this session scope.", 403)
        store = self.agent.auth_store
        if not isinstance(store, AuthStore):
            return _error(
                "connection_storage_unavailable",
                "Encrypted connection storage is unavailable.",
                503,
            )
        parsed = await self._parse_json(request, _ConnectionActionRequest)
        if isinstance(parsed, Response):
            return parsed
        from .connection_lifecycle import OctopusConnectionLifecycle

        service = OctopusConnectionLifecycle(store, self.agent.http)
        args = {
            "user_id": session.user_id,
            "site_id": site.id,
            "connection_id": request.path_params["connection_id"],
        }
        try:
            connection_id = request.path_params["connection_id"]
            if session.workspace_id is not None:
                store.managed_snapshot(session.user_id, session.workspace_id, connection_id)
            elif (
                store.get_account(session.user_id, connection_id, site.id).workspace_id is not None
            ):
                return _error("account_forbidden", "Connection is outside this session scope.", 403)
            result = await service.verify(**args) if verify else service.disconnect(**args)
            self.agent._sync_connections(session.user_id, session.workspace_id)
            return _json_response(result)
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _connections(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        try:
            return _json_response({"connections": self.agent.connections(scope[1])})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _artifacts(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        try:
            return _json_response({"artifacts": self.agent.workbench.list_artifacts(scope[1])})
        except EnergyError as exc:
            return self._energy_error(exc)

    async def _delete_artifact(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        artifact_id = request.path_params.get("artifact_id", "")
        try:
            artifacts = self.agent.workbench.list_artifacts(scope[1])
            if not any(item.get("artifact_id") == artifact_id for item in artifacts):
                return _error("artifact_not_found", "Artifact does not exist in this session.", 404)
            self.agent.workbench.delete(scope[1], artifact_id)
            return _json_response({"deleted": True, "artifact_id": artifact_id})
        except EnergyError as exc:
            return self._energy_error(exc)


def create_host(
    agent: EnergyAgent,
    principals: dict[str, Principal],
    *,
    max_requests_per_minute: int = 60,
    max_body_bytes: int = 1_000_000,
    max_sessions_per_user: int = 10,
    session_idle_timeout: float = 1_800.0,
    max_sessions_global: int = 1_000,
    close_agent_on_shutdown: bool = False,
    control_store: ControlStore | None = None,
    managed_workspaces: bool = False,
    managed_oauth_configurations: tuple[HomeAssistantOAuthConfiguration, ...] = (),
) -> AuthenticatedHost:
    """Build an authenticated multi-user ASGI host.

    ``principals`` contains only SHA-256 token digests.  A caller obtains the
    raw token out of band and sends it as a bearer token at request time.  The
    returned host is safe to mount in an ASGI server and supports the REST
    session API plus streamable MCP at ``/mcp/{site_id}``.
    """

    return AuthenticatedHost(
        agent,
        principals,
        max_requests_per_minute=max_requests_per_minute,
        max_body_bytes=max_body_bytes,
        max_sessions_per_user=max_sessions_per_user,
        session_idle_timeout=session_idle_timeout,
        max_sessions_global=max_sessions_global,
        close_agent_on_shutdown=close_agent_on_shutdown,
        control_store=control_store,
        managed_workspaces=managed_workspaces,
        managed_oauth_configurations=managed_oauth_configurations,
    )


def token_digest(token: str) -> str:
    """Return the operator-side SHA-256 digest used by :class:`Principal`.

    This helper does not store the token.  Deployments should call it while
    provisioning a secret and persist only the returned digest.
    """

    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ValueError("Token must be a non-empty bounded string.")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["AuthenticatedHost", "Principal", "create_host", "token_digest"]
