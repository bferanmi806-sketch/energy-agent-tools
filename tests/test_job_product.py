from __future__ import annotations

import sqlite3

import httpx
import pytest
from cryptography.fernet import Fernet
from test_jobs import heat_loss_args

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.sdk import EnergyAgentTools


@pytest.mark.asyncio
async def test_public_job_discovery_recovery_preserves_original_session_and_artifacts(tmp_path):
    config = {
        "sites": [
            {"id": "site-a", "user_id": "actor", "name": "A", "timezone": "UTC"},
            {"id": "site-b", "user_id": "actor", "name": "B", "timezone": "UTC"},
        ]
    }
    async with EnergyAgentTools(tmp_path / "state", config) as energy:
        host = create_host(
            energy.agent, {"actor": Principal("actor", {"site-a"}, token_digest("fixture-key"))}
        )
        original = energy.session("actor", "site-a", access_mode="hosted")
        submitted = await original.job("submit", simulation="heat_loss", arguments=heat_loss_args())
        assert submitted["ok"]
        job_id = submitted["job"]["job_id"]
        await energy.agent._job_task
        persisted = await original.execute(
            "engineering.calculate_heat_loss", heat_loss_args(), persist=True
        )
        assert persisted["ok"]
        artifacts_before = energy.agent.workbench.list_artifacts(original.context)
        assert artifacts_before
        for site, workspace, mode in [
            ("site-b", None, "hosted"),
            ("site-a", "other", "hosted"),
            ("site-a", None, "local"),
        ]:
            foreign = energy.session("actor", site, workspace_id=workspace, access_mode=mode)
            assert (
                await foreign.job("submit", simulation="heat_loss", arguments=heat_loss_args())
            )["ok"]
        await energy.agent._job_task
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            assert (await client.post("/jobs", json={})).status_code == 401
            auth = {"Authorization": "Bearer fixture-key"}
            page = await client.post(
                "/jobs", headers=auth, json={"status": "completed", "limit": 1}
            )
            assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
            assert [job["job_id"] for job in page.json()["jobs"]] == [job_id]
            assert page.json()["next_before"] is None
            for invalid in [
                {"user_id": "actor"},
                {"site_ids": ["site-b"]},
                {"workspace_id": "other"},
                {"limit": True},
                {"limit": 101},
                {"status": "unknown"},
                {"before": "eA"},
            ]:
                assert (await client.post("/jobs", headers=auth, json=invalid)).status_code == 400
            registered = await client.post(
                "/sessions", headers=auth, json={"site_id": "site-a", "resume_job_id": job_id}
            )
            assert registered.status_code == 200
            stored_before = host._sessions[original.id]
            recovered = await client.post(
                f"/jobs/{job_id}", headers=auth, json={"operation": "result"}
            )
            assert recovered.status_code == 200 and recovered.json()["ok"]
            assert recovered.json()["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
            assert host._sessions[original.id] is stored_before
            assert energy.agent.workbench.list_artifacts(original.context) == artifacts_before
            assert (
                await client.post(f"/jobs/{job_id}", headers=auth, json={"operation": "submit"})
            ).status_code == 400
            deleted = await client.post(
                f"/jobs/{job_id}", headers=auth, json={"operation": "delete"}
            )
            assert deleted.json() == {"ok": True, "deleted": True}
            assert (
                await client.post(f"/jobs/{job_id}", headers=auth, json={"operation": "result"})
            ).status_code == 403
            assert (await client.post("/jobs", headers=auth, json={})).json()["jobs"] == []


@pytest.mark.asyncio
async def test_native_sdk_jobs_survive_backup_restore_without_original_session(tmp_path):
    config = {"sites": [{"id": "site", "user_id": "actor", "name": "A", "timezone": "UTC"}]}
    async with EnergyAgentTools(tmp_path / "state", config) as energy:
        original = energy.session("actor", "site")
        submitted = await original.job("submit", simulation="heat_loss", arguments=heat_loss_args())
        job_id = submitted["job"]["job_id"]
        await energy.agent._job_task
        reopened = energy.session("actor", "site")
        assert reopened.id != original.id
        assert reopened.job_history(status="completed")["jobs"][0]["job_id"] == job_id
        assert (await reopened.job_action(job_id, "result"))["result"]["data"][
            "gross_heat_loss_kw"
        ] == pytest.approx(0.38)
    manifest = create_backup(tmp_path / "state", tmp_path / "backup.tar.gz")
    assert next(item for item in manifest.files if item.kind == "jobs").schema == "jobs.v2"
    restore_backup(tmp_path / "backup.tar.gz", tmp_path / "restored")
    async with EnergyAgentTools(tmp_path / "restored", config) as energy:
        recovered = energy.session("actor", "site")
        assert recovered.job_history()["jobs"][0]["job_id"] == job_id
        assert (await recovered.job_action(job_id, "result"))["ok"]
        assert (await recovered.job_action(job_id, "delete"))["deleted"]
        assert recovered.job_history()["jobs"] == []


@pytest.mark.asyncio
async def test_managed_job_discovery_rechecks_current_grants_and_adopts_only_legacy_site(tmp_path):
    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    key = control.create_key(
        owner.user.id, owner.workspace.id, "Agent", access=AgentKeyAccess(site_ids=[site.id])
    )
    async with EnergyAgentTools(tmp_path / "state") as energy:
        energy.agent.auth_store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        host = create_host(energy.agent, {}, control_store=control, managed_workspaces=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            auth = {"Authorization": "Bearer " + key.token}
            created = await client.post("/sessions", headers=auth, json={"site_id": site.id})
            session_id = created.json()["session_id"]
            submitted = await client.post(
                f"/sessions/{session_id}/jobs",
                headers=auth,
                json={
                    "operation": "submit",
                    "simulation": "heat_loss",
                    "arguments": heat_loss_args(),
                },
            )
            assert submitted.json()["ok"]
            job_id = submitted.json()["job"]["job_id"]
            await energy.agent._job_task
            with sqlite3.connect(tmp_path / "state/jobs/jobs.sqlite3") as database:
                database.execute("UPDATE jobs SET workspace_id=NULL WHERE job_id=?", (job_id,))
            await client.delete(f"/sessions/{session_id}", headers=auth)
            page = await client.post("/jobs", headers=auth, json={})
            assert (
                page.status_code == 200
                and page.json()["jobs"][0]["workspace_id"] == owner.workspace.id
            )
            result = await client.post(
                f"/jobs/{job_id}", headers=auth, json={"operation": "result"}
            )
            assert result.json()["ok"]
            control.revoke_key(owner.user.id, owner.workspace.id, key.key.id)
            assert (await client.post("/jobs", headers=auth, json={})).status_code == 401
            assert (
                await client.post(f"/jobs/{job_id}", headers=auth, json={"operation": "result"})
            ).status_code == 401
    control.close()


@pytest.mark.asyncio
async def test_locked_and_corrupt_job_history_fail_safely_without_blocking_gateway(tmp_path):
    import time

    config = {"sites": [{"id": "site", "user_id": "actor", "name": "A", "timezone": "UTC"}]}
    async with EnergyAgentTools(tmp_path / "state", config) as energy:
        original = energy.session("actor", "site", access_mode="hosted")
        submitted = await original.job("submit", simulation="heat_loss", arguments=heat_loss_args())
        await energy.agent._job_task
        job_id = submitted["job"]["job_id"]
        host = create_host(
            energy.agent, {"actor": Principal("actor", {"site"}, token_digest("fixture-key"))}
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            auth = {"Authorization": "Bearer fixture-key"}
            assert (await client.post("/jobs", headers=auth, json={})).status_code == 200
            database = sqlite3.connect(tmp_path / "state/jobs/jobs.sqlite3")
            try:
                database.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                failed = await client.post("/jobs", headers=auth, json={})
                assert time.monotonic() - started < 2
                assert failed.status_code == 503
                assert failed.json()["error"]["code"] == "job_history_unavailable"
                assert (await client.get("/me", headers=auth)).status_code == 200
            finally:
                database.rollback()
                database.close()
            with sqlite3.connect(tmp_path / "state/jobs/jobs.sqlite3") as database:
                database.execute(
                    "UPDATE jobs SET error_code='https://private.invalid/?token=private-marker' WHERE job_id=?",
                    (job_id,),
                )
            failed = await client.post("/jobs", headers=auth, json={})
            assert failed.status_code == 503 and "private-marker" not in failed.text
            assert (
                await client.post(f"/jobs/{job_id}", headers=auth, json={"operation": "status"})
            ).status_code == 503
