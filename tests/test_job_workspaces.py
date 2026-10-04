from __future__ import annotations

import sqlite3

import pytest
from test_jobs import heat_loss_args

from energy_agent_tools.jobs import JobError, JobManager, SimulationOperation
from energy_agent_tools.sdk import EnergyAgentTools


def test_workspace_reference_is_durable_and_legacy_adoption_is_narrow(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    created = manager.submit(
        "actor",
        "session",
        SimulationOperation.HEAT_LOSS,
        heat_loss_args(),
        site_id="managed-site",
        access_mode="hosted",
        workspace_id="home",
    )
    legacy = manager.submit(
        "actor",
        "old-session",
        SimulationOperation.HEAT_LOSS,
        heat_loss_args(),
        site_id="managed-site",
        access_mode="hosted",
    )
    local = manager.submit(
        "actor",
        "local-session",
        SimulationOperation.HEAT_LOSS,
        heat_loss_args(),
        site_id="managed-site",
    )
    other_site = manager.submit(
        "actor",
        "other-session",
        SimulationOperation.HEAT_LOSS,
        heat_loss_args(),
        site_id="other-site",
        access_mode="hosted",
    )
    manager.adopt_legacy_hosted_workspace("managed-site", "home")
    manager.adopt_legacy_hosted_workspace("managed-site", "other-workspace")
    assert manager.status(created.job_id, "actor", "session").workspace_id == "home"
    assert manager.status(legacy.job_id, "actor", "old-session").workspace_id == "home"
    assert manager.status(local.job_id, "actor", "local-session").workspace_id is None
    assert manager.status(other_site.job_id, "actor", "other-session").workspace_id is None
    manager.close()
    reopened = JobManager(tmp_path / "jobs")
    assert reopened.status(created.job_id, "actor", "session").workspace_id == "home"
    with sqlite3.connect(tmp_path / "jobs/jobs.sqlite3") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 2
    reopened.close()


def test_future_job_schema_is_refused(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    with sqlite3.connect(root / "jobs.sqlite3") as database:
        database.execute("PRAGMA user_version=999")
    with pytest.raises(JobError, match="schema version is not supported"):
        JobManager(root)
    with sqlite3.connect(root / "jobs.sqlite3") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 999
        assert (
            database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )


@pytest.mark.asyncio
async def test_runtime_job_recovery_checks_workspace_even_with_same_actor_site_session(tmp_path):
    config = {"sites": [{"id": "site", "user_id": "actor", "name": "Fixture", "timezone": "UTC"}]}
    async with EnergyAgentTools(tmp_path / "state", config) as energy:
        home = energy.session("actor", "site", workspace_id="home", id="same-session")
        submitted = await home.job("submit", simulation="heat_loss", arguments=heat_loss_args())
        assert submitted["ok"] and submitted["job"]["workspace_id"] == "home"
        job_id = submitted["job"]["job_id"]
        other = energy.session("actor", "site", workspace_id="other", id="same-session")
        assert (await other.job("list"))["jobs"] == []
        for operation in ["resume", "status", "result", "cancel", "delete"]:
            denied = await other.job(operation, job_id=job_id)
            assert not denied["ok"] and denied["error"]["code"] == "workspace_forbidden"
        assert (await home.job("resume", job_id=job_id))["ok"]
