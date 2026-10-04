from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from energy_agent_tools.activity import (
    ExecutionEntry,
    ExecutionFailure,
    ExecutionLogScope,
    ExecutionSuccess,
)
from energy_agent_tools.execution_log_store import ExecutionLogError, ExecutionLogStore
from energy_agent_tools.models import DataKind


def _entry(
    execution_id: str,
    *,
    user_id: str = "alice",
    workspace_id: str | None = "home",
    access_mode: str = "hosted",
    site_id: str | None = "site-a",
    account_id: str | None = "connection-a",
    tool: str = "energy.read",
    recorded_at: datetime | None = None,
) -> ExecutionEntry:
    return ExecutionEntry(
        execution_id=execution_id,
        user_id=user_id,
        workspace_id=workspace_id,
        key_id="key-a",
        session_id="session-a",
        site_id=site_id,
        account_id=account_id,
        access_mode=access_mode,  # type: ignore[arg-type]
        recorded_at=recorded_at or datetime.now(UTC),
        tool=tool,
        duration_ms=12.5,
        outcome=ExecutionSuccess(kind="success", data_kind=DataKind.METERED),
    )


def _scope(
    *,
    user_id: str = "alice",
    workspace_id: str | None = "home",
    access_mode: str = "hosted",
    site_ids: set[str | None] | None = None,
    connection_ids: set[str] | None = None,
) -> ExecutionLogScope:
    return ExecutionLogScope(
        user_id=user_id,
        workspace_id=workspace_id,
        access_mode=access_mode,  # type: ignore[arg-type]
        site_ids=site_ids if site_ids is not None else {"site-a"},
        connection_ids=connection_ids,
    )


def test_real_sqlite_persistence_and_same_namespace_retry(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    first = _entry("execution-1", recorded_at=datetime(2026, 10, 1, 10, tzinfo=UTC))
    stored = store.append(first)
    assert stored.sequence == 1
    assert stored.recorded_at.utcoffset() == timedelta(0)

    retried = store.append(first.model_copy(update={"tool": "different.tool"}))
    assert retried == stored
    with sqlite3.connect(tmp_path / "activity.sqlite3") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            database.execute(
                "SELECT COUNT(*) FROM execution_activity WHERE execution_id = 'execution-1'"
            ).fetchone()[0]
            == 1
        )
    store.close()

    reopened = ExecutionLogStore(tmp_path)
    page = reopened.read(_scope())
    assert page.entries == [stored]
    assert page.retention_limit == 2000
    assert page.recording_status == "ok"
    reopened.close()


def test_retry_collision_does_not_return_another_namespace_record(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    record = store.append(_entry("same-id", user_id="bob"))
    with pytest.raises(ExecutionLogError) as caught:
        store.append(_entry("same-id", user_id="alice"))
    assert str(caught.value) == "Execution ID is already in use."
    assert "bob" not in str(caught.value)
    assert record.user_id == "bob"
    assert store.read(_scope()).entries == []
    store.close()


def test_retention_is_per_scope_then_global_and_pagination_is_exclusive(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path, per_scope_limit=3, total_limit=5)
    for index in range(1, 7):
        store.append(_entry(f"alice-{index}"))
    page = store.read(_scope(), limit=2)
    assert [entry.execution_id for entry in page.entries] == ["alice-6", "alice-5"]
    assert page.next_before == page.entries[-1].sequence
    next_page = store.read(_scope(), limit=2, before=page.next_before)
    assert [entry.execution_id for entry in next_page.entries] == ["alice-4"]
    assert next_page.next_before is None
    assert next_page.retention_limit == 3

    store.append(_entry("bob-1", user_id="bob"))
    store.append(_entry("carol-1", user_id="carol"))
    store.append(_entry("carol-2", user_id="carol"))
    store.append(_entry("daisy-1", user_id="daisy"))
    store.append(_entry("daisy-2", user_id="daisy"))
    assert store.read(_scope()).entries == []
    assert store.read(_scope(user_id="bob")).entries[0].execution_id == "bob-1"
    assert [entry.execution_id for entry in store.read(_scope(user_id="carol")).entries] == [
        "carol-2",
        "carol-1",
    ]
    store.close()


def test_read_filters_actor_workspace_mode_site_and_connections(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    allowed = store.append(_entry("allowed"))
    public = store.append(_entry("public", account_id=None))
    store.append(_entry("other-account", account_id="connection-b"))
    store.append(_entry("other-site", site_id="site-b"))
    store.append(_entry("no-site", site_id=None, account_id="connection-a"))
    store.append(_entry("foreign-actor", user_id="bob"))
    store.append(_entry("foreign-workspace", workspace_id="other"))
    store.append(_entry("local-mode", access_mode="local"))

    result = store.read(
        _scope(site_ids={"site-a", None}, connection_ids={"connection-a"}), limit=100
    )
    assert [entry.execution_id for entry in result.entries] == ["no-site", "public", "allowed"]
    assert public in result.entries and allowed in result.entries

    explicit_empty = store.read(_scope(site_ids={"site-a", None}, connection_ids=set()), limit=100)
    assert [entry.execution_id for entry in explicit_empty.entries] == ["public"]
    no_null_site = store.read(_scope(site_ids={"site-a"}, connection_ids=None), limit=100)
    assert "no-site" not in {entry.execution_id for entry in no_null_site.entries}
    assert "other-account" in {entry.execution_id for entry in no_null_site.entries}
    store.close()


def test_large_site_and_connection_grants_are_filtered_in_stream(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    store.append(_entry("large-grant", site_id="site-1499", account_id="connection-1499"))
    scope = _scope(
        site_ids={f"site-{index}" for index in range(1500)},
        connection_ids={f"connection-{index}" for index in range(1500)},
    )
    page = store.read(scope)
    assert [entry.execution_id for entry in page.entries] == ["large-grant"]
    store.close()


def test_workspace_null_is_an_exact_namespace(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    local = store.append(
        _entry(
            "local-null-workspace",
            workspace_id=None,
            access_mode="local",
            site_id=None,
            account_id=None,
        )
    )
    store.append(_entry("hosted-workspace"))
    page = store.read(
        _scope(
            workspace_id=None,
            access_mode="local",
            site_ids={None},
            connection_ids=set(),
        )
    )
    assert page.entries == [local]
    store.close()


def test_invalid_limits_and_constructed_models_fail_with_fixed_messages(tmp_path: Path) -> None:
    for kwargs in (
        {"per_scope_limit": 0},
        {"per_scope_limit": True},
        {"total_limit": 10001},
    ):
        with pytest.raises(ExecutionLogError) as caught:
            ExecutionLogStore(tmp_path / str(len(kwargs)), **kwargs)
        assert str(caught.value) == "Execution log request is invalid."

    store = ExecutionLogStore(tmp_path / "valid")
    invalid_entry = ExecutionEntry.model_construct(
        execution_id="bad",
        user_id="alice",
        workspace_id="home",
        key_id=None,
        session_id="session",
        site_id="site-a",
        account_id=None,
        access_mode="hosted",
        recorded_at=datetime(2026, 1, 1),
        tool="tool",
        duration_ms=-1,
        outcome=ExecutionSuccess(kind="success"),
    )
    with pytest.raises(ExecutionLogError) as caught:
        store.append(invalid_entry)
    assert str(caught.value) == "Execution log request is invalid."
    with pytest.raises(ExecutionLogError) as caught:
        store.append({"execution_id": "raw"})  # type: ignore[arg-type]
    assert str(caught.value) == "Execution log request is invalid."

    for limit, before in ((0, None), (101, None), (True, None), (1, 0), (1, True)):
        with pytest.raises(ExecutionLogError) as caught:
            store.read(_scope(), limit=limit, before=before)
        assert str(caught.value) == "Execution log request is invalid."

    bad_scope = ExecutionLogScope.model_construct(
        user_id="alice",
        workspace_id="home",
        access_mode="operator",
        site_ids={"site-a"},
        connection_ids=None,
    )
    with pytest.raises(ExecutionLogError) as caught:
        store.read(bad_scope)
    assert str(caught.value) == "Execution log request is invalid."
    store.close()


def test_corrupt_visible_row_fails_instead_of_returning_valid_history(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    store.append(_entry("corrupt"))
    with sqlite3.connect(tmp_path / "activity.sqlite3") as database:
        database.execute(
            "UPDATE execution_activity SET outcome_json = ? WHERE execution_id = ?",
            ('{"kind":"failure","error_code":"safe","secret":"marker"}', "corrupt"),
        )

    with pytest.raises(ExecutionLogError) as caught:
        store.read(_scope())
    assert str(caught.value) == "Execution log contains invalid data."
    assert "marker" not in str(caught.value)
    store.close()


def test_failure_outcome_is_typed_and_round_trips(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    failed = ExecutionEntry(
        **{
            **_entry("failure", account_id=None).model_dump(mode="python"),
            "outcome": ExecutionFailure(kind="failure", error_code="provider_unavailable"),
        }
    )
    stored = store.append(failed)
    assert stored.outcome == ExecutionFailure(kind="failure", error_code="provider_unavailable")
    assert store.read(_scope()).entries == [stored]
    store.close()


def test_refuses_symlinks_and_preserves_the_target(tmp_path: Path) -> None:
    target = tmp_path / "private-target"
    target.write_text("do-not-touch")
    root = tmp_path / "activity"
    root.mkdir()
    (root / "activity.sqlite3").symlink_to(target)
    with pytest.raises(ExecutionLogError, match="storage is unavailable"):
        ExecutionLogStore(root)
    assert target.read_text() == "do-not-touch"
    other_root = tmp_path / "linked-root"
    other_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(ExecutionLogError, match="storage is unavailable"):
        ExecutionLogStore(other_root)


def test_lock_failure_is_bounded_and_returns_no_sql_details(tmp_path: Path) -> None:
    store = ExecutionLogStore(tmp_path)
    with sqlite3.connect(tmp_path / "activity.sqlite3") as locked:
        locked.execute("BEGIN EXCLUSIVE")
        with pytest.raises(ExecutionLogError) as caught:
            store.append(_entry("locked-execution"))
        assert str(caught.value) == "Execution log storage is unavailable."
        locked.rollback()
    assert store.append(_entry("recovered-execution")).execution_id == "recovered-execution"
    store.close()
