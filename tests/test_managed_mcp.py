from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography.fernet import Fernet
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.managed_mcp import ManagedMCPService
from energy_agent_tools.models import (
    Action,
    AuthConfig,
    ConnectedAccount,
    EnergyError,
    ExecutionContext,
    Session,
    Site,
)
from energy_agent_tools.registry import Registry

USER_ID = "managed-owner"
WORKSPACE_ID = "managed-workspace"
SITE = Site(id="managed-site", user_id=USER_ID, name="Test site", timezone="UTC")
DISCOVERY_SECRET = "discovery-secret-value"
VAULT_SECRET = "stored-provider-secret"
RUNTIME_SECRET = "fresh-runtime-secret"


class _FixtureServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Concurrent fixture servers share one process; none owns its OS signals.
        yield


@asynccontextmanager
async def _serve(
    server: FastMCP, accepted_auth: set[str]
) -> AsyncIterator[tuple[str, list[str], Any]]:
    requests: list[str] = []
    app = server.streamable_http_app()

    class AuthMiddleware:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                authorization = next(
                    (
                        headers[name].decode()
                        for name in (b"authorization", b"x-api-key")
                        if headers.get(name)
                    ),
                    "",
                )
                requests.append(authorization)
                if authorization not in accepted_auth:
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
    instance = _FixtureServer(uvicorn.Config(app, log_level="critical"))
    task = asyncio.create_task(instance.serve(sockets=[listener]))
    stopped = False

    async def stop() -> None:
        nonlocal stopped
        if not task.done():
            instance.should_exit = True
            await asyncio.wait_for(task, timeout=5)
        if not stopped:
            listener.close()
            stopped = True

    try:
        for _ in range(200):
            if instance.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert instance.started
        yield f"http://127.0.0.1:{port}/mcp", requests, stop
    finally:
        await stop()


def _server(auth_headers: list[str]) -> FastMCP:
    server = FastMCP("managed-mcp-fixture")

    def read_energy(value: str) -> dict[str, str]:
        return {"value": value, "authorization": auth_headers[-1] if auth_headers else ""}

    def unselected_control() -> dict[str, str]:
        return {"state": "changed"}

    server.add_tool(read_energy, name="read_energy", description="Read an energy value")
    server.add_tool(unselected_control, name="unselected_control")
    return server


def _metadata() -> dict[str, dict[str, Any]]:
    return {
        "read_energy": {
            "reviewed": True,
            "actions": ["read-only"],
            "kind": "metered",
            "unit": "kWh",
        }
    }


def _context(account: ConnectedAccount, credential: str | None) -> ExecutionContext:
    return ExecutionContext(
        session=Session(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            site_id=SITE.id,
            allowed_actions={Action.READ},
        ),
        account=account,
        credential=credential,
        http=None,  # type: ignore[arg-type]
        workbench=None,
        site_id=SITE.id,
    )


async def _inspect(
    service: ManagedMCPService, url: str, auth: AuthConfig, credential: str | None
) -> str:
    manifest = await service.inspect(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        url=url,
        auth=auth,
        credential=credential,
    )
    assert credential is None or credential not in repr(manifest)
    return str(manifest["schema_digest"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth", "credential", "discovery", "runtime", "discovery_header", "runtime_header"),
    [
        (
            AuthConfig(scheme="bearer"),
            VAULT_SECRET,
            DISCOVERY_SECRET,
            RUNTIME_SECRET,
            f"Bearer {DISCOVERY_SECRET}",
            f"Bearer {RUNTIME_SECRET}",
        ),
        (AuthConfig(scheme="none"), None, None, None, "", ""),
        (
            AuthConfig(scheme="basic"),
            "dmF1bHQ6cGFzcw==",
            "ZGlzY292ZXJ5OnBhc3M=",
            "cnVudGltZTpwYXNz",
            "Basic ZGlzY292ZXJ5OnBhc3M=",
            "Basic cnVudGltZTpwYXNz",
        ),
        (
            AuthConfig(scheme="api-key", header="X-API-Key"),
            "vault-api-key",
            "discovery-api-key",
            "runtime-api-key",
            "discovery-api-key",
            "runtime-api-key",
        ),
    ],
)
async def test_managed_connection_stages_maps_recovers_and_revokes_safely(
    tmp_path: Path,
    auth: AuthConfig,
    credential: str | None,
    discovery: str | None,
    runtime: str | None,
    discovery_header: str,
    runtime_header: str,
) -> None:
    auth_headers = [""]
    server = _server(auth_headers)
    accepted = {""}
    if auth.scheme != "none":
        accepted.update(
            {
                discovery_header,
                f"Basic {credential}"
                if auth.scheme == "basic"
                else (credential if auth.scheme == "api-key" else f"Bearer {credential}"),
                runtime_header,
            }
        )

    async with _serve(server, accepted) as (url, requests, _stop):

        def approve(workspace_id: str, requested_url: str):
            assert workspace_id == WORKSPACE_ID
            return approve_mcp_target(requested_url, allow_private=True)

        key = Fernet.generate_key()
        vault_root = tmp_path / "vault"
        store = AuthStore(vault_root, key)
        registry = Registry()
        service = ManagedMCPService(store, registry, approve_target=approve)
        digest = await _inspect(service, url, auth, discovery)
        assert store.workspace_accounts(USER_ID, WORKSPACE_ID) == []
        assert registry.toolkits == {} and registry.tools == {}

        staged = await service.stage(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            url=url,
            display_name="Reviewed energy connection",
            auth=auth,
            credential=credential,
            expected_schema_digest=digest,
            selected_tools=frozenset({"read_energy"}),
            tool_metadata=_metadata(),
        )
        assert staged["account"]["state"] == "pending_mapping"
        assert staged["selected_tool_count"] == 1
        assert registry.toolkits == {} and registry.tools == {}
        connection_id = str(staged["account"]["id"])
        account, revision = store.managed_snapshot(USER_ID, WORKSPACE_ID, connection_id)
        assert (
            account.auth.secret_id is None
            if auth.scheme == "none"
            else account.auth.secret_id == connection_id
        )
        assert revision == 1

        request_count = len(requests)
        with pytest.raises(EnergyError) as foreign:
            await service.map(
                user_id="other-user",
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=SITE,
            )
        assert foreign.value.code == "account_forbidden"
        assert len(requests) == request_count

        mapped = await service.map(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            connection_id=connection_id,
            site=SITE,
        )
        assert mapped["account"]["state"] == "active"
        assert set(registry.tools) == {f"{connection_id}.read_energy"}
        assert f"{connection_id}.unselected_control" not in registry.tools
        assert DISCOVERY_SECRET not in repr(registry.tools)
        assert VAULT_SECRET not in repr(registry.tools)

        active, active_revision = store.managed_snapshot(USER_ID, WORKSPACE_ID, connection_id)
        handler = registry.handlers[f"{connection_id}.read_energy"]
        result = await handler({"value": "current"}, _context(active, runtime))
        assert result.data["value"] == "current"
        assert runtime is None or runtime not in repr(result.model_dump(mode="json"))

        # Retrying the same mapping is idempotent. A different site fails before discovery.
        retry = await service.map(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            connection_id=connection_id,
            site=SITE,
        )
        assert retry["account"]["state"] == "active"
        request_count = len(requests)
        with pytest.raises(EnergyError) as wrong_site:
            await service.map(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=Site(id="other-site", user_id=USER_ID, name="Other", timezone="UTC"),
            )
        assert wrong_site.value.code == "connection_conflict"
        assert len(requests) == request_count

        store.close()
        store = AuthStore(vault_root, key)
        recovered_registry = Registry()
        recovered = ManagedMCPService(store, recovered_registry, approve_target=approve)
        status = await recovered.recover(user_id=USER_ID, workspace_id=WORKSPACE_ID)
        assert status["connections"] == [{"connection_id": connection_id, "status": "ready"}]
        recovered_account, recovered_revision = store.managed_snapshot(
            USER_ID, WORKSPACE_ID, connection_id
        )
        assert recovered_revision == active_revision
        restored = recovered_registry.handlers[f"{connection_id}.read_energy"]
        restored_result = await restored(
            {"value": "after-restart"}, _context(recovered_account, runtime)
        )
        assert restored_result.data["value"] == "after-restart"
        assert DISCOVERY_SECRET not in repr(recovered_registry.tools)
        assert VAULT_SECRET not in repr(recovered_registry.tools)
        assert runtime is None or runtime not in repr(restored_result.model_dump(mode="json"))

        # Revocation removes only this account namespace and recovery cannot republish it.
        store.revoke(USER_ID, connection_id, SITE.id)
        revoked = await recovered.recover(user_id=USER_ID, workspace_id=WORKSPACE_ID)
        assert revoked["connections"] == [
            {
                "connection_id": connection_id,
                "status": "skipped",
                "error_code": "connection_inactive",
            }
        ]
        assert not recovered_registry.tools
        store.close()

        if auth.scheme != "none":
            assert discovery_header in requests
            assert runtime_header in requests
            assert (
                f"Basic {credential}"
                if auth.scheme == "basic"
                else credential
                if auth.scheme == "api-key"
                else f"Bearer {credential}"
            ) in requests
        else:
            assert set(requests) == {""}


@pytest.mark.asyncio
async def test_management_authorization_is_rechecked_before_persistence_and_publication(
    tmp_path: Path,
) -> None:
    auth_headers: list[str] = []
    server = _server(auth_headers)
    accepted = {f"Bearer {DISCOVERY_SECRET}", f"Bearer {VAULT_SECRET}"}
    checks = 0
    deny_on: int | None = None

    def authorize_write() -> None:
        nonlocal checks
        checks += 1
        if checks == deny_on:
            raise EnergyError(
                "workspace_key_revoked", "Management authorization is no longer valid."
            )

    async with _serve(server, accepted) as (url, requests, _stop):

        async def approve(workspace_id: str, requested_url: str):
            assert workspace_id == WORKSPACE_ID
            return await approve_mcp_target(requested_url, allow_private=True)

        store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        registry = Registry()
        service = ManagedMCPService(
            store, registry, approve_target=approve, authorize_write=authorize_write
        )
        auth = AuthConfig(scheme="bearer")
        digest = await _inspect(service, url, auth, DISCOVERY_SECRET)

        # A stale key is rejected before any provider network request.
        checks = 0
        deny_on = 1
        before = len(requests)
        with pytest.raises(EnergyError) as early_denial:
            await service.stage(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                url=url,
                display_name="Reviewed provider",
                auth=auth,
                credential=VAULT_SECRET,
                expected_schema_digest=digest,
                selected_tools=frozenset({"read_energy"}),
                tool_metadata=_metadata(),
            )
        assert early_denial.value.code == "workspace_key_revoked"
        assert len(requests) == before

        # Authorization is checked again after discovery, before storing the secret.
        checks = 0
        deny_on = 2
        with pytest.raises(EnergyError) as stage_denial:
            await service.stage(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                url=url,
                display_name="Reviewed provider",
                auth=auth,
                credential=VAULT_SECRET,
                expected_schema_digest=digest,
                selected_tools=frozenset({"read_energy"}),
                tool_metadata=_metadata(),
            )
        assert stage_denial.value.code == "workspace_key_revoked"
        assert not store.workspace_accounts(USER_ID, WORKSPACE_ID)

        checks = 0
        deny_on = None
        staged = await service.stage(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            url=url,
            display_name="Reviewed provider",
            auth=auth,
            credential=VAULT_SECRET,
            expected_schema_digest=digest,
            selected_tools=frozenset({"read_energy"}),
            tool_metadata=_metadata(),
        )
        connection_id = str(staged["account"]["id"])
        assert checks == 2

        # A key revoked during mapping discovery cannot activate or publish the connection.
        checks = 0
        deny_on = 2
        with pytest.raises(EnergyError) as map_denial:
            await service.map(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=SITE,
            )
        assert map_denial.value.code == "workspace_key_revoked"
        pending, _ = store.managed_snapshot(USER_ID, WORKSPACE_ID, connection_id)
        assert pending.state == "pending_mapping" and not pending.enabled
        assert registry.tools == {}

        checks = 0
        deny_on = None
        await service.map(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            connection_id=connection_id,
            site=SITE,
        )
        assert checks == 3
        handler = registry.handlers[f"{connection_id}.read_energy"]
        closed_values = [cell.cell_contents for cell in (handler.__closure__ or ())]
        assert all(value is not service and value is not authorize_write for value in closed_values)
        store.close()


@pytest.mark.asyncio
async def test_failed_active_mapping_retry_keeps_the_published_namespace(tmp_path: Path) -> None:
    server = _server([])
    accepted = {f"Bearer {DISCOVERY_SECRET}", f"Bearer {VAULT_SECRET}"}

    async with _serve(server, accepted) as (url, _requests, stop):

        async def approve(workspace_id: str, requested_url: str):
            assert workspace_id == WORKSPACE_ID
            return await approve_mcp_target(requested_url, allow_private=True)

        store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        registry = Registry()
        service = ManagedMCPService(store, registry, approve_target=approve)
        auth = AuthConfig(scheme="bearer")
        digest = await _inspect(service, url, auth, DISCOVERY_SECRET)
        staged = await service.stage(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            url=url,
            display_name="Reviewed provider",
            auth=auth,
            credential=VAULT_SECRET,
            expected_schema_digest=digest,
            selected_tools=frozenset({"read_energy"}),
            tool_metadata=_metadata(),
        )
        connection_id = str(staged["account"]["id"])
        await service.map(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            connection_id=connection_id,
            site=SITE,
        )
        tool_name = f"{connection_id}.read_energy"
        published_handler = registry.handlers[tool_name]
        await stop()

        with pytest.raises(EnergyError):
            await service.map(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=SITE,
            )
        assert tool_name in registry.tools
        assert registry.handlers[tool_name] is published_handler
        store.close()


@pytest.mark.asyncio
async def test_managed_definition_rejects_secret_names_and_drift_keeps_pending(
    tmp_path: Path,
) -> None:
    auth_headers = [""]
    server = _server(auth_headers)
    accepted = {f"Bearer {DISCOVERY_SECRET}", f"Bearer {VAULT_SECRET}"}
    async with _serve(server, accepted) as (url, requests, _stop):

        async def approve(workspace_id: str, requested_url: str):
            assert workspace_id == WORKSPACE_ID
            return await approve_mcp_target(requested_url, allow_private=True)

        store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        registry = Registry()
        service = ManagedMCPService(store, registry, approve_target=approve)
        auth = AuthConfig(scheme="bearer")
        digest = await _inspect(service, url, auth, DISCOVERY_SECRET)

        before = len(requests)
        with pytest.raises(EnergyError) as secret_name:
            await service.stage(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                url=url,
                display_name="Reviewed provider",
                auth=auth,
                credential=VAULT_SECRET,
                expected_schema_digest=digest,
                selected_tools=frozenset({f"{VAULT_SECRET}.read_energy"}),
                tool_metadata=_metadata(),
            )
        assert secret_name.value.code == "mcp_review_invalid"
        assert len(requests) == before
        assert not store.workspace_accounts(USER_ID, WORKSPACE_ID)

        staged = await service.stage(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            url=url,
            display_name="Reviewed provider",
            auth=auth,
            credential=VAULT_SECRET,
            expected_schema_digest=digest,
            selected_tools=frozenset({"read_energy"}),
            tool_metadata=_metadata(),
        )
        connection_id = str(staged["account"]["id"])
        before_wrong_scope = len(requests)
        with pytest.raises(EnergyError):
            await service.map(
                user_id=USER_ID,
                workspace_id="another-workspace",
                connection_id=connection_id,
                site=SITE,
            )
        assert len(requests) == before_wrong_scope

        def changed_energy(value: str, quality: str = "changed") -> dict[str, str]:
            return {"value": value, "quality": quality}

        server.remove_tool("read_energy")
        server.add_tool(changed_energy, name="read_energy", description="Changed schema")
        with pytest.raises(EnergyError) as drift:
            await service.map(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                connection_id=connection_id,
                site=SITE,
            )
        assert drift.value.code == "mcp_schema_drift"
        pending, _ = store.managed_snapshot(USER_ID, WORKSPACE_ID, connection_id)
        assert pending.state == "pending_mapping" and not pending.enabled
        assert registry.toolkits == {} and registry.tools == {}
        assert VAULT_SECRET not in str(pending.settings)
        store.close()


@pytest.mark.asyncio
async def test_recovery_isolates_offline_connections_and_removes_deleted_namespace(
    tmp_path: Path,
) -> None:
    auth_log_a: list[str] = []
    auth_log_b: list[str] = []
    server_a = _server(auth_log_a)
    server_b = _server(auth_log_b)
    accepted = {f"Bearer {VAULT_SECRET}"}
    store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    registry = Registry()

    async def approve(workspace_id: str, url: str):
        assert workspace_id == WORKSPACE_ID
        return await approve_mcp_target(url, allow_private=True)

    service = ManagedMCPService(store, registry, approve_target=approve)
    async with _serve(server_a, accepted) as (url_a, _requests_a, stop_a):
        async with _serve(server_b, accepted) as (url_b, _requests_b, _stop_b):
            for url in (url_a, url_b):
                digest = await _inspect(service, url, AuthConfig(scheme="bearer"), VAULT_SECRET)
                staged = await service.stage(
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    url=url,
                    display_name="Provider connection",
                    auth=AuthConfig(scheme="bearer"),
                    credential=VAULT_SECRET,
                    expected_schema_digest=digest,
                    selected_tools=frozenset({"read_energy"}),
                    tool_metadata=_metadata(),
                )
                await service.map(
                    user_id=USER_ID,
                    workspace_id=WORKSPACE_ID,
                    connection_id=str(staged["account"]["id"]),
                    site=SITE,
                )

            accounts = store.workspace_accounts(USER_ID, WORKSPACE_ID)
            account_a = next(
                account for account in accounts if account.settings["managed_mcp"]["url"] == url_a
            )
            account_b = next(
                account for account in accounts if account.settings["managed_mcp"]["url"] == url_b
            )
            assert f"{account_a.id}.read_energy" in registry.tools
            assert f"{account_b.id}.read_energy" in registry.tools

            await stop_a()
            recovery = await service.recover(user_id=USER_ID, workspace_id=WORKSPACE_ID)
            by_id = {entry["connection_id"]: entry for entry in recovery["connections"]}
            assert by_id[account_a.id]["status"] == "unavailable"
            assert by_id[account_a.id]["error_code"] in {
                "mcp_discovery_failed",
                "mcp_timeout",
            }
            assert by_id[account_b.id] == {"connection_id": account_b.id, "status": "ready"}
            assert f"{account_a.id}.read_energy" not in registry.tools
            assert f"{account_b.id}.read_energy" in registry.tools

            store._db.execute("DELETE FROM accounts WHERE id = ?", (account_b.id,))
            store._db.commit()
            await service.recover(user_id=USER_ID, workspace_id=WORKSPACE_ID)
            assert f"{account_b.id}.read_energy" not in registry.tools
    store.close()


@pytest.mark.parametrize("operation", ["inspect", "stage"])
async def test_managed_discovery_has_an_overall_deadline(tmp_path, monkeypatch, operation):
    import energy_agent_tools.managed_mcp as lifecycle

    stalled = False
    entered = asyncio.Event()

    class SlowDiscovery(FastMCP):
        async def list_tools(self):
            if stalled:
                entered.set()
                await asyncio.sleep(60)
            return await super().list_tools()

    server = SlowDiscovery("deadline-fixture")

    @server.tool()
    def read_energy(value: str) -> dict[str, str]:
        return {"value": value}

    async with _serve(server, {""}) as (url, requests, _):

        async def approve(workspace_id, requested_url):
            assert workspace_id == WORKSPACE_ID
            assert requested_url == url
            return await approve_mcp_target(url, allow_private=True)

        store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        registry = Registry()
        service = ManagedMCPService(store, registry, approve_target=approve)
        auth = AuthConfig(scheme="none")
        digest = await _inspect(service, url, auth, None)
        stalled = True
        monkeypatch.setattr(lifecycle, "_DISCOVERY_TIMEOUT_SECONDS", 0.25)
        try:
            with pytest.raises(EnergyError) as failure:
                async with asyncio.timeout(2):
                    if operation == "inspect":
                        await service.inspect(
                            user_id=USER_ID,
                            workspace_id=WORKSPACE_ID,
                            url=url,
                            auth=auth,
                            credential=None,
                        )
                    else:
                        await service.stage(
                            user_id=USER_ID,
                            workspace_id=WORKSPACE_ID,
                            url=url,
                            display_name="Slow fixture",
                            auth=auth,
                            credential=None,
                            expected_schema_digest=digest,
                            selected_tools=frozenset({"read_energy"}),
                            tool_metadata=_metadata(),
                        )
            assert entered.is_set()
            assert failure.value.code == "mcp_timeout"
            assert registry.tools == {}
            assert store.workspace_accounts(USER_ID, WORKSPACE_ID) == []
            assert requests
        finally:
            store.close()
