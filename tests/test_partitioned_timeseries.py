from __future__ import annotations

import sqlite3
import tracemalloc
from collections.abc import Iterator

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult, Session
from energy_agent_tools.partitioned_timeseries import PartitionedTimeseriesStore


def _metadata(**overrides: object) -> EnergyResult:
    values: dict[str, object] = {
        "data": [],
        "kind": DataKind.METERED,
        "unit": "kWh",
        "source": "large-site-meter",
        "site_id": "site-17",
        "asset_id": "meter-4",
        "provenance": [{"provider": "fixture", "source_id": "stream-a"}],
    }
    values.update(overrides)
    return EnergyResult(**values)  # type: ignore[arg-type]


def _stored_counts(store: PartitionedTimeseriesStore) -> tuple[int, int]:
    with sqlite3.connect(store.path) as db:
        datasets = db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return datasets, chunks


def test_million_rows_ingest_and_stream_with_bounded_python_memory(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=1000)
    session = Session(user_id="large-site-user")

    def generated_rows() -> Iterator[dict[str, object]]:
        for index in range(1_000_000):
            yield {"index": index, "energy_kwh": index % 13}

    tracemalloc.start()
    try:
        reference = store.ingest(session, _metadata(), generated_rows())
        row_count = 0
        energy_total = 0
        for chunk in store.iter_chunks(session, reference["artifact_id"]):
            row_count += len(chunk)
            energy_total += sum(int(row["energy_kwh"]) for row in chunk)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert reference["rows"] == row_count == 1_000_000
    assert energy_total == sum(index % 13 for index in range(1_000_000))
    assert peak_bytes < 32 * 1024 * 1024
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT MAX(row_count) FROM chunks").fetchone()[0] <= 1000
        assert db.execute("SELECT MAX(bytes) FROM chunks").fetchone()[0] <= 4 * 1024 * 1024


def test_restart_pages_across_chunks_and_metadata_provenance(tmp_path):
    session = Session(user_id="u", id="session-1")
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=4)
    reference = store.ingest(
        session,
        _metadata(),
        ({"index": index, "value": index * 10} for index in range(11)),
    )

    restarted = PartitionedTimeseriesStore(tmp_path, chunk_rows=4)
    page = restarted.read_page(session, reference["artifact_id"], offset=3, limit=6)
    assert [row["index"] for row in page["rows"]] == list(range(3, 9))
    assert page["total_rows"] == 11
    assert page["next_offset"] == 9
    assert page["metadata"]["site_id"] == "site-17"
    assert page["metadata"]["asset_id"] == "meter-4"
    assert page["metadata"]["kind"] == "metered"
    assert page["metadata"]["unit"] == "kWh"
    assert page["metadata"]["provenance"] == [{"provider": "fixture", "source_id": "stream-a"}]
    final = restarted.read_page(session, reference["artifact_id"], offset=9, limit=5)
    assert [row["index"] for row in final["rows"]] == [9, 10]
    assert final["next_offset"] is None


def test_artifact_is_scoped_by_user_and_session_and_delete_is_scoped(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2)
    owner = Session(user_id="u", id="s1")
    artifact = store.ingest(owner, _metadata(), [{"n": 1}])["artifact_id"]
    wrong_user = Session(user_id="other", id="s1")
    wrong_session = Session(user_id="u", id="s2")

    for wrong_scope in (wrong_user, wrong_session):
        with pytest.raises(EnergyError, match="does not exist"):
            store.read_page(wrong_scope, artifact, offset=0)
        with pytest.raises(EnergyError, match="does not exist"):
            list(store.iter_chunks(wrong_scope, artifact))
        store.delete(wrong_scope, artifact)

    assert store.read_page(owner, artifact, offset=0)["rows"] == [{"n": 1}]
    store.delete(owner, artifact)
    with pytest.raises(EnergyError, match="does not exist"):
        store.read_page(owner, artifact, offset=0)


def test_generator_failure_rolls_back_all_chunks(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2)
    session = Session(user_id="u")

    def broken_rows() -> Iterator[dict[str, int]]:
        yield {"n": 1}
        yield {"n": 2}
        yield {"n": 3}
        raise RuntimeError("provider stream failed")

    with pytest.raises(RuntimeError, match="provider stream failed"):
        store.ingest(session, _metadata(), broken_rows())
    assert _stored_counts(store) == (0, 0)


def test_quota_and_invalid_rows_roll_back_all_chunks(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path, chunk_rows=2, max_bytes=900)
    session = Session(user_id="u")

    with pytest.raises(EnergyError, match="byte quota"):
        store.ingest(
            session, _metadata(), ({"n": index, "value": "x" * 40} for index in range(100))
        )
    assert _stored_counts(store) == (0, 0)

    def invalid_rows() -> Iterator[dict[str, object]]:
        yield {"n": 1}
        yield {"n": 2}
        yield {"n": 3}
        yield {"n": float("nan")}

    with pytest.raises(EnergyError, match="finite JSON"):
        store.ingest(session, _metadata(), invalid_rows())
    assert _stored_counts(store) == (0, 0)


def test_metadata_and_page_arguments_are_strict(tmp_path):
    store = PartitionedTimeseriesStore(tmp_path)
    session = Session(user_id="u")
    consumed = False

    def rows() -> Iterator[dict[str, int]]:
        nonlocal consumed
        consumed = True
        yield {"n": 1}

    with pytest.raises(EnergyError, match="empty list"):
        store.ingest(session, _metadata(data=[{"already": "materialized"}]), rows())
    assert not consumed
    artifact = store.ingest(session, _metadata(), [{"n": 1}])["artifact_id"]
    for offset, limit in ((-1, 1), (0, 0), (0, 10_001), (True, 1), (0, True)):
        with pytest.raises(EnergyError, match="Page"):
            store.read_page(session, artifact, offset=offset, limit=limit)
