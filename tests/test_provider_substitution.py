from __future__ import annotations

from pathlib import Path

import pytest

from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.models import DataKind, EnergyError
from energy_agent_tools.workflows import run_skill
from tests.fixtures.provider_sites import (
    SCENARIOS,
    WINDOW_END,
    WINDOW_START,
    build_provider_fixture,
)


@pytest.fixture(autouse=True)
def provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIXTURE_OCTOPUS_KEY", "octopus-fixture-secret")
    monkeypatch.setenv("FIXTURE_EMON_KEY", "emon-secret")


async def _close(agent: object) -> None:
    # The fixture supplies the AsyncClient, so EnergyAgent deliberately does
    # not own it.  Close both objects to keep the test suite leak-free.
    await agent.close()  # type: ignore[attr-defined]
    await agent.http.aclose()  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", sorted(SCENARIOS))
async def test_same_cost_workflow_substitutes_provider_sources(
    tmp_path: Path, provider: str
) -> None:
    """One reviewed workflow works with three independent source contracts."""

    fixture = build_provider_fixture(tmp_path / provider)
    agent = fixture.agent(tmp_path / f"{provider}-state")
    scenario = SCENARIOS[provider]
    try:
        session = agent.session(scenario.user_id, scenario.site_id)
        workflow = await run_skill(
            agent,
            session,
            "electricity-cost",
            {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        )
        assert workflow["ok"], workflow

        analysis = workflow["evidence"][-1]["analysis"]
        assert analysis["ok"], analysis
        result = analysis["result"]
        costs = [row["cost"] for row in result["data"]]
        assert sum(costs) == pytest.approx(scenario.expected_cost_gbp)

        consumption_evidence = next(
            item
            for item in workflow["evidence"]
            if item.get("capability") == "get_energy_consumption"
        )
        consumption = consumption_evidence["result"]
        consumption_artifact = agent.workbench.read(
            session, consumption["data"]["artifact_id"]
        ).model_dump(mode="json")
        assert sum(float(row["value"]) for row in consumption_artifact["data"]) == pytest.approx(
            scenario.expected_consumption_kwh
        )
        assert consumption_artifact["source"] == scenario.consumption_source
        assert consumption_artifact["site_id"] == scenario.site_id
        assert result["site_id"] == scenario.site_id

        # The derived cost keeps both provider inputs and their source metadata.
        input_sources = {
            entry.get("source")
            for entry in result["provenance"]
            if entry.get("input_kind") is not None
        }
        assert scenario.consumption_source in input_sources
        assert scenario.tariff_source in input_sources
        assert any(
            entry.get("input_kind") == DataKind.METERED.value for entry in result["provenance"]
        )
        assert any(
            entry.get("input_kind") == DataKind.CALCULATED.value for entry in result["provenance"]
        )

        if provider == "octopus":
            assert any(
                request.url.host == "api.octopus.energy" for request in fixture.http.requests
            )
        elif provider == "emon":
            assert any(request.url.host == "emon.example" for request in fixture.http.requests)
        else:
            assert not fixture.http.requests
    finally:
        await _close(agent)


async def test_account_choice_site_scope_and_cross_user_isolation(tmp_path: Path) -> None:
    """A user can choose a source explicitly without crossing site boundaries."""

    fixture = build_provider_fixture(tmp_path / "scope")
    agent = fixture.agent(tmp_path / "scope-state")
    try:
        broad = agent.session("alice")
        request = CapabilityRequest(
            capability="get_energy_consumption",
            kind=DataKind.METERED,
            arguments={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        )
        ambiguous = agent.resolver.resolve(broad, request)
        assert ambiguous["status"] == "ambiguous", ambiguous
        assert {candidate["account_id"] for candidate in ambiguous["candidates"]} >= {
            "octopus-alice-main",
            "octopus-alice-alt",
            "emon-alice",
            "csv-alice",
        }

        request.account_id = "octopus-alice-main"
        selected = agent.resolver.resolve(broad, request)
        assert selected["status"] == "resolved", selected
        assert selected["selected"]["account_id"] == "octopus-alice-main"
        execution = await agent.resolver.execute(broad, request, persist=True)
        assert execution["ok"], execution
        assert execution["result"]["site_id"] == "alice-octopus-site"
        assert execution["result"]["source"] == "octopus-energy"

        site_session = agent.session("alice", "alice-emon-site")
        site_resolution = agent.resolver.resolve(
            site_session,
            CapabilityRequest(
                capability="get_energy_consumption",
                kind=DataKind.METERED,
                arguments={
                    "start": WINDOW_START.isoformat(),
                    "end": WINDOW_END.isoformat(),
                },
            ),
        )
        assert site_resolution["status"] == "resolved", site_resolution
        assert site_resolution["selected"]["account_id"] == "emon-alice"

        with pytest.raises(EnergyError, match="Site"):
            agent.session("bob", "alice-emon-site")

        bob_request = CapabilityRequest(
            capability="get_energy_consumption",
            account_id="octopus-alice-main",
            kind=DataKind.METERED,
            arguments={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        )
        bob_result = await agent.resolver.execute(agent.session("bob"), bob_request)
        assert not bob_result["ok"]
        assert bob_result["error"]["code"] == "capability_unavailable"
    finally:
        await _close(agent)
