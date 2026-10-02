from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult, Session
from energy_agent_tools.partitioned_timeseries import PartitionedTimeseriesStore
from energy_agent_tools.workbench import Workbench

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _metadata(
    *,
    provenance: list[dict[str, object]] | None = None,
    resolution: str | None = None,
) -> EnergyResult:
    return EnergyResult(
        data=[],
        kind=DataKind.METERED,
        unit="kWh",
        source="time-index-fixture",
        resolution=resolution,
        provenance=provenance or [{"provider": "fixture"}],
    )


def _row_at(minute: int, *, end_minutes: int = 1, value: int | None = None) -> dict[str, object]:
    start = _BASE + timedelta(minutes=minute)
    end = start + timedelta(minutes=end_minutes)
    return {
        "timestamp": start.isoformat(),
        "interval_end": end.isoformat(),
        "value": minute if value is None else value,
    }


def _chunk_rows(chunks: list[list[dict[str, object]]]) -> list[dict[str, object]]:
    return [row for chunk in chunks for row in chunk]


def test_window_index_scans_only_relevant_chunks_for_large_dataset(tmp_path):
    store = Workbench(tmp_path).partitioned
    session = Session(user_id="owner", id="session")
    row_count = 10_000
    reference = store.ingest(
        session,
        _metadata(),
        (_row_at(index) for index in range(row_count)),
    )
    start = _BASE + timedelta(minutes=5_555)
    end = start + timedelta(minutes=10)

    chunks = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=start,
            end=end,
        )
    )
    decoded_rows = _chunk_rows(chunks)
    selected = [
        row for row in decoded_rows if start <= datetime.fromisoformat(str(row["timestamp"])) < end
    ]

    assert len(selected) == 10
    assert len(chunks) <= 3
    assert len(decoded_rows) < row_count


def test_window_index_preserves_owner_and_session_scope(tmp_path):
    store = Workbench(tmp_path).partitioned
    owner = Session(user_id="owner", id="session-a")
    artifact_id = store.ingest(owner, _metadata(), [_row_at(0)])["artifact_id"]

    for wrong_scope in (
        Session(user_id="other", id="session-a"),
        Session(user_id="owner", id="session-b"),
    ):
        with pytest.raises(EnergyError, match="does not exist"):
            list(
                store.iter_window_chunks(
                    wrong_scope,
                    artifact_id,
                    start=_BASE,
                    end=_BASE + timedelta(minutes=1),
                )
            )


def test_window_index_keeps_unsorted_chunk_whose_interval_crosses_start(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=3)
    session = Session(user_id="owner", id="session")
    crossing = {
        "timestamp": (_BASE + timedelta(minutes=0)).isoformat(),
        "interval_end": (_BASE + timedelta(minutes=12)).isoformat(),
        "value": "crosses-window-start",
    }
    rows = [
        {**_row_at(10), "value": "later-first"},
        crossing,
        {**_row_at(5), "value": "middle"},
        {**_row_at(30), "value": "next-chunk-a"},
        {**_row_at(31), "value": "next-chunk-b"},
        {**_row_at(32), "value": "next-chunk-c"},
    ]
    reference = store.ingest(session, _metadata(), rows)

    chunks = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=_BASE + timedelta(minutes=11),
            end=_BASE + timedelta(minutes=11, seconds=30),
        )
    )

    assert len(chunks) == 1
    assert [row["value"] for row in chunks[0]] == [
        "later-first",
        "crosses-window-start",
        "middle",
    ]


def test_explicit_different_end_column_falls_back_to_all_chunks(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=1)
    session = Session(user_id="owner", id="session")
    rows = []
    for minute in range(0, 6, 2):
        row = _row_at(minute, end_minutes=1)
        row["custom_finish"] = (_BASE + timedelta(minutes=minute + 5)).isoformat()
        rows.append(row)
    reference = store.ingest(session, _metadata(), rows)

    chunks = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=_BASE + timedelta(minutes=2),
            end=_BASE + timedelta(minutes=4),
            end_column="custom_finish",
        )
    )

    assert len(chunks) == 3
    assert [row["value"] for row in _chunk_rows(chunks)] == [0, 2, 4]


def test_timestamp_provenance_controls_index_matching_and_fallback(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2)
    session = Session(user_id="owner", id="session")
    metadata = _metadata(provenance=[{"provider": "fixture", "timestamp_column": "observed_at"}])
    rows = []
    for minute in range(10):
        row = _row_at(minute)
        row["observed_at"] = row.pop("timestamp")
        rows.append(row)
    reference = store.ingest(session, metadata, rows)
    start = _BASE + timedelta(minutes=4)
    end = start + timedelta(minutes=1)

    matching = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=start,
            end=end,
            timestamp="observed_at",
        )
    )
    different = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=start,
            end=end,
            timestamp="timestamp",
        )
    )

    assert len(matching) == 1
    assert len(different) == 5
    assert len(_chunk_rows(different)) == len(rows)


def test_malformed_timestamp_chunk_is_always_a_candidate(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2)
    session = Session(user_id="owner", id="session")
    rows = [
        _row_at(0),
        _row_at(1),
        {"timestamp": "not-a-time", "interval_end": _row_at(2)["interval_end"], "value": "bad"},
        _row_at(3),
        _row_at(4),
        _row_at(5),
    ]
    reference = store.ingest(session, _metadata(), rows)

    chunks = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=datetime(2030, 1, 1, tzinfo=UTC),
            end=datetime(2030, 1, 2, tzinfo=UTC),
        )
    )

    assert len(chunks) == 1
    assert any(row["value"] == "bad" for row in chunks[0])


def test_positive_resolution_indexes_rows_without_explicit_ends(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2)
    session = Session(user_id="owner", id="session")
    rows = [
        {"timestamp": (_BASE + timedelta(hours=hour)).isoformat(), "value": hour}
        for hour in range(6)
    ]
    reference = store.ingest(session, _metadata(resolution="1h"), rows)

    chunks = list(
        store.iter_window_chunks(
            session,
            reference["artifact_id"],
            start=_BASE + timedelta(hours=3),
            end=_BASE + timedelta(hours=4),
        )
    )

    assert len(chunks) == 1
    assert [row["value"] for row in chunks[0]] == [2, 3]


def test_legacy_chunks_migrate_to_safe_fallback_and_survive_backup_restore(tmp_path):
    state = tmp_path / "legacy-state"
    state.mkdir()
    database_path = state / "artifacts.sqlite3"
    session = Session(user_id="legacy-owner", id="legacy-session")
    artifact_id = "legacy-dataset"
    rows = [_row_at(index, value=index) for index in range(4)]
    payload = json.dumps(rows, separators=(",", ":"))
    metadata = _metadata().model_dump_json()

    with sqlite3.connect(database_path) as db:
        db.execute(
            "CREATE TABLE artifacts ("
            "id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, payload TEXT, "
            "created_at REAL NOT NULL DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE datasets ("
            "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "metadata TEXT NOT NULL, total_rows INTEGER NOT NULL, bytes INTEGER NOT NULL, "
            "created_at REAL NOT NULL DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE chunks ("
            "dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE, "
            "chunk_index INTEGER NOT NULL, first_row INTEGER NOT NULL, "
            "row_count INTEGER NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL, "
            "PRIMARY KEY(dataset_id,chunk_index))"
        )
        db.execute(
            "INSERT INTO datasets(id,user_id,session_id,metadata,total_rows,bytes,created_at) "
            "VALUES (?,?,?,?,?,?,strftime('%s','now'))",
            (artifact_id, session.user_id, session.id, metadata, len(rows), len(payload)),
        )
        db.execute(
            "INSERT INTO chunks(dataset_id,chunk_index,first_row,row_count,payload,bytes) "
            "VALUES (?,?,?,?,?,?)",
            (artifact_id, 0, 0, len(rows), payload, len(payload)),
        )

    store = Workbench(state).partitioned
    with store.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
        assert {"min_start_us", "max_end_us", "indexed_end_column"} <= columns
        assert db.execute(
            "SELECT min_start_us,max_end_us,indexed_end_column FROM chunks WHERE dataset_id=?",
            (artifact_id,),
        ).fetchone() == (None, None, None)

    old_candidates = list(
        store.iter_window_chunks(
            session,
            artifact_id,
            start=datetime(2030, 1, 1, tzinfo=UTC),
            end=datetime(2030, 1, 2, tzinfo=UTC),
        )
    )
    assert _chunk_rows(old_candidates) == rows

    archive = tmp_path / "legacy-backup.tar.gz"
    create_backup(state, archive)
    restored_path = tmp_path / "restored-state"
    restore_backup(archive, restored_path)
    restored = Workbench(restored_path).partitioned

    assert _chunk_rows(list(restored.iter_chunks(session, artifact_id))) == rows
    assert restored.read_page(session, artifact_id, offset=0, limit=10)["total_rows"] == len(rows)
