"""Job recovery must not transfer local operator artifacts into a hosted session."""

from pathlib import Path

import httpx
import pytest

from energy_agent_tools.app import build_agent
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.jobs import JobManager


@pytest.mark.asyncio
async def test_hosted_resume_cannot_inherit_local_operator_artifact_scope(tmp_path: Path):
    data = tmp_path / "operator"
    data.mkdir()
    (data / "private.csv").write_text("timestamp,value\n2026-10-01T00:00:00Z,1042.5\n")
    agent = build_agent(
        tmp_path / "agent",
        {
            "sites": [
                {
                    "id": "home",
                    "user_id": "tenant",
                    "name": "Home",
                    "timezone": "UTC",
                }
            ]
        },
        data_root=data,
    )
    local = agent.session("tenant", "home")
    try:
        imported = await agent.execute(
            local,
            "DATASET_IMPORT_CSV",
            {
                "file": "private.csv",
                "kind": "metered",
                "unit": "kWh",
                "timezone": "UTC",
                "quantity_shape": "interval",
            },
        )
        assert imported["ok"], imported
        artifact_id = imported["result"]["data"]["dataset_id"]
        agent._jobs = JobManager(agent._job_root)
        job = agent._jobs.submit(
            "tenant",
            local.id,
            "heat_loss",
            {
                "indoor_temp_c": 21,
                "outdoor_temp_c": 2,
                "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
                "air_changes_per_hour": 0.4,
            },
            site_id="home",
        )
        host = create_host(
            agent, {"tenant": Principal("tenant", {"home"}, token_digest("fixture"))}
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host),
            base_url="http://127.0.0.1:8000",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            resumed = await client.post(
                "/sessions", json={"site_id": "home", "resume_job_id": job.job_id}
            )
            if resumed.status_code == 200:
                # On the vulnerable host, prove the recovered ID actually exposes
                # the operator-imported rows before failing the intended denial.
                leaked = await client.post(
                    "/sessions/" + resumed.json()["session_id"] + "/execute",
                    json={"tool": "DATASET_PAGE", "arguments": {"artifact_id": artifact_id}},
                )
                assert "1042.5" in leaked.text
            assert resumed.status_code == 403
            assert local.id not in resumed.text and artifact_id not in resumed.text
    finally:
        await agent.close()
