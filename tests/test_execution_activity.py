from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from test_hosting import _agent, _principals

from energy_agent_tools.activity import ExecutionLogScope
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import Session


def _scope(*, user="one", workspace=None, mode="local", sites=None):
    return ExecutionLogScope(
        user_id=user,
        workspace_id=workspace,
        access_mode=mode,
        site_ids={"one-a", "one-b"} if sites is None else sites,
        connection_ids=None,
    )


@pytest.mark.asyncio
async def test_real_runtime_outcomes_survive_restart_and_backup(tmp_path):
    agent = _agent(tmp_path)
    session = Session(user_id="one", site_id="one-a")
    success = await agent.execute(session, "FIXTURE_ENERGY", {"value": 3})
    failure = await agent.execute(session, "FIXTURE_ENERGY", {"value": "invalid"})
    assert success["ok"] and not failure["ok"]
    page = agent.execution_activity(_scope())
    assert [item.execution_id for item in page.entries] == [
        failure["execution_id"],
        success["execution_id"],
    ]
    assert page.entries[0].outcome.kind == "failure"
    assert page.entries[1].outcome.data_kind.value == "metered"
    assert page.entries[1].duration_ms >= 0
    assert page.entries[1].recorded_at.tzinfo is not None
    await agent.close()
    restarted = _agent(tmp_path)
    try:
        assert restarted.execution_activity(_scope()).entries == page.entries
        manifest = create_backup(tmp_path / "state", tmp_path / "backup.tar.gz")
        activity_file = next(item for item in manifest.files if item.kind == "activity")
        assert activity_file.path == "activity/activity.sqlite3"
        assert activity_file.schema == "activity.v1"
        restored = tmp_path / "restore-parent"
        restored.mkdir()
        restore_backup(tmp_path / "backup.tar.gz", restored / "state")
        recovered = _agent(restored)
        try:
            assert recovered.execution_activity(_scope()).entries == page.entries
        finally:
            await recovered.close()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_recording_failure_preserves_execution_and_exposes_health(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    try:
        agent.execution_activity(_scope())

        def fail(_entry):
            raise RuntimeError("private-storage-details")

        monkeypatch.setattr(agent._activity_store, "append", fail)
        result = await agent.execute(
            Session(user_id="one", site_id="one-a"), "FIXTURE_ENERGY", {"value": 3}
        )
        assert result["ok"]
        page = agent.execution_activity(_scope())
        assert page.entries == [] and page.recording_status == "unavailable"
        assert "private-storage-details" not in page.model_dump_json()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_cancelled_execution_keeps_cancellation_and_records_fixed_outcome(tmp_path):
    agent = _agent(tmp_path)
    started = asyncio.Event()

    async def block(_arguments, _context):
        started.set()
        await asyncio.Future()

    agent.registry.handlers["FIXTURE_ENERGY"] = block
    try:
        task = asyncio.create_task(
            agent.execute(Session(user_id="one", site_id="one-a"), "FIXTURE_ENERGY", {"value": 3})
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = agent.execution_activity(_scope()).entries[0]
        assert entry.outcome.kind == "failure" and entry.outcome.error_code == "cancelled"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_metadata_allowlist_omits_arguments_results_messages_and_unknown_urls(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    monkeypatch.setattr(
        agent,
        "_session_secrets",
        lambda _session: ["provider-private-marker", "owner-private-marker"],
    )
    session = Session(user_id="one", site_id="one-a")
    try:
        result = await agent.execute(
            session, "FIXTURE_ENERGY", {"value": 3, "private": "argument-marker"}
        )
        unknown = await agent.execute(
            session, "https://private.invalid/?token=provider-private-marker", {}
        )
        assert not unknown["ok"]
        page = agent.execution_activity(_scope())
        assert page.entries[0].tool == "unknown_tool"
        encoded = page.model_dump_json()
        for marker in [
            "provider-private-marker",
            "owner-private-marker",
            "argument-marker",
            "https://",
            '"arguments"',
            '"result"',
            '"message"',
        ]:
            assert marker not in encoded
        with sqlite3.connect(tmp_path / "state/activity/activity.sqlite3") as database:
            stored = json.dumps(database.execute("SELECT * FROM execution_activity").fetchall())
            assert "argument-marker" not in stored and "https://" not in stored
        assert result["execution_id"] in encoded
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_rest_activity_auth_current_site_actor_mode_paging_and_strict_query(tmp_path):
    agent = _agent(tmp_path)
    principals = _principals()
    host = create_host(agent, principals)
    try:
        for user, site, mode in [
            ("one", "one-a", "hosted"),
            ("one", "one-b", "hosted"),
            ("two", "two-a", "hosted"),
            ("one", "one-a", "local"),
        ]:
            await agent.execute(
                Session(user_id=user, site_id=site, access_mode=mode),
                "FIXTURE_ENERGY",
                {"value": 3},
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://local"
        ) as client:
            assert (await client.post("/activity", json={})).status_code == 401
            auth = {"Authorization": "Bearer one-token"}
            first = await client.post("/activity", headers=auth, json={"limit": 1})
            assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
            assert [e["site_id"] for e in first.json()["entries"]] == ["one-b"]
            second = await client.post(
                "/activity", headers=auth, json={"limit": 1, "before": first.json()["next_before"]}
            )
            assert [e["site_id"] for e in second.json()["entries"]] == ["one-a"]
            assert second.json()["next_before"] is None
            restricted_host = create_host(
                agent, {"one": Principal("one", {"one-a"}, token_digest("restricted-token"))}
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restricted_host), base_url="http://local"
            ) as restricted_client:
                restricted = await restricted_client.post(
                    "/activity", headers={"Authorization": "Bearer restricted-token"}, json={}
                )
            assert [e["site_id"] for e in restricted.json()["entries"]] == ["one-a"]
            for body in [
                {"user_id": "two"},
                {"site_ids": ["one-b"]},
                {"before": True},
                {"limit": 101},
                {"before": 0},
            ]:
                assert (await client.post("/activity", headers=auth, json=body)).status_code == 400
            with sqlite3.connect(tmp_path / "state/activity/activity.sqlite3") as database:
                database.execute(
                    "UPDATE execution_activity SET outcome_json='broken' WHERE user_id='one' AND access_mode='hosted'"
                )
            failed = await client.post("/activity", headers=auth, json={})
            assert (
                failed.status_code == 503
                and failed.json()["error"]["code"] == "activity_unavailable"
            )
            assert "broken" not in failed.text
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_bound_python_sdk_reads_current_site_history_without_session_reuse(tmp_path):
    from pydantic import ValidationError

    from energy_agent_tools.sdk import BoundSession

    agent = _agent(tmp_path)
    try:
        original = BoundSession(agent, Session(user_id="one", site_id="one-a"))
        executed = await original.execute("FIXTURE_ENERGY", {"value": 3})
        other_site = BoundSession(agent, Session(user_id="one", site_id="one-b"))
        await other_site.execute("FIXTURE_ENERGY", {"value": 4})
        reopened = BoundSession(agent, Session(user_id="one", site_id="one-a"))
        page = reopened.activity(limit=1)
        assert [item["execution_id"] for item in page["entries"]] == [executed["execution_id"]]
        assert page["next_before"] is None
        foreign = BoundSession(agent, Session(user_id="two", site_id="two-a"))
        assert foreign.activity()["entries"] == []
        with pytest.raises(ValidationError):
            reopened.activity(limit=101)
    finally:
        await agent.close()
