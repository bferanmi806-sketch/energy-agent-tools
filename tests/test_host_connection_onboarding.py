from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from test_hosting import _Lifespan

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import Site

PAYLOAD = {
    "provider": "octopus",
    "credential": "fictional-octopus-key",
    "mpan": "1234567890123",
    "serial_number": "TEST123",
}


def _rpc(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    return json.loads(
        next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
    )


@pytest.mark.asyncio
async def test_connect_updates_the_same_rest_and_hosted_mcp_sessions(tmp_path: Path):
    probes = []

    def provider(request: httpx.Request):
        probes.append(request)
        assert request.url.host == "api.octopus.energy"
        assert (
            base64.b64decode(request.headers["Authorization"][6:]).decode()
            == PAYLOAD["credential"] + ":"
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "consumption": 1.25,
                        "interval_start": "2026-09-30T00:00:00Z",
                        "interval_end": "2026-09-30T00:30:00Z",
                    }
                ]
            },
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    agent.http = upstream
    agent._owns_http = False
    agent.auth_store = store
    agent.sites = {
        site.id: site
        for site in [
            Site(id="home", user_id="one", name="Home", timezone="UTC"),
            Site(id="foreign", user_id="two", name="Other", timezone="UTC"),
        ]
    }
    host = create_host(
        agent,
        {
            "one": Principal("one", {"home"}, token_digest("fixture-one")),
            "two": Principal("two", {"foreign"}, token_digest("fixture-two")),
        },
    )
    auth = {"Authorization": "Bearer fixture-one"}
    mcp_headers = {**auth, "Accept": "application/json, text/event-stream"}
    try:
        async with (
            _Lifespan(host),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            session = (
                await client.post("/sessions", headers=auth, json={"site_id": "home"})
            ).json()["session_id"]
            initialized = await client.post(
                "/mcp/home",
                headers=mcp_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "onboarding-fixture", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200
            mcp_headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            await client.post(
                "/mcp/home",
                headers=mcp_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            setup = (
                await client.get(f"/sessions/{session}/connection-setup", headers=auth)
            ).json()["setups"][0]
            assert setup["enabled"] is True
            assert {field["name"] for field in setup["fields"]} == {
                "credential",
                "mpan",
                "serial_number",
            }
            created = await client.post(
                f"/sessions/{session}/connections", headers=auth, json=PAYLOAD
            )
            assert created.status_code == 201, created.text
            account = created.json()["account"]
            assert account["verified"] is True and account["site_id"] == "home"
            assert PAYLOAD["credential"] not in created.text
            assert PAYLOAD["credential"].encode() not in store.path.read_bytes()
            resolved = (
                await client.post(
                    f"/sessions/{session}/resolve",
                    headers=auth,
                    json={"capability": "get_energy_consumption"},
                )
            ).json()
            assert resolved["selected"]["account_id"] == account["id"]
            executed = (
                await client.post(
                    f"/sessions/{session}/capability",
                    headers=auth,
                    json={"capability": "get_energy_consumption"},
                )
            ).json()
            assert executed["ok"] is True and executed["result"]["data"][0]["value"] == 1.25
            mcp = _rpc(
                await client.post(
                    "/mcp/home",
                    headers=mcp_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "ENERGY_EXECUTE_CAPABILITY",
                            "arguments": {"capability": "get_energy_consumption"},
                        },
                    },
                )
            )
            result = json.loads(mcp["result"]["content"][0]["text"])
            assert result["ok"] is True and result["result"]["kind"] == "metered"
            assert result["result"]["unit"] == "kWh"
            assert PAYLOAD["credential"] not in json.dumps(agent.events)
            denied = await client.post(
                f"/sessions/{session}/connections",
                headers={"Authorization": "Bearer fixture-two"},
                json=PAYLOAD,
            )
            assert denied.status_code == 404
            assert len(probes) == 3  # Verification, REST execution and the existing MCP session.
            actions = f"/sessions/{session}/connections/{account['id']}"
            checked = await client.post(actions + "/verify", headers=auth, json={})
            assert checked.status_code == 200 and checked.json()["health"]["status"] == "healthy"
            disconnected = await client.post(actions + "/disconnect", headers=auth, json={})
            assert (
                disconnected.status_code == 200
                and disconnected.json()["account"]["state"] == "revoked"
            )
            denied_rest = (
                await client.post(
                    f"/sessions/{session}/capability",
                    headers=auth,
                    json={"capability": "get_energy_consumption"},
                )
            ).json()
            assert denied_rest["ok"] is False
            denied_mcp = _rpc(
                await client.post(
                    "/mcp/home",
                    headers=mcp_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "ENERGY_EXECUTE_CAPABILITY",
                            "arguments": {"capability": "get_energy_consumption"},
                        },
                    },
                )
            )
            assert json.loads(denied_mcp["result"]["content"][0]["text"])["ok"] is False
            assert len(probes) == 4

    finally:
        await agent.close()
        await upstream.aclose()


@pytest.mark.asyncio
async def test_rejected_onboarding_does_not_publish_an_account(tmp_path: Path):
    probes = []

    def provider(request: httpx.Request):
        probes.append(request)
        return httpx.Response(401, text=PAYLOAD["credential"])

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    agent = build_agent(tmp_path / "agent")
    await agent.http.aclose()
    agent.http, agent.auth_store, agent._owns_http = upstream, store, False
    agent.sites = {"home": Site(id="home", user_id="one", name="Home", timezone="UTC")}
    host = create_host(agent, {"one": Principal("one", {"home"}, token_digest("fixture-one"))})
    auth = {"Authorization": "Bearer fixture-one"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
        ) as client:
            session = (
                await client.post("/sessions", headers=auth, json={"site_id": "home"})
            ).json()["session_id"]
            path = f"/sessions/{session}/connections"
            invalid = await client.post(
                path, headers=auth, json={**PAYLOAD, "base_url": "http://127.0.0.1/"}
            )
            assert invalid.status_code == 400 and not probes
            failed = await client.post(path, headers=auth, json=PAYLOAD)
            assert failed.status_code == 400 and PAYLOAD["credential"] not in failed.text
            assert store.accounts("one") == [] and agent.accounts == {}
            assert len(probes) == 1
    finally:
        await agent.close()
        await upstream.aclose()


@pytest.mark.asyncio
async def test_onboarding_refuses_missing_encryption_and_unauthenticated_requests(tmp_path: Path):
    agent = build_agent(tmp_path / "agent")
    agent.sites = {"home": Site(id="home", user_id="one", name="Home", timezone="UTC")}
    host = create_host(agent, {"one": Principal("one", {"home"}, token_digest("fixture-one"))})
    auth = {"Authorization": "Bearer fixture-one"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
        ) as client:
            session = (
                await client.post("/sessions", headers=auth, json={"site_id": "home"})
            ).json()["session_id"]
            setup = (await client.get(f"/sessions/{session}/connection-setup", headers=auth)).json()
            assert setup["setups"][0]["enabled"] is False
            refused = await client.post(
                f"/sessions/{session}/connections", headers=auth, json=PAYLOAD
            )
            assert refused.status_code == 503
            assert agent.accounts == {}
            assert (
                await client.post(f"/sessions/{session}/connections", json=PAYLOAD)
            ).status_code == 401
    finally:
        await agent.close()
