"""Qualify concurrent scoped dataset access and SQLite backup recovery."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult, Session
from energy_agent_tools.partitioned_timeseries import PartitionedTimeseriesStore, storage_usage
from energy_agent_tools.workbench import Workbench

_WRITER_COUNT = 4
_READER_ROWS = 2_000
_THREAD_COUNT = _WRITER_COUNT + 1


def _metadata(dataset_key: str) -> EnergyResult:
    return EnergyResult(
        data=[],
        kind=DataKind.METERED,
        unit="kWh",
        source="synthetic-concurrency-qualification",
        timezone="UTC",
        resolution="1min",
        site_id=f"site-{dataset_key}",
        asset_id=f"meter-{dataset_key}",
        quantity_shape="interval",
        field_units={"value": "kWh"},
        provenance=[{"provider": "generated-fixture", "dataset_key": dataset_key}],
    )


def _timestamp(start: datetime, index: int) -> str:
    return (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")


def _rows(
    dataset_key: str,
    count: int,
    start: datetime,
    *,
    writer_paused: threading.Event | None = None,
    reader_finished: threading.Event | None = None,
) -> Iterator[dict[str, Any]]:
    for index in range(count):
        if index == count // 2 and writer_paused is not None and reader_finished is not None:
            writer_paused.set()
            if not reader_finished.wait(timeout=30):
                raise TimeoutError("Reader did not finish while the writer held its transaction.")
        yield {
            "dataset_key": dataset_key,
            "index": index,
            "timestamp": _timestamp(start, index),
            "value": index % 13,
        }


def _expected_sum(count: int) -> int:
    cycles, remainder = divmod(count, 13)
    return cycles * 78 + remainder * (remainder - 1) // 2


def _session_scope(session: Session) -> dict[str, str]:
    return {"user_id": session.user_id, "session_id": session.id}


def _assert_inaccessible(
    store: PartitionedTimeseriesStore, session: Session, artifact_id: str
) -> None:
    try:
        store.read_page(session, artifact_id, limit=1)
    except EnergyError as exc:
        assert exc.code == "artifact_not_found", exc.code
    else:
        raise AssertionError(f"Dataset was visible outside its scope: {_session_scope(session)}")


def _summarize_and_window(
    store: PartitionedTimeseriesStore,
    session: Session,
    artifact_id: str,
    start: datetime,
) -> dict[str, int]:
    page = store.read_page(session, artifact_id, offset=23, limit=77)
    assert page["total_rows"] == _READER_ROWS
    assert [row["index"] for row in page["rows"]] == list(range(23, 100))

    count = total = 0
    window_count = window_total = 0
    window_start = start + timedelta(minutes=500)
    window_end = start + timedelta(minutes=1_000)
    for chunk in store.iter_chunks(session, artifact_id):
        for row in chunk:
            count += 1
            total += row["value"]
            instant = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if window_start <= instant < window_end:
                window_count += 1
                window_total += row["value"]

    assert count == _READER_ROWS
    assert total == _expected_sum(_READER_ROWS)
    assert window_count == 500
    assert window_total == _expected_sum(1_000) - _expected_sum(500)
    return {
        "page_rows": len(page["rows"]),
        "streamed_summary_count": count,
        "streamed_summary_sum": total,
        "streamed_window_count": window_count,
        "streamed_window_sum": window_total,
    }


def _verify_dataset(
    store: PartitionedTimeseriesStore,
    session: Session,
    artifact_id: str,
    dataset_key: str,
    expected_rows: int,
) -> None:
    count = total = 0
    for chunk in store.iter_chunks(session, artifact_id):
        for row in chunk:
            assert row["dataset_key"] == dataset_key
            assert row["index"] == count
            assert row["value"] == count % 13
            total += row["value"]
            count += 1
    assert count == expected_rows
    assert total == _expected_sum(expected_rows)


def qualify(rows_per_dataset: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="energy-dataset-concurrency-") as folder:
        root = Path(folder)
        workbench = Workbench(
            root,
            user_quota_bytes=100_000_000,
            global_quota_bytes=500_000_000,
        )
        database_path = workbench.path
        main_store = workbench.partitioned

        reader_session = Session(user_id="reader-user", id="reader-session")
        reader_start = datetime(2024, 1, 1, tzinfo=UTC)
        reader_ref = main_store.ingest(
            reader_session,
            _metadata("reader-baseline"),
            _rows("reader-baseline", _READER_ROWS, reader_start),
        )

        writer_sessions = [
            Session(user_id="shared-user", id="writer-session-0"),
            Session(user_id="shared-user", id="writer-session-1"),
            Session(user_id="writer-user-2", id="writer-session-2"),
            Session(user_id="writer-user-3", id="writer-session-3"),
        ]
        writer_stores = [
            PartitionedTimeseriesStore(
                root,
                chunk_rows=500,
                database_path=database_path,
                user_quota_bytes=100_000_000,
                global_quota_bytes=500_000_000,
            )
            for _ in writer_sessions
        ]
        reader_store = PartitionedTimeseriesStore(
            root,
            chunk_rows=500,
            database_path=database_path,
            user_quota_bytes=100_000_000,
            global_quota_bytes=500_000_000,
        )

        start_barrier = threading.Barrier(_THREAD_COUNT)
        writer_paused = threading.Event()
        reader_finished = threading.Event()
        writer_starts = [
            datetime(2024, 2, 1, tzinfo=UTC) + timedelta(days=index)
            for index in range(_WRITER_COUNT)
        ]

        def import_dataset(index: int) -> dict[str, Any]:
            start_barrier.wait(timeout=15)
            started = time.perf_counter()
            reference = writer_stores[index].ingest(
                writer_sessions[index],
                _metadata(f"writer-{index}"),
                _rows(
                    f"writer-{index}",
                    rows_per_dataset,
                    writer_starts[index],
                    writer_paused=writer_paused,
                    reader_finished=reader_finished,
                ),
            )
            return {
                "index": index,
                "artifact_id": reference["artifact_id"],
                "rows": reference["rows"],
                "seconds": time.perf_counter() - started,
            }

        def read_while_writer_is_paused() -> dict[str, Any]:
            start_barrier.wait(timeout=15)
            if not writer_paused.wait(timeout=30):
                reader_finished.set()
                raise TimeoutError("No writer reached its in-transaction pause point.")
            started = time.perf_counter()
            try:
                result = _summarize_and_window(
                    reader_store,
                    reader_session,
                    reader_ref["artifact_id"],
                    reader_start,
                )
                result["seconds"] = time.perf_counter() - started
                result["overlapped_uncommitted_writer"] = 1
                return result
            finally:
                reader_finished.set()

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=_THREAD_COUNT) as executor:
            reader_future = executor.submit(read_while_writer_is_paused)
            writer_futures = [
                executor.submit(import_dataset, index) for index in range(_WRITER_COUNT)
            ]
            concurrent_reads = reader_future.result(timeout=90)
            writer_results = [future.result(timeout=90) for future in writer_futures]
        concurrent_seconds = time.perf_counter() - started

        assert writer_paused.is_set()
        assert concurrent_reads["overlapped_uncommitted_writer"] == 1
        assert sum(item["rows"] for item in writer_results) == _WRITER_COUNT * rows_per_dataset

        datasets: list[tuple[Session, str, str, int]] = [
            (reader_session, reader_ref["artifact_id"], "reader-baseline", _READER_ROWS)
        ]
        for item in writer_results:
            index = item["index"]
            assert item["rows"] == rows_per_dataset
            datasets.append(
                (
                    writer_sessions[index],
                    item["artifact_id"],
                    f"writer-{index}",
                    rows_per_dataset,
                )
            )

        for owner, artifact_id, dataset_key, expected_rows in datasets:
            _verify_dataset(main_store, owner, artifact_id, dataset_key, expected_rows)

        outsider_sessions = [reader_session, *writer_sessions]
        scope_checks = 0
        for owner, artifact_id, _dataset_key, _row_count in datasets:
            for outsider in outsider_sessions:
                if _session_scope(outsider) == _session_scope(owner):
                    continue
                _assert_inaccessible(main_store, outsider, artifact_id)
                scope_checks += 1
            _assert_inaccessible(
                main_store,
                Session(user_id=owner.user_id, id=f"wrong-{owner.id}"),
                artifact_id,
            )
            scope_checks += 1

        with sqlite3.connect(database_path) as database:
            shared_user_bytes, total_bytes = storage_usage(database, "shared-user")
            dataset_count = database.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        assert dataset_count == len(datasets)
        assert shared_user_bytes > 0 and total_bytes >= shared_user_bytes

        shared_user_quota_store = PartitionedTimeseriesStore(
            root,
            database_path=database_path,
            user_quota_bytes=shared_user_bytes,
            global_quota_bytes=500_000_000,
        )
        try:
            shared_user_quota_store.ingest(
                Session(user_id="shared-user", id="quota-probe-session"),
                _metadata("user-quota-probe"),
                [{"value": 1}],
            )
        except EnergyError as exc:
            assert exc.code == "artifact_quota_exceeded", exc.code
            user_quota_result = exc.code
        else:
            raise AssertionError("Per-user quota did not include data from another store/session.")

        shared_global_quota_store = PartitionedTimeseriesStore(
            root,
            database_path=database_path,
            user_quota_bytes=500_000_000,
            global_quota_bytes=total_bytes,
        )
        try:
            shared_global_quota_store.ingest(
                Session(user_id="global-probe-user", id="global-probe-session"),
                _metadata("global-quota-probe"),
                [{"value": 1}],
            )
        except EnergyError as exc:
            assert exc.code == "artifact_quota_exceeded", exc.code
            global_quota_result = exc.code
        else:
            raise AssertionError("Global quota did not include data from another store/session.")

        backup_path = root / "qualification-backup.tar.gz"
        restore_root = root / "restored-state"
        backup_started = time.perf_counter()
        manifest = create_backup(root, backup_path)
        restore_backup(backup_path, restore_root)
        backup_restore_seconds = time.perf_counter() - backup_started
        assert sum(entry.path == "artifacts.sqlite3" for entry in manifest.files) == 1

        restored_workbench = Workbench(restore_root)
        restored_store = restored_workbench.partitioned
        with sqlite3.connect(restored_workbench.path) as restored_database:
            restored_dataset_count = restored_database.execute(
                "SELECT COUNT(*) FROM datasets"
            ).fetchone()[0]
        assert restored_dataset_count == len(datasets)
        restored_rows = 0
        for owner, artifact_id, dataset_key, expected_rows in datasets:
            last = restored_store.read_page(owner, artifact_id, offset=expected_rows - 1, limit=1)
            assert last["total_rows"] == expected_rows
            assert len(last["rows"]) == 1
            assert last["rows"][0]["dataset_key"] == dataset_key
            assert last["rows"][0]["index"] == expected_rows - 1
            assert last["rows"][0]["value"] == (expected_rows - 1) % 13
            _verify_dataset(restored_store, owner, artifact_id, dataset_key, expected_rows)
            restored_rows += last["total_rows"]
        assert restored_rows == _READER_ROWS + _WRITER_COUNT * rows_per_dataset

        return {
            "status": "passed",
            "scenario": "same-process, separate SQLite store instances with four concurrent imports",
            "python": {
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
            },
            "thread_count": _THREAD_COUNT,
            "inputs": {
                "concurrent_writer_datasets": _WRITER_COUNT,
                "rows_per_concurrent_dataset": rows_per_dataset,
                "concurrent_writer_rows": _WRITER_COUNT * rows_per_dataset,
                "preloaded_reader_rows": _READER_ROWS,
                "total_dataset_count": len(datasets),
                "total_rows": restored_rows,
                "concurrent_store_instances": _WRITER_COUNT + 1,
            },
            "assertions": {
                "all_writer_rows_committed": True,
                "every_stored_and_restored_row_and_closed_form_total_verified": True,
                "page_and_stream_window_read_during_uncommitted_write": True,
                "foreign_user_and_session_reads_rejected": True,
                "scope_denial_checks": scope_checks,
                "user_quota_shared_across_store_instances_and_sessions": user_quota_result,
                "global_quota_shared_across_store_instances": global_quota_result,
                "single_sqlite_backup_restored_all_datasets": True,
                "restored_rows_verified": restored_rows,
            },
            "concurrent_reader": concurrent_reads,
            "timings_seconds": {
                "concurrent_scenario_wall": round(concurrent_seconds, 6),
                "writer_imports": {
                    f"writer_{item['index']}": round(item["seconds"], 6)
                    for item in sorted(writer_results, key=lambda value: value["index"])
                },
                "backup_and_restore": round(backup_restore_seconds, 6),
            },
            "limitations": [
                "Rows are generated fixtures; no physical meter or live provider was exercised.",
                "Concurrency used five threads in one Python process on one local SQLite file.",
                "The run pauses one active transaction to prove read/write overlap; it is not a multi-host qualification or 30-day soak.",
                "The window total is computed from streamed stored rows; this does not qualify a production gateway or external connector path.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rows-per-dataset",
        type=int,
        default=25_000,
        help="Rows per concurrent writer dataset (default: 25000; allowed: 1000..100000).",
    )
    args = parser.parse_args()
    if not 1_000 <= args.rows_per_dataset <= 100_000:
        parser.error("--rows-per-dataset must be between 1000 and 100000")
    try:
        result = qualify(args.rows_per_dataset)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "scenario": "same-process, separate SQLite store instances with four concurrent imports",
                    "python": platform.python_version(),
                    "thread_count": _THREAD_COUNT,
                    "rows_per_dataset": args.rows_per_dataset,
                    "failure_type": type(exc).__name__,
                    "failure": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
