from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from energy_agent_tools.jobs import (
    JobAccessDenied,
    JobError,
    JobManager,
    JobStatus,
    SimulationOperation,
)


def _arguments() -> dict[str, object]:
    return {
        "indoor_temp_c": 21,
        "outdoor_temp_c": 2,
        "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
        "air_changes_per_hour": 0.4,
    }


def _remove_access_mode_column(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        source_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()[0]
        legacy_sql = re.sub(
            r",\s*access_mode TEXT NOT NULL DEFAULT 'local'\s+"
            r"CHECK \(access_mode IN \('local', 'hosted'\)\)",
            "",
            source_sql,
        )
        assert legacy_sql != source_sql
        legacy_sql = re.sub(r"CREATE TABLE jobs\s*\(", "CREATE TABLE jobs_legacy (", legacy_sql)
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(jobs)")
            if str(row[1]) != "access_mode"
        ]
        columns_sql = ", ".join(f'"{column}"' for column in columns)
        indexes = [
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='jobs' "
                "AND sql IS NOT NULL"
            )
        ]
        connection.execute(legacy_sql)
        connection.execute(
            f"INSERT INTO jobs_legacy ({columns_sql}) SELECT {columns_sql} FROM jobs"
        )
        connection.execute("DROP TABLE jobs")
        connection.execute("ALTER TABLE jobs_legacy RENAME TO jobs")
        for index_sql in indexes:
            connection.execute(index_sql)


def test_local_jobs_cannot_be_resumed_in_hosted_mode(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    private_session_id = "local-session-private"
    job = manager.submit("owner", private_session_id, SimulationOperation.HEAT_LOSS, _arguments())

    assert job.access_mode == "local"
    assert job.as_dict()["access_mode"] == "local"
    assert manager.resume_scope(job.job_id, "owner", access_mode="local")["session_id"] == (
        private_session_id
    )
    with pytest.raises(JobAccessDenied) as caught:
        manager.resume_scope(job.job_id, "owner", access_mode="hosted")
    assert caught.value.code == "job_access_denied"
    assert private_session_id not in str(caught.value)
    manager.close()


def test_hosted_job_resumes_in_hosted_mode_after_reopen(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    private_session_id = "hosted-session-private"
    manager = JobManager(root)
    job = manager.submit(
        "owner",
        private_session_id,
        SimulationOperation.HEAT_LOSS,
        _arguments(),
        access_mode="hosted",
    )
    assert job.access_mode == "hosted"
    manager.close()

    reopened = JobManager(root)
    assert reopened.status(job.job_id, "owner", private_session_id).access_mode == "hosted"
    scope = reopened.resume_scope(job.job_id, "owner", access_mode="hosted")
    assert scope["session_id"] == private_session_id
    with pytest.raises(JobAccessDenied):
        reopened.resume_scope(job.job_id, "owner", access_mode="local")
    reopened.close()


@pytest.mark.asyncio
async def test_legacy_jobs_migrate_to_local_without_changing_their_data(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root)
    job = manager.submit(
        "owner", "legacy-session", SimulationOperation.HEAT_LOSS, _arguments(), site_id="home"
    )
    output = {"ok": True, "legacy_result": {"value": 12}}
    output_bytes = json.dumps(output).encode("utf-8")
    with sqlite3.connect(manager.database_path) as connection:
        output_path = Path(
            connection.execute(
                "SELECT output_path FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()[0]
        )
        output_path.write_bytes(output_bytes)
        connection.execute(
            """UPDATE jobs SET status = ?, output_bytes = ?, finished_at = ?
               WHERE job_id = ?""",
            (JobStatus.COMPLETED.value, len(output_bytes), "2026-10-01T00:00:00+00:00", job.job_id),
        )
    manager.close()

    _remove_access_mode_column(root / "jobs.sqlite3")

    migrated = JobManager(root)
    record = migrated.status(job.job_id, "owner", "legacy-session")
    assert record.access_mode == "local"
    assert record.status is JobStatus.COMPLETED
    assert record.site_id == "home"
    assert record.operation is SimulationOperation.HEAT_LOSS
    assert migrated.result(job.job_id, "owner", "legacy-session") == output
    assert migrated.resume_scope(job.job_id, "owner")["session_id"] == "legacy-session"
    with pytest.raises(JobAccessDenied):
        migrated.resume_scope(job.job_id, "owner", access_mode="hosted")
    migrated.close()


def test_invalid_access_modes_are_rejected_before_job_creation(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root)
    for access_mode in ("remote", "Hosted", None, 1):
        with pytest.raises(JobError) as caught:
            manager.submit(
                "owner",
                "session",
                SimulationOperation.HEAT_LOSS,
                _arguments(),
                access_mode=access_mode,  # type: ignore[arg-type]
            )
        assert caught.value.code == "invalid_access_mode"
    with sqlite3.connect(manager.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert [path for path in root.iterdir() if path.is_dir()] == []
    manager.close()


def test_corrupt_persisted_mode_and_invalid_resume_mode_are_denied(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    private_session_id = "corrupt-session-private"
    job = manager.submit("owner", private_session_id, SimulationOperation.HEAT_LOSS, _arguments())
    with sqlite3.connect(manager.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE jobs SET access_mode = 'unknown' WHERE job_id = ?", (job.job_id,)
        )

    for requested_mode in ("local", "hosted", "unknown"):
        with pytest.raises(JobAccessDenied) as caught:
            manager.resume_scope(
                job.job_id,
                "owner",
                access_mode=requested_mode,  # type: ignore[arg-type]
            )
        assert caught.value.code == "job_access_denied"
        assert private_session_id not in str(caught.value)
    with pytest.raises(JobAccessDenied) as caught:
        manager.status(job.job_id, "owner", private_session_id)
    assert caught.value.code == "job_access_denied"
    assert private_session_id not in str(caught.value)
    manager.close()
