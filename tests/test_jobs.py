from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from energy_agent_tools.jobs import (
    JobAccessDenied,
    JobError,
    JobManager,
    JobQuotaExceeded,
    JobStatus,
    SimulationOperation,
)


def heat_loss_args() -> dict[str, object]:
    return {
        "indoor_temp_c": 21,
        "outdoor_temp_c": 2,
        "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
        "air_changes_per_hour": 0.4,
    }


@pytest.mark.asyncio
async def test_heat_loss_runs_through_real_private_worker(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = manager.submit("user-a", "session-a", SimulationOperation.HEAT_LOSS, heat_loss_args())

    await manager.run_pending()

    status = manager.status(job.job_id, "user-a", "session-a")
    assert status.status is JobStatus.COMPLETED
    result = manager.result(job.job_id, "user-a", "session-a")
    assert result["ok"] is True
    assert result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
    assert result["result"]["kind"] == "calculated"

    job_dir = tmp_path / "jobs" / job.job_id
    assert stat.S_IMODE((tmp_path / "jobs").stat().st_mode) == 0o700
    assert stat.S_IMODE(job_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((job_dir / "output.json").stat().st_mode) == 0o600
    assert not (job_dir / "input.json").exists()
    for path in (tmp_path / "jobs").rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_job_access_requires_both_owner_dimensions(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = manager.submit("user-a", "session-a", SimulationOperation.HEAT_LOSS, heat_loss_args())

    with pytest.raises(JobAccessDenied):
        manager.status(job.job_id, "user-b", "session-a")
    with pytest.raises(JobAccessDenied):
        manager.status(job.job_id, "user-a", "session-b")
    with pytest.raises(JobAccessDenied):
        manager.cancel(job.job_id, "user-b", "session-a")
    with pytest.raises(JobAccessDenied):
        manager.result(job.job_id, "user-a", "session-b")


def test_arguments_cannot_smuggle_credentials_or_execution_controls(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    with pytest.raises(Exception, match="credentials"):
        manager.submit("u", "s", "heat_loss", {**heat_loss_args(), "token": "secret"})
    with pytest.raises(Exception, match="commands|paths"):
        manager.submit("u", "s", "heat_loss", {**heat_loss_args(), "path": "/tmp/model"})


@pytest.mark.asyncio
async def test_cancel_running_job_terminates_real_worker(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs", timeout_seconds=15)
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    running = asyncio.create_task(manager.run_pending())
    for _ in range(100):
        await asyncio.sleep(0.005)
        if manager.status(job.job_id, "u", "s").status is JobStatus.RUNNING:
            break
    else:
        pytest.fail("job did not become running")
    cancelled = manager.cancel(job.job_id, "u", "s")
    assert cancelled.status is JobStatus.CANCELLED
    await running
    assert manager.status(job.job_id, "u", "s").status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_timeout_marks_job_failed_and_kills_real_process(tmp_path: Path) -> None:
    # A tiny deadline exercises the actual subprocess startup and process-group
    # termination path without needing an artificial worker operation.
    manager = JobManager(tmp_path / "jobs", timeout_seconds=0.001)
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())

    await manager.run_pending()

    status = manager.status(job.job_id, "u", "s")
    assert status.status is JobStatus.FAILED
    assert status.error_code == "timeout"


def test_restart_marks_abandoned_running_but_preserves_queue(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root)
    running = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    pending = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    with sqlite3.connect(manager.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', started_at = '2026-01-01T00:00:00+00:00' WHERE job_id = ?",
            (running.job_id,),
        )
        connection.commit()
    manager.close()

    restarted = JobManager(root)

    assert restarted.status(running.job_id, "u", "s").status is JobStatus.INTERRUPTED
    assert restarted.status(pending.job_id, "u", "s").status is JobStatus.PENDING


def test_live_managers_cannot_share_a_root(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root)
    with pytest.raises(JobError) as error:
        JobManager(root)
    assert error.value.code == "manager_locked"
    manager.close()


@pytest.mark.asyncio
async def test_aclose_interrupts_workers_and_releases_root(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root, timeout_seconds=15)
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    runner = asyncio.create_task(manager.run_pending())
    for _ in range(100):
        await asyncio.sleep(0.005)
        if manager.status(job.job_id, "u", "s").status is JobStatus.RUNNING:
            break
    else:
        pytest.fail("job did not become running")
    await manager.aclose()
    await runner

    reopened = JobManager(root)
    assert reopened.status(job.job_id, "u", "s").status is JobStatus.INTERRUPTED
    reopened.close()


def test_resume_scope_is_owner_only_and_contains_no_payload(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = manager.submit(
        "u",
        "old-session",
        SimulationOperation.HEAT_LOSS,
        heat_loss_args(),
        site_id="site-a",
    )
    assert manager.resume_scope(job.job_id, "u") == {
        "job_id": job.job_id,
        "session_id": "old-session",
        "site_id": "site-a",
    }
    with pytest.raises(JobAccessDenied):
        manager.resume_scope(job.job_id, "other-user")


def test_delete_frees_terminal_count_quota(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs", max_jobs_per_user=1)
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    manager.cancel(job.job_id, "u", "s")
    manager.delete(job.job_id, "u", "s")
    replacement = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    assert replacement.status is JobStatus.PENDING


def test_timeout_must_be_finite(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JobManager(tmp_path / "nan", timeout_seconds=float("nan"))
    with pytest.raises(ValueError):
        JobManager(tmp_path / "inf", timeout_seconds=float("inf"))


def test_input_output_and_job_quotas_are_durable(tmp_path: Path) -> None:
    manager = JobManager(
        tmp_path / "jobs",
        max_jobs_per_user=1,
        max_output_bytes_per_job=1,
        max_output_bytes_per_user=1,
        max_output_bytes_global=1,
    )
    manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    with pytest.raises(JobQuotaExceeded, match="job count"):
        manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())

    small = JobManager(tmp_path / "small", max_input_bytes=10)
    with pytest.raises(JobQuotaExceeded, match="input"):
        small.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())


@pytest.mark.asyncio
async def test_output_limit_is_recorded_when_worker_result_is_too_large(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs", max_output_bytes=1)
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())

    await manager.run_pending()

    status = manager.status(job.job_id, "u", "s")
    assert status.status is JobStatus.FAILED
    assert status.error_code == "output_limit"
    assert not (tmp_path / "jobs" / job.job_id / "output.json").exists()


def test_status_does_not_echo_private_paths(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = manager.submit("u", "s", SimulationOperation.HEAT_LOSS, heat_loss_args())
    encoded = json.dumps(manager.status(job.job_id, "u", "s").as_dict())
    assert "input.json" not in encoded
    assert str(tmp_path) not in encoded


@pytest.mark.asyncio
async def test_legacy_job_constraint_migrates_without_losing_results_or_scope(tmp_path):
    import re

    root = tmp_path / "jobs"
    original = JobManager(root)
    job = original.submit(
        "owner", "session", SimulationOperation.HEAT_LOSS, heat_loss_args(), site_id="home"
    )
    await original.run_pending()
    original.close()
    legacy_constraint = "CHECK (operation IN ('heat_loss', 'power_flow', 'battery', 'solar'))"
    with sqlite3.connect(root / "jobs.sqlite3") as connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        old_sql = re.sub(r"CHECK \(operation IN \([^)]*\)\)", legacy_constraint, sql)
        old_sql = old_sql.replace("CREATE TABLE jobs (", "CREATE TABLE legacy_jobs (")
        connection.execute(old_sql)
        connection.execute("INSERT INTO legacy_jobs SELECT * FROM jobs")
        connection.execute("DROP TABLE jobs")
        connection.execute("ALTER TABLE legacy_jobs RENAME TO jobs")
    upgraded = JobManager(root)
    try:
        record = upgraded.status(job.job_id, "owner", "session")
        assert record.status is JobStatus.COMPLETED
        assert record.site_id == "home"
        assert (
            upgraded.result(job.job_id, "owner", "session")["result"]["data"]["gross_heat_loss_kw"]
            == 0.38
        )
        queued = upgraded.submit(
            "owner",
            "session",
            SimulationOperation.NETWORK_POWER_FLOW,
            {"network": {}},
            site_id="home",
        )
        assert queued.operation is SimulationOperation.NETWORK_POWER_FLOW
        assert upgraded.status(queued.job_id, "owner", "session").status is JobStatus.PENDING
        with sqlite3.connect(root / "jobs.sqlite3") as connection:
            indexes = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            assert {"jobs_user_session_idx", "jobs_pending_idx"} <= indexes
    finally:
        upgraded.close()
    reopened = JobManager(root)
    try:
        assert reopened.status(job.job_id, "owner", "session").status is JobStatus.COMPLETED
        assert reopened.status(queued.job_id, "owner", "session").site_id == "home"
    finally:
        reopened.close()


def test_numerical_worker_uses_bundled_fonts_without_inheriting_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ENERGY_VAULT_KEY", "must-not-enter-worker")
    monkeypatch.setenv("MPLCONFIGDIR", "must-not-enter-worker")
    environment = JobManager._safe_environment(tmp_path)
    assert environment["MPL_IGNORE_SYSTEM_FONTS"] == "1"
    assert "ENERGY_VAULT_KEY" not in environment
    assert "MPLCONFIGDIR" not in environment
    assert Path(environment["HOME"]) == tmp_path / "home"
