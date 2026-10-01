from datetime import UTC, datetime, timedelta

import pytest

from benchmarks.fixture import FIXTURE_SITE, FIXTURE_USER, write_fixture
from benchmarks.server import build_fixture_agent
from energy_agent_tools.models import DataKind, EnergyResult
from energy_agent_tools.sdk import EnergyAgentTools


async def test_reused_artifact_is_filtered_to_requested_half_open_window(tmp_path):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    async with EnergyAgentTools(tmp_path, agent=agent) as energy:
        session = energy.session(FIXTURE_USER, FIXTURE_SITE)
        begin = datetime(2026, 9, 28, 23, tzinfo=UTC)
        rows = [
            {"timestamp": (begin + timedelta(minutes=30 * i)).isoformat(), "value": 1}
            for i in range(-2, 50)
        ]
        source = EnergyResult(
            data=rows,
            kind=DataKind.METERED,
            unit="kWh",
            source="actual-test-meter",
            site_id=FIXTURE_SITE,
            resolution="30min",
        )
        ref = agent.workbench.persist(session.context, source)["artifact_id"]
        result = await session.skill(
            "yesterday-consumption", {"artifacts": {"get_energy_consumption": ref}}
        )
        assert result["ok"], result
        analysis = result["evidence"][-1]["analysis"]["result"]
        assert analysis["data"]["sum"] == 48
        assert analysis["provenance"][0]["input_kind"] == "metered"
        assert any(
            item.get("operation") == "window" and item.get("artifact_id") == ref
            for item in analysis["provenance"][0]["provenance"]
        )


async def test_requested_window_reports_edge_and_interior_missing_intervals(tmp_path):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    async with EnergyAgentTools(tmp_path, agent=agent) as energy:
        session = energy.session(FIXTURE_USER, FIXTURE_SITE)
        begin = datetime(2026, 9, 28, 23, tzinfo=UTC)
        rows = [
            {"timestamp": (begin + timedelta(minutes=30 * i)).isoformat(), "value": 1}
            for i in range(1, 47)
            if i != 20
        ]
        source = EnergyResult(
            data=rows, kind=DataKind.METERED, unit="kWh", source="test-meter", resolution="30min"
        )
        ref = agent.workbench.persist(session.context, source)["artifact_id"]
        result = await session.skill(
            "yesterday-consumption", {"artifacts": {"get_energy_consumption": ref}}
        )
        assert result["ok"], result
        analysis = result["evidence"][-1]["analysis"]["result"]
        assert analysis["data"]["sum"] == 45
        assert any("3 missing" in warning for warning in analysis["warnings"])


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-29T00:00:00", "2026-09-30T00:00:00Z"),
        ("2026-09-30T00:00:00Z", "2026-09-29T00:00:00Z"),
    ],
)
async def test_invalid_workflow_window_fails_before_provider_execution(tmp_path, start, end):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    async with EnergyAgentTools(tmp_path, agent=agent) as energy:
        result = await energy.session(FIXTURE_USER, FIXTURE_SITE).skill(
            "electricity-cost", {"start": start, "end": end}
        )
        assert not result["ok"]
        assert result["evidence"] == []
