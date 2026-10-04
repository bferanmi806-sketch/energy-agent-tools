from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from energy_agent_tools.job_contracts import JobListQuery, JobReadScope
from energy_agent_tools.jobs import JobError, JobManager, JobStatus, SimulationOperation


def _heat_loss_args() -> dict[str, object]:
    return {
        "indoor_temp_c": 21,
        "outdoor_temp_c": 2,
        "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
        "air_changes_per_hour": 0.4,
    }


def _submit(
    manager: JobManager,
    user_id: str,
    session_id: str,
    *,
    workspace_id: str | None,
    access_mode: str,
    site_id: str | None,
) -> object:
    return manager.submit(
        user_id,
        session_id,
        SimulationOperation.HEAT_LOSS,
        _heat_loss_args(),
        workspace_id=workspace_id,
        access_mode=access_mode,  # type: ignore[arg-type]
        site_id=site_id,
    )


def _scope(
    *,
    user_id: str = "alice",
    workspace_id: str | None = "home",
    access_mode: str = "hosted",
    site_ids: set[str | None] | None = None,
) -> JobReadScope:
    return JobReadScope(
        user_id=user_id,
        workspace_id=workspace_id,
        access_mode=access_mode,  # type: ignore[arg-type]
        site_ids=site_ids if site_ids is not None else {"site-a"},
    )


def test_activity_survives_reopen_and_spans_originating_sessions(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manager = JobManager(root)
    try:
        first = _submit(
            manager,
            "alice",
            "session-one",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        second = _submit(
            manager,
            "alice",
            "session-two",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
    finally:
        manager.close()

    reopened = JobManager(root)
    try:
        page = reopened.metadata_page(_scope(), JobListQuery())
        assert {job.job_id for job in page.jobs} == {first.job_id, second.job_id}
        assert {job.session_id for job in page.jobs} == {"session-one", "session-two"}
        assert all(job.workspace_id == "home" for job in page.jobs)

        public_metadata = page.model_dump_json()
        for private_value in (
            "components",
            "indoor_temp_c",
            "air_changes_per_hour",
            "input_path",
            "output_path",
            "state_dir",
            str(tmp_path),
        ):
            assert private_value not in public_metadata
    finally:
        reopened.close()


def test_discovery_enforces_actor_workspace_mode_and_exact_site_scope(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    try:
        allowed = _submit(
            manager,
            "alice",
            "session-allowed",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        null_site = _submit(
            manager,
            "alice",
            "session-null-site",
            workspace_id="home",
            access_mode="hosted",
            site_id=None,
        )
        _submit(
            manager,
            "alice",
            "session-other-site",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-b",
        )
        _submit(
            manager,
            "alice",
            "session-other-workspace",
            workspace_id="other-home",
            access_mode="hosted",
            site_id="site-a",
        )
        _submit(
            manager,
            "alice",
            "session-local",
            workspace_id=None,
            access_mode="local",
            site_id="site-a",
        )
        _submit(
            manager,
            "bob",
            "session-foreign-actor",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        _submit(
            manager,
            "alice",
            "session-local-same-ids",
            workspace_id="home",
            access_mode="local",
            site_id="site-a",
        )

        page = manager.metadata_page(_scope(site_ids={"site-a", None}), JobListQuery(limit=100))
        assert {job.job_id for job in page.jobs} == {allowed.job_id, null_site.job_id}

        without_null = manager.metadata_page(_scope(site_ids={"site-a"}), JobListQuery(limit=100))
        assert {job.job_id for job in without_null.jobs} == {allowed.job_id}

        no_sites = manager.metadata_page(_scope(site_ids=set()), JobListQuery())
        assert no_sites.jobs == []
    finally:
        manager.close()


def test_cursor_pages_remain_stable_after_new_insert_and_prior_delete(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    try:
        jobs = [
            _submit(
                manager,
                "alice",
                f"session-{index}",
                workspace_id="home",
                access_mode="hosted",
                site_id="site-a",
            )
            for index in range(5)
        ]
        first_page = manager.metadata_page(_scope(), JobListQuery(limit=2))
        assert len(first_page.jobs) == 2
        assert first_page.next_before is not None

        _submit(
            manager,
            "alice",
            "session-newer",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        deleted = first_page.jobs[0]
        manager.cancel(deleted.job_id, "alice", deleted.session_id)
        manager.delete(deleted.job_id, "alice", deleted.session_id)

        second_page = manager.metadata_page(
            _scope(), JobListQuery(limit=2, before=first_page.next_before)
        )
        first_ids = {job.job_id for job in first_page.jobs}
        second_ids = {job.job_id for job in second_page.jobs}
        assert first_ids.isdisjoint(second_ids)
        assert deleted.job_id not in second_ids
        assert len(second_page.jobs) == 2
        assert not any(job.session_id == "session-newer" for job in second_page.jobs)

        third_page = manager.metadata_page(
            _scope(), JobListQuery(limit=2, before=second_page.next_before)
        )
        assert len(third_page.jobs) == 1
        assert first_ids.isdisjoint({job.job_id for job in third_page.jobs})
        assert second_ids.isdisjoint({job.job_id for job in third_page.jobs})
        assert {job.job_id for job in first_page.jobs + second_page.jobs + third_page.jobs} == {
            job.job_id for job in jobs
        }
    finally:
        manager.close()


def test_status_filter_invalid_cursor_and_corrupt_row_are_safe(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    try:
        pending = _submit(
            manager,
            "alice",
            "session-pending",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        cancelled = _submit(
            manager,
            "alice",
            "session-cancelled",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        manager.cancel(cancelled.job_id, "alice", "session-cancelled")

        cancelled_page = manager.metadata_page(_scope(), JobListQuery(status=JobStatus.CANCELLED))
        assert [job.job_id for job in cancelled_page.jobs] == [cancelled.job_id]
        pending_page = manager.metadata_page(_scope(), JobListQuery(status=JobStatus.PENDING))
        assert [job.job_id for job in pending_page.jobs] == [pending.job_id]

        with pytest.raises(JobError) as cursor_error:
            manager.metadata_page(_scope(), JobListQuery(before="YQ"))
        assert cursor_error.value.code == "invalid_cursor"
        assert str(cursor_error.value) == "Job history cursor is invalid."

        with sqlite3.connect(manager.database_path) as database:
            database.execute("PRAGMA ignore_check_constraints = ON")
            database.execute(
                "UPDATE jobs SET operation = ? WHERE job_id = ?",
                ("private-corruption-marker", pending.job_id),
            )
        with pytest.raises(JobError) as corrupt_error:
            manager.metadata_page(_scope(), JobListQuery())
        assert corrupt_error.value.code == "job_history_unavailable"
        assert str(corrupt_error.value) == "Job history is unavailable."
        assert "private-corruption-marker" not in str(corrupt_error.value)
    finally:
        manager.close()


def test_metadata_omits_argument_payload_raw_error_and_private_paths(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    try:
        job = _submit(
            manager,
            "alice",
            "session-private-metadata",
            workspace_id="home",
            access_mode="hosted",
            site_id="site-a",
        )
        raw_error = "private-error-message-marker"
        with sqlite3.connect(manager.database_path) as database:
            database.execute(
                """UPDATE jobs SET status = ?, finished_at = ?, error_code = ?, error_message = ?
                   WHERE job_id = ?""",
                (
                    JobStatus.FAILED.value,
                    "2026-10-04T10:00:00+00:00",
                    "provider_failure",
                    raw_error,
                    job.job_id,
                ),
            )

        page = manager.metadata_page(_scope(), JobListQuery())
        encoded = json.dumps(page.model_dump(mode="json"))
        for private_value in (
            "components",
            "indoor_temp_c",
            "air_changes_per_hour",
            raw_error,
            "input_path",
            "output_path",
            "state_dir",
            str(tmp_path),
        ):
            assert private_value not in encoded
        assert page.jobs[0].error_code == "provider_failure"
    finally:
        manager.close()
