from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.responses import JSONResponse

from energy_agent_tools.connectors import mcp_bridge
from energy_agent_tools.connectors.mcp_bridge import MCPImportError, import_mcp, inspect_mcp
from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    EnergyError,
    ExecutionContext,
    Session,
    ToolAccountScope,
)
from energy_agent_tools.registry import Registry

_DISCOVERY_TOKEN = "private-discovery-token"


@asynccontextmanager
async def _serve(server: FastMCP, accepted: set[str]) -> AsyncIterator[tuple[str, list[str]]]:
    requests: list[str] = []
    app = server.streamable_http_app()

    class AuthMiddleware:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                authorization = headers.get(b"authorization", b"").decode()
                requests.append(authorization)
                if authorization not in accepted:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
            await self.wrapped(scope, receive, send)

    app.add_middleware(AuthMiddleware)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server_instance = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    task = asyncio.create_task(server_instance.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server_instance.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert server_instance.started
        yield f"http://127.0.0.1:{port}/mcp", requests
    finally:
        server_instance.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


def _account_context(
    *,
    credential: str = "runtime-secret-one",
    workspace_id: str = "workspace-1",
    account_id: str = "account-1",
    account_workspace_id: str = "workspace-1",
    grants: set[str] | None = None,
    session_site_id: str | None = "site-1",
    context_site_id: str | None = "site-1",
    account_site_id: str | None = "site-1",
    state: str = "active",
    enabled: bool = True,
) -> ExecutionContext:
    verified = datetime.now(UTC) if state == "active" else None
    account = ConnectedAccount(
        id=account_id,
        user_id="owner-1",
        toolkit="owned-mcp",
        workspace_id=account_workspace_id,
        site_id=account_site_id,
        auth=AuthConfig(scheme="bearer"),
        enabled=enabled,
        state=state,
        last_verified_at=verified,
    )
    session = Session(
        user_id="member-1",
        workspace_id=workspace_id,
        access_mode="hosted",
        site_id=session_site_id,
        allowed_actions={Action.READ},
        resource_owner_id="owner-1",
        workspace_key_id="key-1",
        workspace_policy_revision=3,
        connection_grants={"account-1"} if grants is None else grants,
    )
    return ExecutionContext(
        session=session,
        account=account,
        credential=credential,
        http=None,  # type: ignore[arg-type]
        workbench=None,
        site_id=context_site_id,
    )


def _reviewed_metadata(token: str) -> dict[str, dict[str, Any]]:
    return {
        "owned_read": {
            "reviewed": True,
            "action": "read-only",
            "kind": "metered",
            "unit": token,
            "source": token,
            "quality": token,
            "capabilities": [token],
            "assumptions": [token],
        },
        "control": {
            "reviewed": True,
            "action": "read-only",
            "kind": "metered",
            "unit": "kWh",
        },
    }


@pytest.mark.asyncio
async def test_account_scoped_import_redacts_discovery_credentials_and_checks_live_scope() -> None:
    discovery_token = _DISCOVERY_TOKEN
    auth_log: list[list[str]] = [[]]
    calls: list[str] = []
    control_calls: list[bool] = []
    server = FastMCP("scoped-mcp")

    def owned_read(
        value: Annotated[str, Field(description=f"schema uses {_DISCOVERY_TOKEN}")],
    ) -> dict[str, str]:
        calls.append(value)
        return {"value": value, "authorization": auth_log[0][-1]}

    def control() -> dict[str, bool]:
        control_calls.append(True)
        return {"called": True}

    server.add_tool(
        owned_read,
        name="owned_read",
        description=f"reviewed description {discovery_token}",
    )
    server.add_tool(control, name="control", description="unselected control tool")

    accepted_auth = {
        f"Bearer {discovery_token}",
        "Bearer runtime-secret-one",
        "Bearer runtime-secret-two",
    }
    async with _serve(server, accepted_auth) as (url, requests):
        target = await approve_mcp_target(url, allow_private=True)
        auth_log[0] = requests
        metadata = _reviewed_metadata(discovery_token)
        manifest = await inspect_mcp(
            "owned-mcp",
            remote_url=target.url,
            approved_target=target,
            discovery_auth=AuthConfig(scheme="bearer"),
            discovery_credential=discovery_token,
            tool_metadata=metadata,
        )
        assert discovery_token not in repr(manifest)
        assert f"Bearer {discovery_token}" in requests

        scope = ToolAccountScope(
            workspace_id="workspace-1", user_id="owner-1", account_id="account-1"
        )
        registry = Registry()

        failed_review_registry = Registry()
        with pytest.raises(MCPImportError) as review_error:
            await import_mcp(
                failed_review_registry,
                "owned-mcp",
                remote_url=target.url,
                approved_target=target,
                discovery_auth=AuthConfig(scheme="bearer"),
                discovery_credential=discovery_token,
                account_scope=scope,
                selected_tools=frozenset({"owned_read", "control"}),
                tool_metadata={"owned_read": metadata["owned_read"]},
                expected_schema_digest=str(manifest["schema_digest"]),
            )
        assert review_error.value.code == "mcp_review_required"
        assert failed_review_registry.toolkits == {}
        assert failed_review_registry.tools == {}
        assert failed_review_registry.handlers == {}

        for invalid_unit in (None, "", " ", 3):
            with pytest.raises(MCPImportError) as unit_error:
                await import_mcp(
                    failed_review_registry,
                    "owned-mcp",
                    remote_url=target.url,
                    approved_target=target,
                    discovery_auth=AuthConfig(scheme="bearer"),
                    discovery_credential=discovery_token,
                    account_scope=scope,
                    selected_tools=frozenset({"owned_read"}),
                    tool_metadata={"owned_read": {**metadata["owned_read"], "unit": invalid_unit}},
                    expected_schema_digest=str(manifest["schema_digest"]),
                )
            assert unit_error.value.code == "mcp_review_required"
            assert failed_review_registry.tools == {} and failed_review_registry.toolkits == {}

        with pytest.raises(MCPImportError) as selection_error:
            await import_mcp(
                failed_review_registry,
                "owned-mcp",
                remote_url=target.url,
                approved_target=target,
                discovery_auth=AuthConfig(scheme="bearer"),
                discovery_credential=discovery_token,
                account_scope=scope,
                selected_tools=frozenset({"missing"}),
                tool_metadata=metadata,
                expected_schema_digest=str(manifest["schema_digest"]),
            )
        assert selection_error.value.code == "mcp_selection_invalid"
        assert failed_review_registry.toolkits == {}
        assert failed_review_registry.tools == {}
        assert failed_review_registry.handlers == {}

        for invalid_options in (
            {"auth_required": False},
            {"headers": {"Authorization": "static-secret"}},
            {"credential_env": "REMOTE_MCP_TOKEN"},
        ):
            with pytest.raises(MCPImportError) as profile_error:
                await import_mcp(
                    failed_review_registry,
                    "owned-mcp",
                    remote_url=target.url,
                    approved_target=target,
                    discovery_auth=AuthConfig(scheme="bearer"),
                    discovery_credential=discovery_token,
                    account_scope=scope,
                    selected_tools=frozenset({"owned_read"}),
                    tool_metadata=metadata,
                    expected_schema_digest=str(manifest["schema_digest"]),
                    **invalid_options,
                )
            assert profile_error.value.code == "mcp_account_profile_invalid"
        assert failed_review_registry.toolkits == {}

        for invalid_target, endpoint, expected_code in (
            (None, target.url, "mcp_account_profile_invalid"),
            (target, target.url + "/elsewhere", "mcp_target_invalid"),
        ):
            request_count = len(requests)
            with pytest.raises(MCPImportError) as target_error:
                await import_mcp(
                    failed_review_registry,
                    "owned-mcp",
                    remote_url=endpoint,
                    approved_target=invalid_target,
                    account_scope=scope,
                    selected_tools=frozenset({"owned_read"}),
                    tool_metadata=metadata,
                    expected_schema_digest=str(manifest["schema_digest"]),
                )
            assert target_error.value.code == expected_code
            assert len(requests) == request_count and failed_review_registry.tools == {}

        for unsafe_header in ("Host", "Connection", "Transfer-Encoding", "Bad\nHeader"):
            request_count = len(requests)
            with pytest.raises(MCPImportError) as auth_error:
                await import_mcp(
                    failed_review_registry,
                    "owned-mcp",
                    remote_url=target.url,
                    approved_target=target,
                    discovery_auth=AuthConfig(scheme="api-key", header=unsafe_header),
                    discovery_credential=discovery_token,
                    account_scope=scope,
                    selected_tools=frozenset({"owned_read"}),
                    tool_metadata=metadata,
                    expected_schema_digest=str(manifest["schema_digest"]),
                )
            assert auth_error.value.code == "mcp_account_profile_invalid"
            assert len(requests) == request_count and failed_review_registry.tools == {}

        toolkit = await import_mcp(
            registry,
            "owned-mcp",
            remote_url=target.url,
            approved_target=target,
            discovery_auth=AuthConfig(scheme="bearer"),
            discovery_credential=discovery_token,
            account_scope=scope,
            selected_tools=frozenset({"owned_read"}),
            tool_metadata=metadata,
            expected_schema_digest=str(manifest["schema_digest"]),
        )
        name = "owned-mcp.owned_read"
        assert toolkit.auth_required is True
        assert set(registry.tools) == {name}
        assert registry.tools[name].resource_scope == "account"
        assert registry.tools[name].account_scope == scope
        assert discovery_token not in repr(registry.tools[name].public())
        assert discovery_token not in registry.tools[name].description
        assert discovery_token not in repr(registry.tools[name].input_schema)

        handler = registry.handlers[name]
        captured = [cell.cell_contents for cell in handler.__closure__ or ()]
        transport = next(value for value in captured if isinstance(value, mcp_bridge._Transport))
        assert transport.headers is None
        assert discovery_token not in repr(captured)

        first_result = await handler({"value": "first"}, _account_context())
        assert first_result.data["value"] == "first"
        assert first_result.data["authorization"] == "Bearer [REDACTED]"
        assert "runtime-secret-one" not in repr(first_result.model_dump())
        assert requests[-1] == "Bearer runtime-secret-one"

        second_result = await handler(
            {"value": "second"}, _account_context(credential="runtime-secret-two")
        )
        assert second_result.data["authorization"] == "Bearer [REDACTED]"
        assert "runtime-secret-two" not in repr(second_result.model_dump())
        assert requests[-1] == "Bearer runtime-secret-two"
        assert calls == ["first", "second"]
        assert control_calls == []

        unsafe_context = _account_context()
        unsafe_context.account.auth.header = "Host"
        request_count = len(requests)
        with pytest.raises(MCPImportError) as unsafe_execution:
            await handler({"value": "unsafe-auth"}, unsafe_context)
        assert unsafe_execution.value.code == "mcp_account_profile_invalid"
        assert len(requests) == request_count

        for denied_context in (
            _account_context(workspace_id="workspace-elsewhere"),
            _account_context(account_id="account-elsewhere"),
            _account_context(grants=set()),
            _account_context(session_site_id="site-elsewhere"),
            _account_context(context_site_id="site-elsewhere"),
            _account_context(state="disabled", enabled=False),
        ):
            request_count = len(requests)
            with pytest.raises(EnergyError) as denied:
                await handler({"value": "denied"}, denied_context)
            assert denied.value.code == "account_forbidden"
            assert len(requests) == request_count

        def changed_read(value: str, extra: str = "") -> dict[str, str]:
            calls.append(value)
            return {"value": value, "authorization": auth_log[0][-1]}

        server.remove_tool("owned_read")
        server.add_tool(changed_read, name="owned_read", description="changed schema")
        changed_manifest = await inspect_mcp(
            "owned-mcp",
            remote_url=target.url,
            approved_target=target,
            discovery_auth=AuthConfig(scheme="bearer"),
            discovery_credential=discovery_token,
        )
        assert changed_manifest["schema_digest"] != manifest["schema_digest"]
        request_count = len(requests)
        with pytest.raises(EnergyError) as drift:
            await handler({"value": "schema-changed"}, _account_context())
        assert drift.value.code == "mcp_schema_drift", drift.value.message
        assert len(requests) > request_count
        assert calls == ["first", "second"]
