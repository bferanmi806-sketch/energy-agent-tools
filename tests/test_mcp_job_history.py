"""Exercise MCP job discovery and recovery through real FastMCP dispatch."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from energy_agent_tools.sdk import EnergyAgentTools

HEAT_LOSS_ARGS = {
    "indoor_temp_c": 21,
    "outdoor_temp_c": 2,
    "components": [{"name": "private-input-marker", "area_m2": 100, "u_value_w_m2k": 0.2}],
    "air_changes_per_hour": 0.4,
}
CONFIG = {
    "sites": [
        {"id": "home", "user_id": "alice", "name": "Home", "timezone": "UTC"},
    ]
}


async def _submit_heat_loss(session):
    submitted = await session.dispatch(
        "ENERGY_SIMULATION_JOB",
        {
            "operation": "submit",
            "simulation": "heat_loss",
            "arguments": HEAT_LOSS_ARGS,
        },
    )
    assert submitted["ok"], submitted
    return submitted["job"]["job_id"]


async def test_mcp_history_and_result_recover_across_sessions_without_exposing_payloads(tmp_path):
    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        original = energy.session("alice", "home")
        job_id = await _submit_heat_loss(original)
        await asyncio.wait_for(energy.agent._job_task, timeout=30)

        reconnected = energy.session("alice", "home")
        assert reconnected.id != original.id
        reconnect_id = reconnected.id
        page = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB",
            {"operation": "list", "status": "completed"},
        )
        assert page["ok"] is True
        assert page["next_before"] is None
        assert [item["job_id"] for item in page["jobs"]] == [job_id]

        metadata = page["jobs"][0]
        assert metadata["session_id"] == original.id
        assert metadata["status"] == "completed"
        assert set(metadata) == {
            "job_id",
            "user_id",
            "session_id",
            "workspace_id",
            "site_id",
            "access_mode",
            "operation",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "input_bytes",
            "output_bytes",
            "error_code",
        }
        serialized = json.dumps(page)
        for forbidden in (
            "private-input-marker",
            "input_path",
            "output_path",
            "state_dir",
            "error_message",
            str(tmp_path),
        ):
            assert forbidden not in serialized

        status = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB", {"operation": "status", "job_id": job_id}
        )
        assert status["ok"] and status["job"]["session_id"] == original.id
        result = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB", {"operation": "result", "job_id": job_id}
        )
        assert result["ok"], result
        assert result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)

        invalid_cursor = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB", {"operation": "list", "before": "eA"}
        )
        assert invalid_cursor["ok"] is False
        assert invalid_cursor["error"]["code"] == "invalid_cursor"

        deleted = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB", {"operation": "delete", "job_id": job_id}
        )
        assert deleted == {"ok": True, "deleted": True}
        assert reconnected.id == reconnect_id
        assert (await reconnected.dispatch("ENERGY_SIMULATION_JOB", {"operation": "list"}))[
            "jobs"
        ] == []


async def test_mcp_history_corruption_fails_safely_and_non_list_filters_are_rejected(tmp_path):
    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        original = energy.session("alice", "home")
        job_id = await _submit_heat_loss(original)
        await asyncio.wait_for(energy.agent._job_task, timeout=30)
        reconnected = energy.session("alice", "home")

        invalid_filters = [
            {"operation": "result", "job_id": job_id, "limit": 1},
            {"operation": "status", "job_id": job_id, "status": "completed"},
            {"operation": "cancel", "job_id": job_id, "before": "eA"},
            {"operation": "delete", "job_id": job_id, "limit": 1},
        ]
        for arguments in invalid_filters:
            rejected = await reconnected.dispatch("ENERGY_SIMULATION_JOB", arguments)
            assert rejected == {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "History filters apply only to job listing.",
                },
            }

        result = await reconnected.dispatch(
            "ENERGY_SIMULATION_JOB", {"operation": "result", "job_id": job_id}
        )
        assert result["ok"]
        assert result["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)

        marker = "https://private.invalid/private-marker"
        manager = energy.agent._jobs
        assert manager is not None
        with sqlite3.connect(manager.database_path) as database:
            database.execute("UPDATE jobs SET error_code = ? WHERE job_id = ?", (marker, job_id))
            database.commit()

        unavailable = await reconnected.dispatch("ENERGY_SIMULATION_JOB", {"operation": "list"})
        assert unavailable == {
            "ok": False,
            "error": {
                "code": "job_history_unavailable",
                "message": "Job history is unavailable.",
            },
        }
        serialized = json.dumps(unavailable)
        assert marker not in serialized
        assert str(manager.database_path) not in serialized
        assert "ValidationError" not in serialized


async def test_mcp_history_scopes_pages_status_and_filters_toolkits_before_paging(tmp_path):
    from benchmarks.engineering_environments import _pypsa_args

    async with EnergyAgentTools(tmp_path, CONFIG) as energy:
        target = energy.session("alice", None, workspace_id="workspace-a", access_mode="hosted")
        first = await _submit_heat_loss(target)
        await asyncio.wait_for(energy.agent._job_task, timeout=30)

        manager = energy.agent._jobs
        assert manager is not None
        await manager._run_lock.acquire()
        try:
            pending = await _submit_heat_loss(target)
            actor_boundary = await _submit_heat_loss(
                energy.session("bob", None, workspace_id="workspace-a", access_mode="hosted")
            )
            workspace_boundary = await _submit_heat_loss(
                energy.session("alice", None, workspace_id="workspace-b", access_mode="hosted")
            )
            site_boundary = await _submit_heat_loss(
                energy.session("alice", "home", workspace_id="workspace-a", access_mode="hosted")
            )
            mode_boundary = await _submit_heat_loss(
                energy.session("alice", None, workspace_id="workspace-a", access_mode="local")
            )

            flow = _pypsa_args(1.0, line_id="feeder")
            hidden_operation = await target.dispatch(
                "ENERGY_SIMULATION_JOB",
                {
                    "operation": "submit",
                    "simulation": "network_power_flow",
                    "arguments": flow,
                },
            )
            assert hidden_operation["ok"], hidden_operation
            hidden_id = hidden_operation["job"]["job_id"]

            # The task is blocked on the real manager lock, so both states are stable
            # while we verify status filters and cursor paging.
            pending_page = await target.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "list", "status": "pending"}
            )
            assert {item["job_id"] for item in pending_page["jobs"]} == {pending, hidden_id}
            assert all(item["status"] == "pending" for item in pending_page["jobs"])
            completed_page = await target.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "list", "status": "completed"}
            )
            assert [item["job_id"] for item in completed_page["jobs"]] == [first]

            toolkit_limited = energy.session(
                "alice",
                None,
                workspace_id="workspace-a",
                access_mode="hosted",
                toolkits={"engineering"},
            )
            first_page = await toolkit_limited.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "list", "limit": 1}
            )
            assert [item["job_id"] for item in first_page["jobs"]] == [pending]
            assert first_page["next_before"] is not None
            second_page = await toolkit_limited.dispatch(
                "ENERGY_SIMULATION_JOB",
                {"operation": "list", "limit": 1, "before": first_page["next_before"]},
            )
            assert [item["job_id"] for item in second_page["jobs"]] == [first]
            assert second_page["next_before"] is None

            reconnect = energy.session(
                "alice", None, workspace_id="workspace-a", access_mode="hosted"
            )
            cancelled = await reconnect.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "cancel", "job_id": hidden_id}
            )
            assert cancelled["ok"] and cancelled["job"]["status"] == "cancelled"
            removed = await reconnect.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "delete", "job_id": hidden_id}
            )
            assert removed == {"ok": True, "deleted": True}

            all_target = await target.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "list", "limit": 10}
            )
            assert {item["job_id"] for item in all_target["jobs"]} == {first, pending}
            for foreign_id in (actor_boundary, workspace_boundary, site_boundary, mode_boundary):
                assert foreign_id not in {item["job_id"] for item in all_target["jobs"]}
        finally:
            manager._run_lock.release()
        await asyncio.wait_for(energy.agent._job_task, timeout=60)


async def test_mcp_history_rechecks_revoked_workspace_access(tmp_path):
    import httpx
    from cryptography.fernet import Fernet

    from energy_agent_tools.auth import AuthStore
    from energy_agent_tools.control_contracts import AgentKeyAccess
    from energy_agent_tools.control_store import ControlStore
    from energy_agent_tools.hosting import create_host

    control = ControlStore(tmp_path / "control")
    owner = control.bootstrap_workspace("Owner", "Home")
    site = control.create_site(owner.user.id, owner.workspace.id, name="Home", timezone="UTC")
    key = control.create_key(
        owner.user.id,
        owner.workspace.id,
        "Agent",
        access=AgentKeyAccess(site_ids=[site.id]),
    )
    async with EnergyAgentTools(tmp_path / "state") as energy:
        energy.agent.auth_store = AuthStore(tmp_path / "vault", Fernet.generate_key())
        host = create_host(energy.agent, {}, control_store=control, managed_workspaces=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            created = await client.post(
                "/sessions",
                headers={"Authorization": f"Bearer {key.token}"},
                json={"site_id": site.id},
            )
            assert created.status_code == 200, created.text
            hosted_session = host._sessions[created.json()["session_id"]].session
            from energy_agent_tools.sdk import BoundSession

            bound = BoundSession(energy.agent, hosted_session)
            job_id = await _submit_heat_loss(bound)
            await asyncio.wait_for(energy.agent._job_task, timeout=30)
            page = await bound.dispatch(
                "ENERGY_SIMULATION_JOB", {"operation": "list", "status": "completed"}
            )
            assert page["ok"] and [item["job_id"] for item in page["jobs"]] == [job_id]

            control.revoke_key(owner.user.id, owner.workspace.id, key.key.id)
            revoked = await bound.dispatch("ENERGY_SIMULATION_JOB", {"operation": "list"})
            assert revoked["ok"] is False
            assert revoked["error"]["code"] == "workspace_forbidden"
    control.close()
