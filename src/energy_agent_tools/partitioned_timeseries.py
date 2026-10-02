"""Persistent, chunked storage for large time-series results.

Rows are encoded and committed a chunk at a time. The input iterable is never
collected into a dataset-sized Python object, and reads can stream the stored
chunks back without loading the complete dataset.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import EnergyError, EnergyResult, Json, Session

_MAX_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_PAGE_BYTES = 16 * 1024 * 1024
_MAX_PAGE_LIMIT = 10_000


class PartitionedTimeseriesStore:
    """Store time-series rows in private SQLite chunks scoped to a session."""

    def __init__(
        self,
        root: Path,
        chunk_rows: int = 1000,
        max_bytes: int = 1_000_000_000,
        *,
        database_path: Path | None = None,
        user_quota_bytes: int = 1_000_000_000,
        global_quota_bytes: int = 10_000_000_000,
        retention_seconds: int = 604800,
    ) -> None:
        if type(chunk_rows) is not int or chunk_rows < 1:
            raise ValueError("chunk_rows must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if min(user_quota_bytes, global_quota_bytes, retention_seconds) <= 0:
            raise ValueError("Storage quotas and retention must be positive")
        self.user_quota_bytes = user_quota_bytes
        self.global_quota_bytes = global_quota_bytes
        self.retention_seconds = retention_seconds
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.path = database_path or root / "timeseries.sqlite3"
        self.chunk_rows = chunk_rows
        self.max_bytes = max_bytes
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS datasets ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                "metadata TEXT NOT NULL, total_rows INTEGER NOT NULL, bytes INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS timeseries_owner ON datasets(user_id,session_id)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE, "
                "chunk_index INTEGER NOT NULL, first_row INTEGER NOT NULL, "
                "row_count INTEGER NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL, "
                "PRIMARY KEY(dataset_id,chunk_index))"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(datasets)")}
            if "created_at" not in columns:
                db.execute("ALTER TABLE datasets ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE datasets SET created_at=? WHERE created_at=0", (time.time(),))
            db.execute("CREATE INDEX IF NOT EXISTS dataset_created ON datasets(created_at)")
            db.commit()
        finally:
            db.close()
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def ingest(
        self,
        session: Session,
        metadata: EnergyResult,
        rows: Iterable[dict[str, Any]],
    ) -> Json:
        """Atomically store ``rows`` and return the scoped artifact reference."""
        if not isinstance(metadata, EnergyResult):
            raise EnergyError("invalid_timeseries_metadata", "Metadata must be an EnergyResult.")
        if not isinstance(metadata.data, list) or metadata.data:
            raise EnergyError(
                "invalid_timeseries_metadata",
                "Metadata data must be an empty list; supply rows through the rows iterable.",
            )

        metadata_value = metadata.model_dump(mode="json")
        try:
            _validate_json_value(metadata_value)
            metadata_json = _encode_json(metadata_value)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise EnergyError(
                "invalid_timeseries_metadata", "Metadata must contain finite JSON values."
            ) from exc
        metadata_bytes = len(metadata_json.encode("utf-8"))
        if metadata_bytes > _MAX_CHUNK_BYTES:
            raise EnergyError(
                "timeseries_metadata_too_large",
                f"Metadata exceeds the {_MAX_CHUNK_BYTES}-byte storage bound.",
            )
        if metadata_bytes > self.max_bytes:
            raise EnergyError("timeseries_quota_exceeded", "Dataset exceeds its byte quota.")

        artifact_id = uuid4().hex
        db = self.connect()
        total_rows = 0
        total_bytes = metadata_bytes
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM datasets WHERE created_at<?", (time.time() - self.retention_seconds,)
            )
            if _table_exists(db, "artifacts"):
                db.execute(
                    "DELETE FROM artifacts WHERE created_at<?",
                    (time.time() - self.retention_seconds,),
                )
            user_usage, global_usage = storage_usage(db, session.user_id)
            quota_remaining = min(
                self.user_quota_bytes - user_usage, self.global_quota_bytes - global_usage
            )
            if metadata_bytes > quota_remaining:
                raise EnergyError(
                    "artifact_quota_exceeded",
                    "Delete old artifacts or increase the operator quota.",
                )
            # The row stays invisible to other connections until every chunk commits.
            db.execute(
                "INSERT INTO datasets(id,user_id,session_id,metadata,total_rows,bytes,created_at) "
                "VALUES (?,?,?,?,0,?,?)",
                (
                    artifact_id,
                    session.user_id,
                    session.id,
                    metadata_json,
                    metadata_bytes,
                    time.time(),
                ),
            )

            encoded_rows: list[str] = []
            encoded_chunk_bytes = 2  # opening and closing array brackets
            chunk_index = 0
            for row in rows:
                if not isinstance(row, dict):
                    raise EnergyError(
                        "invalid_timeseries_row", "Each time-series row must be an object."
                    )
                try:
                    _validate_json_value(row)
                    encoded = _encode_json(row)
                except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                    raise EnergyError(
                        "invalid_timeseries_row",
                        "Rows must contain only finite JSON values.",
                    ) from exc
                row_bytes = len(encoded.encode("utf-8"))
                if row_bytes + 2 > _MAX_CHUNK_BYTES:
                    raise EnergyError(
                        "timeseries_chunk_too_large",
                        f"A row exceeds the {_MAX_CHUNK_BYTES}-byte chunk limit.",
                    )

                if encoded_rows and (
                    len(encoded_rows) >= self.chunk_rows
                    or encoded_chunk_bytes + 1 + row_bytes > _MAX_CHUNK_BYTES
                ):
                    total_bytes = self._write_chunk(
                        db,
                        artifact_id,
                        chunk_index,
                        total_rows,
                        encoded_rows,
                        encoded_chunk_bytes,
                        total_bytes,
                        quota_remaining,
                    )
                    total_rows += len(encoded_rows)
                    chunk_index += 1
                    encoded_rows = []
                    encoded_chunk_bytes = 2

                encoded_rows.append(encoded)
                encoded_chunk_bytes += row_bytes + (1 if len(encoded_rows) > 1 else 0)

            if encoded_rows:
                total_bytes = self._write_chunk(
                    db,
                    artifact_id,
                    chunk_index,
                    total_rows,
                    encoded_rows,
                    encoded_chunk_bytes,
                    total_bytes,
                    quota_remaining,
                )
                total_rows += len(encoded_rows)

            db.execute(
                "UPDATE datasets SET total_rows=?,bytes=? WHERE id=?",
                (total_rows, total_bytes, artifact_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        os.chmod(self.path, 0o600)
        return {"artifact_id": artifact_id, "rows": total_rows, "bytes": total_bytes}

    def _write_chunk(
        self,
        db: sqlite3.Connection,
        artifact_id: str,
        chunk_index: int,
        first_row: int,
        encoded_rows: list[str],
        payload_bytes: int,
        current_bytes: int,
        quota_remaining: int,
    ) -> int:
        payload = "[" + ",".join(encoded_rows) + "]"
        # JSON is emitted with ASCII escapes, so character length equals byte length.
        actual_bytes = len(payload.encode("utf-8"))
        if actual_bytes != payload_bytes or actual_bytes > _MAX_CHUNK_BYTES:
            raise EnergyError("invalid_timeseries_chunk", "Encoded chunk exceeds its byte bound.")
        updated_bytes = current_bytes + actual_bytes
        if updated_bytes > self.max_bytes:
            raise EnergyError("timeseries_quota_exceeded", "Dataset exceeds its byte quota.")
        if updated_bytes > quota_remaining:
            raise EnergyError(
                "artifact_quota_exceeded", "Delete old artifacts or increase the operator quota."
            )
        db.execute(
            "INSERT INTO chunks(dataset_id,chunk_index,first_row,row_count,payload,bytes) "
            "VALUES (?,?,?,?,?,?)",
            (artifact_id, chunk_index, first_row, len(encoded_rows), payload, actual_bytes),
        )
        return updated_bytes

    def read_page(
        self,
        session: Session,
        artifact_id: str,
        offset: int = 0,
        limit: int = 1000,
        *,
        byte_limit: int = _MAX_PAGE_BYTES,
        columns: list[str] | None = None,
    ) -> Json:
        """Read at most ``limit`` rows, preserving their insertion order."""
        if type(offset) is not int or offset < 0:
            raise EnergyError("invalid_page", "Page offset must be a nonnegative integer.")
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise EnergyError(
                "invalid_page", f"Page limit must be between 1 and {_MAX_PAGE_LIMIT}."
            )
        if type(byte_limit) is not int or not 1 <= byte_limit <= _MAX_PAGE_BYTES:
            raise EnergyError("invalid_page", "Page byte limit is outside its supported range.")
        if columns is not None and (
            not columns
            or len(columns) > 64
            or any(not isinstance(c, str) or not c for c in columns)
        ):
            raise EnergyError("invalid_page", "Page columns must contain 1 to 64 names.")
        db = self.connect()
        try:
            db.execute("BEGIN")
            record = self._dataset_record(db, session, artifact_id)
            if record is None:
                raise _not_found()
            metadata_json, total_rows = record
            end = min(offset + limit, total_rows)
            page_rows: list[dict[str, Any]] = []
            page_bytes = 2
            page_full = False
            if offset < total_rows:
                cursor = db.execute(
                    "SELECT first_row,row_count,payload FROM chunks "
                    "WHERE dataset_id=? AND first_row<? AND first_row+row_count>? "
                    "ORDER BY chunk_index",
                    (artifact_id, end, offset),
                )
                for first_row, row_count, payload in cursor:
                    chunk_rows = json.loads(payload)
                    start_in_chunk = max(0, offset - first_row)
                    end_in_chunk = min(row_count, end - first_row)
                    for row in chunk_rows[start_in_chunk:end_in_chunk]:
                        if columns is not None:
                            if any(column not in row for column in columns):
                                raise EnergyError(
                                    "column_not_found", "Requested page column is missing."
                                )
                            row = {column: row[column] for column in columns}
                        row_bytes = len(_encode_json(row).encode("utf-8"))
                        additional_bytes = row_bytes + (1 if page_rows else 0)
                        if page_bytes + additional_bytes > byte_limit:
                            if not page_rows:
                                raise EnergyError(
                                    "row_too_large",
                                    "Select fewer columns; one row exceeds the page byte budget.",
                                )
                            page_full = True
                            break
                        page_rows.append(row)
                        page_bytes += additional_bytes
                    if page_full:
                        break
            next_offset = offset + len(page_rows)
            return {
                "artifact_id": artifact_id,
                "metadata": json.loads(metadata_json),
                "rows": page_rows,
                "total_rows": total_rows,
                "next_offset": next_offset if next_offset < total_rows else None,
            }
        finally:
            db.close()

    def describe(self, session: Session, artifact_id: str) -> EnergyResult:
        db = self.connect()
        try:
            record = self._dataset_record(db, session, artifact_id)
            if record is None:
                raise _not_found()
            metadata_json, count = record
            result = EnergyResult.model_validate_json(metadata_json)
            result.data = {
                "storage": "partitioned-timeseries.v1",
                "dataset_id": artifact_id,
                "rows": count,
                "timestamp_column": result.provenance[0].get("timestamp_column", "timestamp")
                if result.provenance
                else "timestamp",
            }
            return result
        finally:
            db.close()

    def iter_chunks(self, session: Session, artifact_id: str) -> Iterator[list[dict[str, Any]]]:
        """Yield stored row batches, with each batch bounded by chunk size and bytes."""
        db = self.connect()
        try:
            db.execute("BEGIN")
            if self._dataset_record(db, session, artifact_id) is None:
                raise _not_found()
            cursor = db.execute(
                "SELECT payload FROM chunks WHERE dataset_id=? ORDER BY chunk_index",
                (artifact_id,),
            )
            for (payload,) in cursor:
                yield json.loads(payload)
        finally:
            db.close()

    def delete(self, session: Session, artifact_id: str) -> None:
        """Delete an artifact only when both the user and session match."""
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM datasets WHERE id=? AND user_id=? AND session_id=?",
                (artifact_id, session.user_id, session.id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _dataset_record(
        self, db: sqlite3.Connection, session: Session, artifact_id: str
    ) -> tuple[str, int] | None:
        return db.execute(
            "SELECT metadata,total_rows FROM datasets WHERE id=? AND user_id=? AND session_id=? AND created_at>=?",
            (artifact_id, session.user_id, session.id, time.time() - self.retention_seconds),
        ).fetchone()


def _not_found() -> EnergyError:
    return EnergyError("artifact_not_found", "Artifact does not exist in this session.")


def _encode_json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=True)


def _validate_json_value(value: Any) -> None:
    """Reject non-JSON Python values and non-finite floats before encoding."""
    active: set[int] = set()
    # This iterative walk accepts ordinary recursive JSON trees while avoiding
    # Python recursion limits and rejecting cycles explicitly.
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        item, exiting = stack.pop()
        if isinstance(item, dict):
            marker = id(item)
            if exiting:
                active.remove(marker)
                continue
            if marker in active:
                raise ValueError("cyclic mapping")
            active.add(marker)
            stack.append((item, True))
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                stack.append((nested, False))
        elif isinstance(item, list):
            marker = id(item)
            if exiting:
                active.remove(marker)
                continue
            if marker in active:
                raise ValueError("cyclic sequence")
            active.add(marker)
            stack.append((item, True))
            stack.extend((nested, False) for nested in item)
        elif item is None or isinstance(item, (str, bool, int)):
            continue
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite number")
        else:
            raise TypeError(f"unsupported JSON value: {type(item).__name__}")


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def storage_usage(db: sqlite3.Connection, user_id: str) -> tuple[int, int]:
    """Count small artifacts and dataset chunks in the same transactional quota ledger."""
    owned, total = 0, 0
    for table, expression in (
        ("artifacts", "length(CAST(payload AS BLOB))"),
        ("datasets", "bytes"),
    ):
        if _table_exists(db, table):
            owned += db.execute(
                f"SELECT COALESCE(SUM({expression}),0) FROM {table} WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            total += db.execute(f"SELECT COALESCE(SUM({expression}),0) FROM {table}").fetchone()[0]
    return owned, total
