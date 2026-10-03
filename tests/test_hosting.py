from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import DataKind, EnergyResult, Site, Tool, Toolkit, schema
from energy_agent_tools.registry import Registry
from energy_agent_tools.runtime import EnergyAgent


def _agent(tmp_path: Path) -> EnergyAgent:
    registry = Registry()
    registry.add_toolkit(
        Toolkit(id="fixture", name="Fixture", description="test", runtime="native", status="stable")
    )

    async def energy(arguments: dict[str, Any], _context: Any) -> EnergyResult:
        return EnergyResult(
            data={"value": arguments["value"]},
            kind=DataKind.METERED,
            unit="kWh",
            source="fixture",
        )

    registry.add(
        Tool(
            name="FIXTURE_ENERGY",
            toolkit="fixture",
            description="Read fixture energy",
            input_schema=schema({"value": {"type": "number"}}, ["value"]),
            capabilities=["get_energy_consumption"],
        ),
        energy,
    )
    sites = [
        Site(id="one-a", user_id="one", name="One A", timezone="UTC"),
        Site(id="one-b", user_id="one", name="One B", timezone="UTC"),
        Site(id="two-a", user_id="two", name="Two A", timezone="UTC"),
    ]
    return EnergyAgent(registry, tmp_path / "state", sites=sites)


def _principals() -> dict[str, Principal]:
    return {
        "one": Principal("one", {"one-a", "one-b"}, token_digest("one-token")),
        "two": Principal("two", {"two-a"}, token_digest("two-token")),
    }


async def _client(host: Any, **kwargs: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=host)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000", **kwargs)


class _Lifespan:
    """Minimal ASGI lifespan driver for httpx's ASGI transport."""

    def __init__(self, app: Any):
        self.app = app
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Lifespan:
        async def receive() -> dict[str, Any]:
            return await self.events.get()

        async def send(message: dict[str, Any]) -> None:
            await self.messages.put(message)

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "scope": {}}
        self.task = asyncio.create_task(self.app(scope, receive, send))
        await self.events.put({"type": "lifespan.startup"})
        event = await self.messages.get()
        assert event["type"] == "lifespan.startup.complete", event
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.events.put({"type": "lifespan.shutdown"})
        event = await self.messages.get()
        assert event["type"] == "lifespan.shutdown.complete", event
        assert self.task is not None
        await self.task


@pytest.mark.asyncio
async def test_rest_authentication_and_scope_isolation(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals())
    try:
        async with await _client(host) as client:
            assert (await client.post("/sessions", json={"site_id": "one-a"})).status_code == 401

            first = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer one-token"},
                json={"site_id": "one-a"},
            )
            assert first.status_code == 200
            session_id = first.json()["session_id"]

            foreign_site = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer one-token"},
                json={"site_id": "two-a"},
            )
            assert foreign_site.status_code == 403

            cross_user = await client.post(
                f"/sessions/{session_id}/search",
                headers={"Authorization": "Bearer two-token"},
                json={"query": "energy"},
            )
            assert cross_user.status_code == 404

            cross_mcp = await client.post(
                "/mcp/two-a",
                headers={"Authorization": "Bearer one-token"},
                json={},
            )
            assert cross_mcp.status_code == 403
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_rest_session_execute_resolve_and_limits(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(
        agent,
        _principals(),
        max_requests_per_minute=20,
        max_body_bytes=128,
        max_sessions_per_user=1,
    )
    try:
        async with await _client(host) as client:
            headers = {"Authorization": "Bearer one-token"}
            created = await client.post("/sessions", headers=headers, json={"site_id": "one-a"})
            assert created.status_code == 200
            session_id = created.json()["session_id"]

            executed = await client.post(
                f"/sessions/{session_id}/execute",
                headers=headers,
                json={"tool": "FIXTURE_ENERGY", "arguments": {"value": 3}},
            )
            assert executed.status_code == 200
            assert executed.json()["result"]["kind"] == "metered"

            malformed = await client.post(
                f"/sessions/{session_id}/search",
                headers=headers,
                json={"query": "energy", "unexpected": "secret"},
            )
            assert malformed.status_code == 400
            assert "secret" not in malformed.text

            oversized = await client.post(
                f"/sessions/{session_id}/search",
                headers=headers,
                content=json.dumps({"query": "x" * 1000}),
            )
            assert oversized.status_code == 413

            second = await client.post("/sessions", headers=headers, json={"site_id": "one-b"})
            assert second.status_code == 429
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_delete_is_scoped_and_idle_sessions_are_reclaimed(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(
        agent,
        _principals(),
        session_idle_timeout=1,
        max_sessions_global=1,
    )
    try:
        async with await _client(host) as client:
            one_headers = {"Authorization": "Bearer one-token"}
            two_headers = {"Authorization": "Bearer two-token"}
            created = await client.post("/sessions", headers=one_headers, json={"site_id": "one-a"})
            assert created.status_code == 200
            session_id = created.json()["session_id"]

            cross_user_delete = await client.delete(f"/sessions/{session_id}", headers=two_headers)
            assert cross_user_delete.status_code == 404

            blocked = await client.post("/sessions", headers=two_headers, json={"site_id": "two-a"})
            assert blocked.status_code == 429

            stored = host._sessions[session_id]
            stored.last_used_at = time.monotonic() - 2
            assert host.cleanup_sessions() == 1

            replacement = await client.post(
                "/sessions", headers=two_headers, json={"site_id": "two-a"}
            )
            assert replacement.status_code == 200
            replacement_id = replacement.json()["session_id"]
            deleted = await client.delete(f"/sessions/{replacement_id}", headers=two_headers)
            assert deleted.status_code == 200
            missing = await client.post(
                f"/sessions/{replacement_id}/search",
                headers=two_headers,
                json={"query": "energy"},
            )
            assert missing.status_code == 404
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_artifact_listing_and_deletion_stay_session_scoped(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals())
    try:
        async with await _client(host) as client:
            one_headers = {"Authorization": "Bearer one-token"}
            two_headers = {"Authorization": "Bearer two-token"}
            one = await client.post("/sessions", headers=one_headers, json={"site_id": "one-a"})
            two = await client.post("/sessions", headers=two_headers, json={"site_id": "two-a"})
            one_session = one.json()["session_id"]
            two_session = two.json()["session_id"]
            output = await client.post(
                f"/sessions/{one_session}/execute",
                headers=one_headers,
                json={
                    "tool": "FIXTURE_ENERGY",
                    "arguments": {"value": 3},
                    "persist": True,
                },
            )
            assert output.status_code == 200
            artifact_id = output.json()["result"]["data"]["artifact_id"]

            listed = await client.get(f"/sessions/{one_session}/artifacts", headers=one_headers)
            assert listed.status_code == 200
            assert listed.json()["artifacts"][0]["artifact_id"] == artifact_id

            foreign_delete = await client.delete(
                f"/sessions/{two_session}/artifacts/{artifact_id}", headers=two_headers
            )
            assert foreign_delete.status_code == 404

            deleted = await client.delete(
                f"/sessions/{one_session}/artifacts/{artifact_id}", headers=one_headers
            )
            assert deleted.status_code == 200
            assert (
                await client.get(f"/sessions/{one_session}/artifacts", headers=one_headers)
            ).json()["artifacts"] == []
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_principal_expiry_revocation_and_rotation(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    now = datetime.now(UTC)
    host = create_host(
        agent,
        {
            "one": Principal(
                "one",
                {"one-a", "one-b"},
                token_digest("old-token"),
                expires_at=now + timedelta(minutes=5),
                token_id="token-1",
            ),
            "two": Principal(
                "two",
                {"two-a"},
                token_digest("expired-token"),
                expires_at=now - timedelta(seconds=1),
                token_id="token-2",
            ),
        },
    )
    try:
        async with await _client(host) as client:
            expired = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer expired-token"},
                json={"site_id": "two-a"},
            )
            assert expired.status_code == 401

            old = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer old-token"},
                json={"site_id": "one-a"},
            )
            assert old.status_code == 200

            host.rotate_principals(
                {
                    "one": Principal(
                        "one",
                        {"one-a", "one-b"},
                        token_digest("new-token"),
                        token_id="token-3",
                    ),
                    "two": Principal(
                        "two",
                        {"two-a"},
                        token_digest("expired-token"),
                        revoked=True,
                        token_id="token-2",
                    ),
                }
            )
            rotated_old = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer old-token"},
                json={"site_id": "one-a"},
            )
            assert rotated_old.status_code == 401
            rotated_new = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer new-token"},
                json={"site_id": "one-a"},
            )
            assert rotated_new.status_code == 200
            revoked = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer expired-token"},
                json={"site_id": "two-a"},
            )
            assert revoked.status_code == 401
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_rate_limit_and_digest_rejection_do_not_echo_credentials(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals(), max_requests_per_minute=1)
    try:
        async with await _client(host) as client:
            bad = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer wrong-token"},
                json={"site_id": "one-a"},
            )
            assert bad.status_code == 401
            assert "wrong-token" not in bad.text

            good = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer one-token"},
                json={"site_id": "one-a"},
            )
            assert good.status_code == 200
            limited = await client.post(
                "/sessions",
                headers={"Authorization": "Bearer one-token"},
                json={"site_id": "one-a"},
            )
            assert limited.status_code == 429
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_streamable_mcp_is_authenticated_and_fixed_to_site(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals())
    try:
        async with _Lifespan(host):
            async with await _client(host) as client:
                headers = {
                    "Authorization": "Bearer two-token",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                initialize = await client.post(
                    "/mcp/two-a",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "host-test", "version": "1"},
                        },
                    },
                )
                assert initialize.status_code == 200
                assert "Energy Agent Tools" in initialize.text
                mcp_session_id = initialize.headers["mcp-session-id"]

                listed = await client.post(
                    "/mcp/two-a",
                    headers={**headers, "Mcp-Session-Id": mcp_session_id},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                assert listed.status_code == 200
                assert "ENERGY_SEARCH_TOOLS" in listed.text

                wrong_token = await client.post(
                    "/mcp/two-a",
                    headers={**headers, "Authorization": "Bearer invalid-token"},
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                )
                assert wrong_token.status_code == 401
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_mcp_session_limit_is_shared_across_site_mounts(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals(), max_sessions_per_user=1)
    try:
        async with _Lifespan(host):
            async with await _client(host) as client:
                headers = {
                    "Authorization": "Bearer one-token",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "host-test", "version": "1"},
                    },
                }
                first = await client.post("/mcp/one-a", headers=headers, json=body)
                assert first.status_code == 200
                second = await client.post("/mcp/one-b", headers=headers, json=body)
                assert second.status_code == 429
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_started_response_is_not_replaced_by_internal_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals())

    async def sends_then_fails(scope: Any, _receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        raise RuntimeError("private-provider-detail")

    host._app = sends_then_fails
    try:
        async with await _client(host) as client:
            response = await client.get("/anything", headers={"Authorization": "Bearer one-token"})
            assert response.status_code == 200
            assert response.text == "ok"
            assert "private-provider-detail" not in response.text
    finally:
        await agent.close()


def test_principal_requires_digest_and_token_digest_is_not_reversible() -> None:
    digest = token_digest("operator-token")
    assert len(digest) == 64
    assert "operator-token" not in digest
    with pytest.raises(ValueError):
        Principal("u", set(), "raw-token")


@pytest.mark.asyncio
async def test_owned_host_closes_agent_inside_its_lifespan(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    host = create_host(agent, _principals(), close_agent_on_shutdown=True)
    async with _Lifespan(host):
        assert not agent._closed
    assert agent._closed
    assert agent.http.is_closed
    await agent.close()


@pytest.mark.asyncio
async def test_identity_lists_only_token_scoped_sites_and_assets(tmp_path: Path) -> None:
    from energy_agent_tools.models import Asset

    agent = _agent(tmp_path)
    agent.assets["one-meter"] = Asset(
        id="one-meter",
        site_id="one-a",
        name="Meter",
        kind="meter",
        metadata={"api_key": "must-not-reach-identity"},
        parent_id="two-meter",
        account_ids=["foreign-account-id"],
    )
    agent.assets["two-meter"] = Asset(
        id="two-meter", site_id="two-a", name="Other meter", kind="meter"
    )
    principals = _principals()
    principals["one"] = Principal("one", {"one-a"}, token_digest("one-token"))
    host = create_host(agent, principals)
    try:
        async with await _client(host) as client:
            assert (await client.get("/me")).status_code == 401
            response = await client.get("/me", headers={"Authorization": "Bearer one-token"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["user_id"] == "one"
            assert [s["id"] for s in response.json()["sites"]] == ["one-a"]
            assert [a["id"] for a in response.json()["assets"]] == ["one-meter"]
            assert "must-not-reach-identity" not in response.text
            assert "metadata" not in response.json()["assets"][0]
            assert response.json()["assets"][0]["parent_id"] is None
            assert response.json()["assets"][0]["account_ids"] == []
            foreign = await client.get("/me", headers={"Authorization": "Bearer two-token"})
            assert [s["id"] for s in foreign.json()["sites"]] == ["two-a"]
            assert [a["id"] for a in foreign.json()["assets"]] == ["two-meter"]
    finally:
        await agent.close()
