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
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .capabilities import CapabilityRequest
from .models import EnergyError, Json, Session
from .runtime import EnergyAgent
from .server import create_server
from .skills import SKILLS, search_skills

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

    def __post_init__(self) -> None:
        if not self.user_id or len(self.user_id) > 256:
            raise ValueError("Principal user_id must be non-empty and bounded.")
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
        object.__setattr__(self, "allowed_site_ids", sites)


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _SessionCreate(_RequestModel):
    site_id: StrictStr | None = None
    resume_job_id: StrictStr | None = None


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


class _CapabilityExecutionRequest(CapabilityRequest):
    model_config = ConfigDict(extra="forbid", strict=True)
    persist: StrictBool = False


@dataclass
class _MCPMount:
    app: ASGIApp
    manager: Any
    session: Session


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
                or site.user_id != principal.user_id
            ):
                await _error("site_forbidden", "Site is outside this user's scope.", 403)(
                    scope, receive, send
                )
                return
        mount = self.host._mounts.get((principal.user_id, site_id))
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
        self._principals = self._validate_principals(principals)
        self._sessions: dict[str, _StoredSession] = {}
        self._rate_events: dict[str, deque[float]] = {}
        self._mounts: dict[tuple[str, str | None], _MCPMount] = {}
        self._apps: list[Any] = []
        self._mcp_sessions: dict[str, _MCPAdmission] = {}
        self._mcp_pending: dict[str, int] = {}
        self._mcp_lock = asyncio.Lock()
        self._build_mcp_mounts()
        routes = [
            Route("/sessions", self._create_session, methods=["POST"]),
            Route("/sessions/{session_id}", self._delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/search", self._search, methods=["POST"]),
            Route("/sessions/{session_id}/execute", self._execute, methods=["POST"]),
            Route("/sessions/{session_id}/resolve", self._resolve, methods=["POST"]),
            Route("/sessions/{session_id}/capability", self._capability, methods=["POST"]),
            Route("/sessions/{session_id}/skills", self._skills, methods=["GET", "POST"]),
            Route("/sessions/{session_id}/jobs", self._jobs, methods=["POST"]),
            Route("/sessions/{session_id}/connections", self._connections, methods=["GET"]),
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
                try:
                    yield None
                finally:
                    async with self._mcp_lock:
                        self._mcp_sessions.clear()
                        self._mcp_pending.clear()
                    if close_agent_on_shutdown:
                        await self.agent.close()

        self._app = Starlette(routes=routes, lifespan=lifespan)

    @staticmethod
    def _validate_principals(principals: Mapping[str, Principal]) -> dict[str, Principal]:
        if not principals:
            raise ValueError("At least one principal is required.")
        result: dict[str, Principal] = {}
        digests: set[str] = set()
        token_ids: set[str] = set()
        for key, principal in principals.items():
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

        candidate = self._validate_principals(principals)
        if self._validate_mount_configuration(candidate) != set(self._mounts):
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
            if stored.user_id in active_users
        }
        self._mcp_sessions = {
            session_id: admission
            for session_id, admission in self._mcp_sessions.items()
            if admission.user_id in active_users
        }

    def _available_sites(self, principal: Principal) -> list[str | None]:
        sites = [
            site_id
            for site_id in principal.allowed_site_ids
            if site_id in self.agent.sites
            and self.agent.sites[site_id].user_id == principal.user_id
        ]
        if not sites:
            owned = any(site.user_id == principal.user_id for site in self.agent.sites.values())
            if not owned and not principal.allowed_site_ids:
                return [None]
        return cast(list[str | None], sorted(sites))

    def _build_mcp_mounts(self) -> None:
        proxy = _NonClosingAgent(self.agent)
        for principal in self._principals.values():
            available = self._available_sites(principal)
            if principal.allowed_site_ids and len(available) != len(principal.allowed_site_ids):
                raise ValueError("Principal includes a site outside the agent's ownership map.")
            for site_id in available:
                session = self.agent.session(principal.user_id, site_id)
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
        async with self._mcp_lock:
            if request_session_id is not None:
                current = self._mcp_sessions.get(request_session_id)
                if current is None:
                    # Let FastMCP produce its normal 404 for an unknown ID.
                    return _MCPAdmission(principal.user_id, site_id), None
                if current.user_id != principal.user_id or current.site_id != site_id:
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
            return _MCPAdmission(principal.user_id, site_id), None

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
        return matched if matched is not None and self._principal_active(matched) else None

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
        if stored.site_id is None:
            if self._available_sites(principal_value) != [None]:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
        else:
            if stored.site_id not in principal_value.allowed_site_ids:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
            site = self.agent.sites.get(stored.site_id)
            if site is None or site.user_id != principal_value.user_id:
                return _error("site_forbidden", "Site is outside this user's scope.", 403)
        stored.last_used_at = time.monotonic()
        return principal_value, stored.session

    @staticmethod
    def _energy_error(exc: EnergyError) -> Response:
        status = (
            403
            if exc.code.endswith("forbidden")
            else 409
            if exc.code.startswith("ambiguous")
            else 400
        )
        return _error(exc.code, exc.message, status)

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
            if site is None or site.user_id != principal_value.user_id:
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
            session = self.agent.session(principal_value.user_id, site_id)
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
        return _json_response({"skills": search_skills(data.query)})

    async def _jobs(self, request: Request) -> Response:
        scope = self._session_for(request)
        if isinstance(scope, Response):
            return scope
        parsed = await self._parse_json(request, _JobRequest)
        if isinstance(parsed, Response):
            return parsed
        data = cast(_JobRequest, parsed)
        return _json_response(await self.agent.job(scope[1], **data.model_dump()))

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
