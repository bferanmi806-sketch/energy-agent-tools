"""Starting recovered simulation jobs requires fresh scope and an explicit selection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess, ManageKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host
from energy_agent_tools.jobs import JobManager, JobStatus, SimulationOperation
from energy_agent_tools.models import Action
from energy_agent_tools.sdk import BoundSession

HEAT_LOSS_ARGS = {
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
    workspace_id: str,
    site_id: str,
    operation: SimulationOperation = SimulationOperation.HEAT_LOSS,
    arguments: dict[str, object] | None = None,
):
    return manager.submit(
        user_id,
        session_id,
        operation,
        HEAT_LOSS_ARGS if arguments is None else arguments,
        site_id=site_id,
        access_mode="hosted",
        workspace_id=workspace_id,
    )


async def _request_job_action(client, headers, job_id: str, operation: str):
    return await client.post(f"/jobs/{job_id}", headers=headers, json={"operation": operation})


@pytest.mark.asyncio
async def test_recovered_start_runs_only_explicit_jobs_through_root_and_mcp(
    tmp_path: Path, monkeypatch
):
    state = tmp_path / "runtime"
    controls = ControlStore(tmp_path / "control")
    owner = controls.bootstrap_workspace("Owner", "Home")
    home = controls.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    other_site = controls.create_site(
        owner.user.id, owner.workspace.id, name="Workshop", timezone="UTC"
    )
    foreign_workspace = controls.create_workspace(owner.user.id, "Other workspace", mode="managed")
    foreign_site = controls.create_site(
        owner.user.id, foreign_workspace.id, name="Other home", timezone="UTC"
    )
    foreign_manager = controls.create_key(
        owner.user.id,
        foreign_workspace.id,
        "Other workspace manager",
        access=ManageKeyAccess(),
    )
    home_agent = controls.create_key(
        owner.user.id,
        owner.workspace.id,
        "Home agent",
        access=AgentKeyAccess(site_ids=[home.id]),
    )
    other_site_agent = controls.create_key(
        owner.user.id,
        owner.workspace.id,
        "Workshop only agent",
        access=AgentKeyAccess(site_ids=[other_site.id]),
    )

    manager = JobManager(state / "jobs")
    root_job = _submit(
        manager,
        owner.user.id,
        "origin-root-session",
        workspace_id=owner.workspace.id,
        site_id=home.id,
    )
    mcp_job = _submit(
        manager,
        owner.user.id,
        "origin-mcp-session",
        workspace_id=owner.workspace.id,
        site_id=home.id,
    )
    old_queue = _submit(
        manager,
        owner.user.id,
        "origin-old-queue-session",
        workspace_id=owner.workspace.id,
        site_id=home.id,
    )
    foreign_job = _submit(
        manager,
        owner.user.id,
        "origin-foreign-session",
        workspace_id=foreign_workspace.id,
        site_id=foreign_site.id,
    )
    completed_job = _submit(
        manager,
        owner.user.id,
        "origin-completed-session",
        workspace_id=owner.workspace.id,
        site_id=home.id,
    )
    await manager.run_pending(job_ids=frozenset({completed_job.job_id}))
    assert (
        manager.status(completed_job.job_id, owner.user.id, "origin-completed-session").status
        is JobStatus.COMPLETED
    )

    interrupted_job = _submit(
        manager,
        owner.user.id,
        "origin-interrupted-session",
        workspace_id=owner.workspace.id,
        site_id=home.id,
    )
    with sqlite3.connect(manager.database_path) as database:
        database.execute(
            "UPDATE jobs SET status = ?, started_at = created_at WHERE job_id = ?",
            (JobStatus.RUNNING.value, interrupted_job.job_id),
        )
    manager.close()

    agent = build_agent(state)
    agent.auth_store = AuthStore(state / "vault", Fernet.generate_key())
    host = create_host(agent, {}, control_store=controls, managed_workspaces=True)
    owner_headers = {"Authorization": f"Bearer {owner.key.token}"}
    foreign_headers = {"Authorization": f"Bearer {foreign_manager.token}"}
    site_headers = {"Authorization": f"Bearer {other_site_agent.token}"}
    worker_headers = {"Authorization": f"Bearer {home_agent.token}"}
    run_ids: list[str] = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://127.0.0.1:8000"
        ) as client:
            assert (await client.get("/me", headers=owner_headers)).status_code == 200
            recovered = await client.post("/jobs", headers=owner_headers, json={"limit": 100})
            assert recovered.status_code == 200, recovered.text
            recovered_by_id = {item["job_id"]: item for item in recovered.json()["jobs"]}
            for pending in (root_job, mcp_job, old_queue):
                assert recovered_by_id[pending.job_id]["status"] == "pending"
            assert recovered_by_id[completed_job.job_id]["status"] == "completed"
            assert recovered_by_id[interrupted_job.job_id]["status"] == "interrupted"
            assert foreign_job.job_id not in recovered_by_id

            manager = agent._jobs
            assert manager is not None
            original_run_one = manager._run_one

            async def count_worker_start(row):
                run_ids.append(str(row["job_id"]))
                await original_run_one(row)

            monkeypatch.setattr(manager, "_run_one", count_worker_start)
            await manager._run_lock.acquire()
            try:
                first_start = await _request_job_action(
                    client, owner_headers, root_job.job_id, "start"
                )
                assert first_start.status_code == 200, first_start.text
                first_body = first_start.json()
                assert first_body["ok"] is True
                assert first_body["job"]["job_id"] == root_job.job_id
                assert first_body["job"]["session_id"] == "origin-root-session"
                assert first_body["job"]["status"] == "pending"

                repeated_start = await _request_job_action(
                    client, owner_headers, root_job.job_id, "start"
                )
                assert repeated_start.status_code == 200, repeated_start.text
                assert repeated_start.json()["ok"] is True

                denied_foreign = await _request_job_action(
                    client, foreign_headers, root_job.job_id, "start"
                )
                assert denied_foreign.status_code == 403
                denied_site = await _request_job_action(
                    client, site_headers, root_job.job_id, "start"
                )
                assert denied_site.status_code == 403

                for terminal in (completed_job, interrupted_job):
                    rejected = await _request_job_action(
                        client, owner_headers, terminal.job_id, "start"
                    )
                    assert rejected.status_code == 200, rejected.text
                    assert rejected.json() == {
                        "ok": False,
                        "error": {
                            "code": "job_not_pending",
                            "message": "Only pending jobs can be started.",
                        },
                    }

                session_response = await client.post(
                    "/sessions", headers=worker_headers, json={"site_id": home.id}
                )
                assert session_response.status_code == 200, session_response.text
                new_session_id = session_response.json()["session_id"]
                context = host._sessions[new_session_id].session
                mcp_started = await BoundSession(agent, context).dispatch(
                    "ENERGY_SIMULATION_JOB",
                    {"operation": "start", "job_id": mcp_job.job_id},
                )
                assert mcp_started["ok"] is True, mcp_started
                assert mcp_started["job"]["job_id"] == mcp_job.job_id
                assert mcp_started["job"]["session_id"] == "origin-mcp-session"
                assert context.id == new_session_id

                submitted = await client.post(
                    f"/sessions/{new_session_id}/jobs",
                    headers=worker_headers,
                    json={
                        "operation": "submit",
                        "simulation": "heat_loss",
                        "arguments": HEAT_LOSS_ARGS,
                    },
                )
                assert submitted.status_code == 200, submitted.text
                submitted_job = submitted.json()["job"]
                assert submitted_job["job_id"] not in {
                    root_job.job_id,
                    mcp_job.job_id,
                    old_queue.job_id,
                    foreign_job.job_id,
                }
            finally:
                manager._run_lock.release()

            assert agent._job_task is not None
            await asyncio.wait_for(agent._job_task, timeout=60)
            assert Counter(run_ids) == Counter(
                [root_job.job_id, mcp_job.job_id, submitted_job["job_id"]]
            )
            for job_id, original_session in (
                (root_job.job_id, "origin-root-session"),
                (mcp_job.job_id, "origin-mcp-session"),
                (submitted_job["job_id"], new_session_id),
            ):
                status = await _request_job_action(client, owner_headers, job_id, "status")
                assert status.status_code == 200, status.text
                assert status.json()["job"]["status"] == "completed"
                assert status.json()["job"]["session_id"] == original_session

            result = await _request_job_action(client, owner_headers, root_job.job_id, "result")
            assert result.status_code == 200, result.text
            assert result.json()["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
            assert (
                manager.status(old_queue.job_id, owner.user.id, "origin-old-queue-session").status
                is JobStatus.PENDING
            )
            assert (
                manager.status(foreign_job.job_id, owner.user.id, "origin-foreign-session").status
                is JobStatus.PENDING
            )
    finally:
        await agent.close()
        controls.close()


@pytest.mark.asyncio
async def test_start_rechecks_current_runtime_scope_policy_and_saved_inputs(
    tmp_path: Path, monkeypatch
):
    config = {
        "sites": [
            {"id": "home", "user_id": "alice", "name": "Home", "timezone": "UTC"},
            {"id": "other", "user_id": "alice", "name": "Other", "timezone": "UTC"},
        ]
    }
    root = tmp_path / "runtime"
    reopened = build_agent(root, config)
    initial = JobManager(reopened._job_root)
    reopened._jobs = initial
    running = _submit(
        initial,
        "alice",
        "origin-running",
        workspace_id="workspace-a",
        site_id="home",
    )
    claimed = initial._claim_pending(1)
    assert len(claimed) == 1 and claimed[0]["job_id"] == running.job_id

    target = _submit(
        initial,
        "alice",
        "origin-target",
        workspace_id="workspace-a",
        site_id="home",
    )
    missing = _submit(
        initial,
        "alice",
        "origin-missing",
        workspace_id="workspace-a",
        site_id="home",
    )
    (initial.jobs_dir / missing.job_id / "input.json").unlink()
    marker = "private-saved-input-marker"
    invalid = _submit(
        initial,
        "alice",
        "origin-invalid",
        workspace_id="workspace-a",
        site_id="home",
        arguments={"private": marker},
    )
    hook_job = _submit(
        initial,
        "alice",
        "origin-hook",
        workspace_id="workspace-a",
        site_id="home",
    )
    dependency_job = _submit(
        initial,
        "alice",
        "origin-dependency",
        workspace_id="workspace-a",
        site_id="home",
        operation=SimulationOperation.SOLAR,
        arguments={},
    )
    try:
        session = reopened.session(
            "alice", "home", workspace_id="workspace-a", access_mode="hosted"
        )
        manager = reopened._jobs
        assert manager is initial

        repeated = await reopened.job(session, "start", job_id=running.job_id)
        assert repeated["ok"] is True
        assert repeated["job"]["status"] == "running"
        repeated_again = await reopened.job(session, "start", job_id=running.job_id)
        assert repeated_again["ok"] is True
        assert repeated_again["job"]["status"] == "running"
        assert reopened._job_task is None
        assert reopened._queued_job_ids == set()

        readonly = reopened.session(
            "alice",
            "home",
            workspace_id="workspace-a",
            access_mode="hosted",
            allowed_actions={Action.READ},
        )
        denied_readonly = await reopened.job(readonly, "start", job_id=target.job_id)
        assert denied_readonly["error"]["code"] == "policy_denied"
        calculate_only = reopened.session(
            "alice",
            "home",
            workspace_id="workspace-a",
            access_mode="hosted",
            allowed_actions={Action.CALCULATE},
        )
        denied_without_read = await reopened.job(calculate_only, "start", job_id=target.job_id)
        assert denied_without_read["error"]["code"] == "policy_denied"

        scope_cases = [
            (
                reopened.session("bob", None, workspace_id="workspace-a", access_mode="hosted"),
                "job_access_denied",
            ),
            (
                reopened.session("alice", "home", workspace_id="workspace-b", access_mode="hosted"),
                "workspace_forbidden",
            ),
            (
                reopened.session(
                    "alice", "other", workspace_id="workspace-a", access_mode="hosted"
                ),
                "site_forbidden",
            ),
            (
                reopened.session("alice", "home", workspace_id="workspace-a", access_mode="local"),
                "job_access_denied",
            ),
            (
                reopened.session(
                    "alice",
                    "home",
                    workspace_id="workspace-a",
                    access_mode="hosted",
                    toolkits={"workbench"},
                ),
                "tool_forbidden",
            ),
        ]
        for restricted, expected_code in scope_cases:
            response = await reopened.job(restricted, "start", job_id=target.job_id)
            assert response["ok"] is False
            assert response["error"]["code"] == expected_code

        reopened.before.append(lambda tool, arguments, current: arguments)
        try:
            hook_denial = await reopened.job(session, "start", job_id=hook_job.job_id)
        finally:
            reopened.before.clear()
        assert hook_denial["error"]["code"] == "job_hooks_unsupported"

        import energy_agent_tools.runtime as runtime_module

        actual_find_spec = runtime_module.find_spec
        monkeypatch.setattr(
            runtime_module,
            "find_spec",
            lambda name: None if name == "pvlib" else actual_find_spec(name),
        )
        dependency_denial = await reopened.job(session, "start", job_id=dependency_job.job_id)
        assert dependency_denial["error"]["code"] == "dependency_missing"
        monkeypatch.setattr(runtime_module, "find_spec", actual_find_spec)

        missing_error = await reopened.job(session, "start", job_id=missing.job_id)
        assert missing_error == {
            "ok": False,
            "error": {
                "code": "input_unavailable",
                "message": "The pending job input is unavailable.",
            },
        }
        assert str(root) not in json.dumps(missing_error)
        invalid_error = await reopened.job(session, "start", job_id=invalid.job_id)
        assert invalid_error["ok"] is False
        assert invalid_error["error"]["code"] == "invalid_arguments"
        assert marker not in json.dumps(invalid_error)
        assert reopened._job_task is None
        assert reopened._queued_job_ids == set()
        assert (
            reopened._jobs.status(target.job_id, "alice", "origin-target").status
            is JobStatus.PENDING
        )
        assert (
            reopened._jobs.status(missing.job_id, "alice", "origin-missing").status
            is JobStatus.PENDING
        )
        assert (
            reopened._jobs.status(invalid.job_id, "alice", "origin-invalid").status
            is JobStatus.PENDING
        )
    finally:
        await reopened.close()
