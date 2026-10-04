"""Private, bounded persistence for allowlisted execution activity metadata."""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from pydantic import TypeAdapter

from .activity import (
    ExecutionEntry,
    ExecutionLogPage,
    ExecutionLogRecord,
    ExecutionLogScope,
    ExecutionOutcome,
)

__all__ = ["ExecutionLogError", "ExecutionLogStore"]

_SCHEMA_VERSION = 1
_MAX_PER_SCOPE_LIMIT = 2000
_MAX_TOTAL_LIMIT = 10000
_MAX_PAGE_SIZE = 100
_MAX_SQLITE_INTEGER = 2**63 - 1
_OUTCOME_ADAPTER: TypeAdapter[ExecutionOutcome] = TypeAdapter(ExecutionOutcome)

_INVALID_REQUEST = "Execution log request is invalid."
_STORAGE_UNAVAILABLE = "Execution log storage is unavailable."
_CORRUPT_RECORD = "Execution log contains invalid data."
_CLOSED_STORE = "Execution log store is closed."
_EXECUTION_ID_CONFLICT = "Execution ID is already in use."


class ExecutionLogError(RuntimeError):
    """A fixed-message failure from execution activity storage."""


def _raise_invalid() -> NoReturn:
    raise ExecutionLogError(_INVALID_REQUEST)


def _raise_unavailable() -> NoReturn:
    raise ExecutionLogError(_STORAGE_UNAVAILABLE)


def _raise_corrupt() -> NoReturn:
    raise ExecutionLogError(_CORRUPT_RECORD)


def _validate_limits(per_scope_limit: int, total_limit: int) -> tuple[int, int]:
    if (
        type(per_scope_limit) is not int
        or not 1 <= per_scope_limit <= _MAX_PER_SCOPE_LIMIT
        or type(total_limit) is not int
        or not 1 <= total_limit <= _MAX_TOTAL_LIMIT
    ):
        _raise_invalid()
    return per_scope_limit, total_limit


def _entry(value: ExecutionEntry) -> ExecutionEntry:
    if type(value) is not ExecutionEntry:
        _raise_invalid()
    try:
        parsed = ExecutionEntry.model_validate_json(value.model_dump_json())
        return ExecutionEntry.model_validate(
            {
                **parsed.model_dump(mode="python"),
                "recorded_at": parsed.recorded_at.astimezone(UTC),
            }
        )
    except Exception:
        _raise_invalid()


def _scope(value: ExecutionLogScope) -> ExecutionLogScope:
    if type(value) is not ExecutionLogScope:
        _raise_invalid()
    try:
        return ExecutionLogScope.model_validate_json(value.model_dump_json())
    except Exception:
        _raise_invalid()


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class ExecutionLogStore:
    """Durable metadata log with actor-scoped and global retention bounds."""

    def __init__(
        self,
        root: Path,
        *,
        per_scope_limit: int = _MAX_PER_SCOPE_LIMIT,
        total_limit: int = _MAX_TOTAL_LIMIT,
    ) -> None:
        self.per_scope_limit, self.total_limit = _validate_limits(per_scope_limit, total_limit)
        self.retention_limit = min(self.per_scope_limit, self.total_limit)
        self.root = Path(root)
        self.path = self.root / "activity.sqlite3"
        self._closed = False
        try:
            if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
                _raise_unavailable()
            if self.path.exists() or self.path.is_symlink():
                if not stat.S_ISREG(self.path.lstat().st_mode):
                    _raise_unavailable()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._chmod(self.root, 0o700)
            self._initialize()
            self._chmod(self.path, 0o600)
        except ExecutionLogError:
            raise
        except (OSError, sqlite3.Error):
            _raise_unavailable()

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            # Follow ControlStore on platforms that do not support POSIX modes.
            pass

    def _initialize(self) -> None:
        database = sqlite3.connect(self.path, timeout=0.1, isolation_level=None)
        try:
            database.execute("PRAGMA busy_timeout = 100")
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION or version not in (0, _SCHEMA_VERSION):
                _raise_unavailable()
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            database.execute("PRAGMA secure_delete = ON")
            database.execute("BEGIN IMMEDIATE")
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION or version not in (0, _SCHEMA_VERSION):
                _raise_unavailable()
            if version == 0:
                database.execute(
                    """CREATE TABLE execution_activity (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        execution_id TEXT NOT NULL UNIQUE
                            CHECK(length(execution_id) BETWEEN 1 AND 256),
                        user_id TEXT NOT NULL CHECK(length(user_id) BETWEEN 1 AND 256),
                        workspace_id TEXT CHECK(
                            workspace_id IS NULL OR length(workspace_id) BETWEEN 1 AND 256
                        ),
                        access_mode TEXT NOT NULL CHECK(access_mode IN ('local', 'hosted')),
                        key_id TEXT CHECK(key_id IS NULL OR length(key_id) BETWEEN 1 AND 256),
                        session_id TEXT NOT NULL CHECK(length(session_id) BETWEEN 1 AND 256),
                        site_id TEXT CHECK(site_id IS NULL OR length(site_id) BETWEEN 1 AND 256),
                        account_id TEXT CHECK(
                            account_id IS NULL OR length(account_id) BETWEEN 1 AND 256
                        ),
                        recorded_at TEXT NOT NULL,
                        tool TEXT NOT NULL CHECK(length(tool) BETWEEN 1 AND 256),
                        duration_ms REAL NOT NULL CHECK(duration_ms >= 0),
                        outcome_json TEXT NOT NULL
                    )"""
                )
                database.execute(
                    """CREATE INDEX execution_activity_scope
                       ON execution_activity(user_id, workspace_id, access_mode, sequence DESC)"""
                )
                database.execute(
                    "CREATE INDEX execution_activity_sequence ON execution_activity(sequence DESC)"
                )
                database.execute("PRAGMA user_version = 1")
            database.commit()
        except BaseException:
            if database.in_transaction:
                database.rollback()
            raise
        finally:
            database.close()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise ExecutionLogError(_CLOSED_STORE)
        if self.root.is_symlink() or not stat.S_ISREG(self.path.lstat().st_mode):
            _raise_unavailable()
        database = sqlite3.connect(self.path, timeout=0.1, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA busy_timeout = 100")
            database.execute("PRAGMA secure_delete = ON")
            if write:
                database.execute("BEGIN IMMEDIATE")
            yield database
            if write:
                database.commit()
        except BaseException:
            if database.in_transaction:
                database.rollback()
            raise
        finally:
            database.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> ExecutionLogRecord:
        try:
            outcome = _OUTCOME_ADAPTER.validate_json(row["outcome_json"])
            recorded_at = datetime.fromisoformat(row["recorded_at"])
            if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
                _raise_corrupt()
            entry = ExecutionEntry.model_validate(
                {
                    "execution_id": row["execution_id"],
                    "user_id": row["user_id"],
                    "workspace_id": row["workspace_id"],
                    "key_id": row["key_id"],
                    "session_id": row["session_id"],
                    "site_id": row["site_id"],
                    "account_id": row["account_id"],
                    "access_mode": row["access_mode"],
                    "recorded_at": recorded_at.astimezone(UTC),
                    "tool": row["tool"],
                    "duration_ms": row["duration_ms"],
                    "outcome": outcome,
                }
            )
            return ExecutionLogRecord.model_validate(
                {**entry.model_dump(mode="python"), "sequence": row["sequence"]}
            )
        except Exception:
            _raise_corrupt()

    @staticmethod
    def _is_visible(row: sqlite3.Row, scope: ExecutionLogScope) -> bool:
        if row["site_id"] not in scope.site_ids:
            return False
        account_id = row["account_id"]
        return (
            scope.connection_ids is None or account_id is None or account_id in scope.connection_ids
        )

    def _enforce_retention(self, database: sqlite3.Connection, entry: ExecutionEntry) -> None:
        database.execute(
            """DELETE FROM execution_activity
               WHERE user_id = ? AND workspace_id IS ? AND access_mode = ?
               AND sequence NOT IN (
                   SELECT sequence FROM execution_activity
                   WHERE user_id = ? AND workspace_id IS ? AND access_mode = ?
                   ORDER BY sequence DESC LIMIT ?
               )""",
            (
                entry.user_id,
                entry.workspace_id,
                entry.access_mode,
                entry.user_id,
                entry.workspace_id,
                entry.access_mode,
                self.per_scope_limit,
            ),
        )
        database.execute(
            """DELETE FROM execution_activity
               WHERE sequence NOT IN (
                   SELECT sequence FROM execution_activity
                   ORDER BY sequence DESC LIMIT ?
               )""",
            (self.total_limit,),
        )

    def append(self, entry: ExecutionEntry) -> ExecutionLogRecord:
        validated = _entry(entry)
        outcome_json = json.dumps(
            validated.outcome.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        try:
            with self._connection(write=True) as database:
                existing = database.execute(
                    "SELECT * FROM execution_activity WHERE execution_id = ?",
                    (validated.execution_id,),
                ).fetchone()
                if existing is not None:
                    same_namespace = (
                        existing["user_id"] == validated.user_id
                        and existing["workspace_id"] == validated.workspace_id
                        and existing["access_mode"] == validated.access_mode
                    )
                    if not same_namespace:
                        raise ExecutionLogError(_EXECUTION_ID_CONFLICT)
                    return self._record(existing)

                cursor = database.execute(
                    """INSERT INTO execution_activity(
                        execution_id, user_id, workspace_id, access_mode, key_id, session_id,
                        site_id, account_id, recorded_at, tool, duration_ms, outcome_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        validated.execution_id,
                        validated.user_id,
                        validated.workspace_id,
                        validated.access_mode,
                        validated.key_id,
                        validated.session_id,
                        validated.site_id,
                        validated.account_id,
                        _timestamp(validated.recorded_at),
                        validated.tool,
                        validated.duration_ms,
                        outcome_json,
                    ),
                )
                self._enforce_retention(database, validated)
                row = database.execute(
                    "SELECT * FROM execution_activity WHERE sequence = ?", (cursor.lastrowid,)
                ).fetchone()
                if row is None:
                    _raise_unavailable()
                return self._record(row)
        except ExecutionLogError:
            raise
        except (sqlite3.Error, OSError, OverflowError):
            _raise_unavailable()

    def read(
        self,
        scope: ExecutionLogScope,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> ExecutionLogPage:
        validated_scope = _scope(scope)
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
            _raise_invalid()
        if before is not None and (type(before) is not int or before <= 0):
            _raise_invalid()

        clauses = ["user_id = ?", "workspace_id IS ?", "access_mode = ?"]
        parameters: list[str | int | None] = [
            validated_scope.user_id,
            validated_scope.workspace_id,
            validated_scope.access_mode,
        ]
        if before is not None and before <= _MAX_SQLITE_INTEGER:
            clauses.append("sequence < ?")
            parameters.append(before)
        query = (
            "SELECT * FROM execution_activity WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence DESC LIMIT ?"
        )
        parameters.append(self.per_scope_limit)
        visible: list[ExecutionLogRecord] = []
        has_more = False
        try:
            with self._connection() as database:
                database.execute("BEGIN")
                cursor = database.execute(query, parameters)
                while batch := cursor.fetchmany(128):
                    for row in batch:
                        if not self._is_visible(row, validated_scope):
                            continue
                        record = self._record(row)
                        if len(visible) == limit:
                            has_more = True
                            break
                        visible.append(record)
                    if has_more:
                        break
            return ExecutionLogPage(
                entries=visible,
                next_before=visible[-1].sequence if has_more and visible else None,
                retention_limit=self.retention_limit,
                recording_status="ok",
            )
        except ExecutionLogError:
            raise
        except (sqlite3.Error, OSError, OverflowError):
            _raise_unavailable()

    def close(self) -> None:
        self._closed = True
