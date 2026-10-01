import httpx
import pytest
from cryptography.fernet import Fernet

from benchmarks.fixture import FIXTURE_SITE, FIXTURE_USER, write_fixture
from benchmarks.server import build_fixture_agent
from energy_agent_tools.auth import AuthStore, OAuthProvider
from energy_agent_tools.models import AuthConfig, ConnectedAccount
from energy_agent_tools.oauth_callback import callback_app
from energy_agent_tools.sdk import EnergyAgentTools


async def test_sdk_initializes_mcp_before_reviewed_binding_validation(tmp_path):
    import sys
    from pathlib import Path

    config = {
        "mcp_servers": [
            {
                "toolkit_id": "fixture",
                "command": sys.executable,
                "args": [str(Path(__file__).parent / "fixtures" / "mcp_server.py")],
                "tool_metadata": {
                    "energy_sum": {
                        "reviewed": True,
                        "action": "calculation",
                        "kind": "calculated",
                        "unit": "kWh",
                    }
                },
            }
        ],
        "bindings": [
            {
                "capability": "calculate_energy",
                "tool": "fixture.energy_sum",
                "reviewed": True,
                "kind": "calculated",
                "unit": "kWh",
            }
        ],
    }
    energy = EnergyAgentTools(tmp_path, config)
    with pytest.raises(RuntimeError, match="Initialize"):
        energy.session("u")
    async with energy:
        result = await energy.session("u").capability("calculate_energy", {"left": 2, "right": 3})
        assert result["ok"], result
        assert result["result"]["data"]["sum"] == 5


async def test_bound_sdk_and_executable_capability_workflows(tmp_path):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    async with EnergyAgentTools(tmp_path, agent=agent) as energy:
        session = energy.session(FIXTURE_USER, FIXTURE_SITE)
        for provider in ("openai", "openai-responses", "anthropic"):
            assert len(await session.tools(provider)) == 11
        result = await session.capability("get_energy_consumption", persist=True)
        artifact = result["result"]["data"]["artifact_id"]
        assert result["ok"] and result["result"]["kind"] == "metered"
        summary = await session.skill(
            "yesterday-consumption", {"artifacts": {"get_energy_consumption": artifact}}
        )
        assert summary["ok"]
        assert summary["evidence"][-1]["analysis"]["result"]["data"]["sum"] == 17
        cost = await session.skill("electricity-cost")
        assert cost["ok"]
        rows = cost["evidence"][-1]["analysis"]["result"]["data"]
        assert sum(row["cost"] for row in rows) == pytest.approx(3.6925)
        for skill in (
            "building-spike",
            "solar-consumption",
            "grid-conditions",
            "building-comparison",
            "energy-baseline",
        ):
            output = await session.skill(skill)
            assert output["ok"], output
        denied = await session.skill("cheapest-battery")
        assert not denied["ok"]
        forecast = await session.skill("solar-forecast")
        assert forecast["ok"], forecast
        assert forecast["evidence"][0]["result"]["kind"] == "forecast"
        assert forecast["evidence"][-1]["analysis"]["result"]["data"]["sum"] == pytest.approx(22.68)
        dispatched = await session.dispatch("ENERGY_LIST_TOOLKITS", {})
        assert dispatched["toolkits"]


async def test_real_oauth_loopback_callback_state_and_replay(tmp_path):
    async def token(request):
        return httpx.Response(
            200,
            json={
                "access_token": "secret-token",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(token)) as http:
        store = AuthStore(tmp_path, Fernet.generate_key(), http=http)
        account = ConnectedAccount(
            id="a", user_id="u", toolkit="test", auth=AuthConfig(scheme="oauth")
        )
        store.configure(account, "")
        provider = OAuthProvider(
            authorization_endpoint="https://example.com/auth",
            token_endpoint="https://example.com/token",
            client_id="client",
            redirect_uri="http://127.0.0.1:8766/callback",
        )
        authorization = store.begin_oauth("u", "a", provider)
        app = callback_app(store, "u", authorization, provider.redirect_uri)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8766"
        ) as client:
            invalid = await client.get("/callback", params={"state": "wrong", "code": "code"})
            assert invalid.status_code == 400
            good = await client.get(
                "/callback", params={"state": authorization.state, "code": "code"}
            )
            assert good.json()["ok"] and "secret-token" not in good.text
            replay = await client.get(
                "/callback", params={"state": authorization.state, "code": "code"}
            )
            assert replay.status_code == 400
        assert store.credential("u", "a") == "secret-token"
        store.close()
