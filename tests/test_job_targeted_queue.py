from __future__ import annotations

import json
import sqlite3

import pytest

from energy_agent_tools.jobs import (
    JobAccessDenied,
    JobError,
    JobManager,
    JobStatus,
)


def heat_loss_args() -> dict[str, object]:
    return {
        "indoor_temp_c": 21,
        "outdoor_temp_c": 2,
        "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
        "air_changes_per_hour": 0.4,
    }


def _replace_input(manager: JobManager, job_id: str, payload: bytes) -> None:
    input_path = manager.jobs_dir / job_id / "input.json"
    input_path.write_bytes(payload)
    with sqlite3.connect(manager.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET input_bytes = ? WHERE job_id = ?", (len(payload), job_id)
        )
        connection.commit()


@pytest.mark.asyncio
async def test_targeted_pending_queue_runs_only_selected_jobs_and_global_still_drains(tmp_path):
    root = tmp_path / "jobs"
    manager = JobManager(root)
    selected = manager.submit("user-a", "session-a", "heat_loss", heat_loss_args())
    foreign = manager.submit("user-b", "session-b", "heat_loss", heat_loss_args())
    manager.close()

    reopened = JobManager(root)
    try:
        completed = await reopened.run_pending(job_ids=frozenset({selected.job_id}))
        assert [record.job_id for record in completed] == [selected.job_id]
        assert completed[0].status is JobStatus.COMPLETED
        assert reopened.status(selected.job_id, "user-a", "session-a").status is JobStatus.COMPLETED
        assert reopened.status(foreign.job_id, "user-b", "session-b").status is JobStatus.PENDING
        result = reopened.result(selected.job_id, "user-a", "session-a")
        assert result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
        with pytest.raises(JobError) as terminal:
            reopened.pending_arguments(selected.job_id, "user-a", "session-a")
        assert terminal.value.code == "job_not_pending"

        rerun = await reopened.run_pending(job_ids=frozenset({selected.job_id}))
        assert [record.job_id for record in rerun] == [selected.job_id]
        assert rerun[0].status is JobStatus.COMPLETED

        globally_completed = await reopened.run_pending()
        assert {record.job_id for record in globally_completed} == {
            selected.job_id,
            foreign.job_id,
        }
        assert reopened.status(foreign.job_id, "user-b", "session-b").status is (
            JobStatus.COMPLETED
        )
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_empty_and_invalid_targeted_selections_do_not_run_jobs(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    try:
        job = manager.submit("user", "session", "heat_loss", heat_loss_args())
        assert await manager.run_pending(job_ids=frozenset()) == []
        assert manager.status(job.job_id, "user", "session").status is JobStatus.PENDING

        invalid_selections = [
            {job.job_id},
            frozenset({"not-a-job-id"}),
            frozenset(f"{index:032x}" for index in range(101)),
        ]
        for selection in invalid_selections:
            with pytest.raises(JobError) as invalid:
                await manager.run_pending(job_ids=selection)  # type: ignore[arg-type]
            assert invalid.value.code == "invalid_job_selection"
            assert str(invalid.value) == "The targeted job selection is invalid."

        assert manager.status(job.job_id, "user", "session").status is JobStatus.PENDING
        assert (await manager.run_pending())[0].status is JobStatus.COMPLETED
    finally:
        await manager.aclose()


def test_pending_arguments_are_owned_and_read_only(tmp_path):
    manager = JobManager(tmp_path / "jobs")
    try:
        job = manager.submit("user", "session", "heat_loss", heat_loss_args())
        assert manager.pending_arguments(job.job_id, "user", "session") == heat_loss_args()
        with pytest.raises(JobAccessDenied):
            manager.pending_arguments(job.job_id, "other-user", "session")
        with pytest.raises(JobAccessDenied):
            manager.pending_arguments(job.job_id, "user", "other-session")
        assert manager.status(job.job_id, "user", "session").status is JobStatus.PENDING
    finally:
        manager.close()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "symlink",
        "oversize",
        "invalid_json",
        "invalid_envelope",
        "schema_version",
        "operation_mismatch",
        "non_object_arguments",
        "forbidden_arguments",
        "non_finite_argument",
    ],
)
def test_pending_arguments_reject_corrupt_private_input_safely(tmp_path, damage):
    manager = JobManager(tmp_path / "jobs", max_input_bytes_per_job=512)
    try:
        job = manager.submit("user", "session", "heat_loss", heat_loss_args())
        input_path = manager.jobs_dir / job.job_id / "input.json"
        marker = "private-input-marker"
        if damage == "missing":
            input_path.unlink()
        elif damage == "symlink":
            outside = tmp_path / marker
            outside.write_bytes(b"do-not-read")
            input_path.unlink()
            try:
                input_path.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlinks are unavailable: {exc}")
        elif damage == "oversize":
            _replace_input(manager, job.job_id, b"x" * 513)
        else:
            envelope: object = {
                "schema_version": 1,
                "operation": "heat_loss",
                "arguments": heat_loss_args(),
            }
            if damage == "invalid_json":
                payload = b"not valid json"
            else:
                if damage == "invalid_envelope":
                    envelope = {"schema_version": 1, "operation": "heat_loss"}
                elif damage == "schema_version":
                    envelope["schema_version"] = 2  # type: ignore[index]
                elif damage == "operation_mismatch":
                    envelope["operation"] = "power_flow"  # type: ignore[index]
                elif damage == "non_object_arguments":
                    envelope["arguments"] = []  # type: ignore[index]
                elif damage == "forbidden_arguments":
                    envelope["arguments"] = {"token": marker}  # type: ignore[index]
                elif damage == "non_finite_argument":
                    envelope["arguments"] = {"value": float("nan")}  # type: ignore[index]
                payload = json.dumps(envelope, separators=(",", ":")).encode()
            _replace_input(manager, job.job_id, payload)

        with pytest.raises(JobError) as unavailable:
            manager.pending_arguments(job.job_id, "user", "session")
        assert unavailable.value.code == "input_unavailable"
        assert str(unavailable.value) == "The pending job input is unavailable."
        assert marker not in str(unavailable.value)
        assert str(input_path) not in str(unavailable.value)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_targeted_queue_does_not_retry_interrupted_jobs(tmp_path):
    root = tmp_path / "jobs"
    manager = JobManager(root)
    interrupted = manager.submit("user", "session", "heat_loss", heat_loss_args())
    with sqlite3.connect(manager.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', started_at = '2026-01-01T00:00:00+00:00' "
            "WHERE job_id = ?",
            (interrupted.job_id,),
        )
        connection.commit()
    manager.close()

    reopened = JobManager(root)
    try:
        assert reopened.status(interrupted.job_id, "user", "session").status is (
            JobStatus.INTERRUPTED
        )
        records = await reopened.run_pending(job_ids=frozenset({interrupted.job_id}))
        assert [record.status for record in records] == [JobStatus.INTERRUPTED]
        with pytest.raises(JobError) as terminal:
            reopened.pending_arguments(interrupted.job_id, "user", "session")
        assert terminal.value.code == "job_not_pending"
    finally:
        await reopened.aclose()
