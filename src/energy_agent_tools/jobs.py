"""Durable, bounded subprocess jobs for local numerical simulations.

The job service is deliberately narrower than the normal agent gateway.  A job
contains one operation from :class:`SimulationOperation`, and the worker maps
that operation to one reviewed, local numerical tool.  It never accepts a
provider name, plugin, executable, Python module, or filesystem path from the
caller.

This module provides durable coordination.  The worker implementation lives in
``job_worker`` so that a timeout or a crashed numerical dependency cannot take
down the hosting process.  The subprocess boundary is an isolation boundary,
not a security sandbox; see ``docs/simulation-jobs.md``.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .job_contracts import JobListQuery, JobMetadata, JobMetadataPage, JobReadScope

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]


class JobError(Exception):
    """Base exception for a rejected or unavailable simulation job."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class JobAccessDenied(JobError):
    """Raised when a job is accessed outside its user and session scope."""

    def __init__(self) -> None:
        super().__init__("job_access_denied", "The job is outside this user and session scope.")


class JobNotFound(JobError):
    """Raised when a job identifier does not exist."""

    def __init__(self) -> None:
        super().__init__("job_not_found", "The simulation job does not exist.")


class JobQuotaExceeded(JobError):
    """Raised when a job would exceed a configured durable quota."""


class JobNotReady(JobError):
    """Raised when a result is requested before successful completion."""


class SimulationOperation(StrEnum):
    """Numerical operations that may run in the isolated worker."""

    HEAT_LOSS = "heat_loss"
    POWER_FLOW = "power_flow"
    BATTERY = "battery"
    SOLAR = "solar"
    NETWORK_POWER_FLOW = "network_power_flow"
    NETWORK_DISPATCH = "network_dispatch"


_LEGACY_OPERATION_CONSTRAINT = (
    "CHECK (operation IN ('heat_loss', 'power_flow', 'battery', 'solar'))"
)
_OPERATION_CONSTRAINT = (
    "CHECK (operation IN ("
    + ", ".join(f"'{operation.value}'" for operation in SimulationOperation)
    + "))"
)


class JobStatus(StrEnum):
    """Persistent lifecycle states for a job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


_TERMINAL_STATES = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.INTERRUPTED.value,
}
_MAX_SUPPORTED_CONCURRENCY = 2
_INPUT_SCHEMA_VERSION = 1
AccessMode = Literal["local", "hosted"]
_SECRET_KEYS = {
    "authorization",
    "apikey",
    "clientsecret",
    "credential",
    "password",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "privatekey",
}
_EXECUTION_KEYS = {
    "cmd",
    "command",
    "executable",
    "filename",
    "filepath",
    "import",
    "module",
    "path",
    "script",
}


@dataclass(frozen=True, slots=True)
class JobRecord:
    """A scoped, serializable view of one durable job."""

    job_id: str
    user_id: str
    session_id: str
    site_id: str | None
    access_mode: AccessMode
    operation: SimulationOperation
    status: JobStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    input_bytes: int
    output_bytes: int
    error_code: str | None = None
    error_message: str | None = None
    workspace_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a transport-safe status object without private file paths."""

        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "site_id": self.site_id,
            "workspace_id": self.workspace_id,
            "access_mode": self.access_mode,
            "operation": self.operation.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _validate_identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise JobError(
            "invalid_identity", f"{name} must be a non-empty string of at most 256 characters."
        )
    if any(ord(char) < 32 for char in value):
        raise JobError("invalid_identity", f"{name} contains a control character.")
    return value


def _validate_access_mode(value: object) -> AccessMode:
    if type(value) is str:
        if value == "local":
            return "local"
        if value == "hosted":
            return "hosted"
    raise JobError("invalid_access_mode", "Access mode must be local or hosted.")


def _normal_key(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def _check_json_shape(value: Any, *, depth: int = 0, name: str = "arguments") -> None:
    """Reject credential, execution, and path controls before serializing input."""

    if depth > 50:
        raise JobError("invalid_arguments", f"{name} is nested too deeply.")
    if isinstance(value, Mapping):
        if len(value) > 10_000:
            raise JobError("input_too_large", f"{name} contains too many fields.")
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str) or len(raw_key) > 200:
                raise JobError("invalid_arguments", "Argument keys must be short strings.")
            key = _normal_key(raw_key)
            if key in _SECRET_KEYS:
                raise JobError(
                    "credentials_forbidden",
                    "Simulation arguments cannot contain credentials; use a connection outside the job payload.",
                )
            if key in _EXECUTION_KEYS:
                raise JobError(
                    "execution_control_forbidden",
                    "Simulation arguments cannot contain commands, modules, scripts, or filesystem paths.",
                )
            _check_json_shape(nested, depth=depth + 1, name=f"{name}.{raw_key}")
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise JobError("input_too_large", f"{name} contains too many items.")
        for index, nested in enumerate(value):
            _check_json_shape(nested, depth=depth + 1, name=f"{name}[{index}]")
    elif isinstance(value, (str, int, float, bool)) or value is None:
        return
    else:
        raise JobError("invalid_arguments", f"{name} contains a value that is not JSON data.")


def _canonical_payload(operation: SimulationOperation, arguments: Mapping[str, Any]) -> bytes:
    _check_json_shape(arguments)
    try:
        return json.dumps(
            {
                "schema_version": _INPUT_SCHEMA_VERSION,
                "operation": operation.value,
                "arguments": dict(arguments),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JobError(
            "invalid_arguments", "Simulation arguments must be finite JSON data."
        ) from exc


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise JobError(
            "private_storage_unavailable", f"Could not protect job storage: {path.name}"
        ) from exc


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    _chmod_private(path, stat.S_IRUSR | stat.S_IWUSR)


class JobManager:
    """Coordinate bounded numerical jobs backed by a small SQLite state machine.

    ``run_pending`` claims at most two pending jobs at a time, starts fixed
    module workers, waits for them, and continues until the pending queue is
    empty.  The method is safe to call from an async hosting loop; status and
    quota mutations use short SQLite transactions and are scoped by both user
    and session identifiers.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_concurrency: int = 2,
        timeout_seconds: float = 30.0,
        max_jobs_per_user: int = 100,
        max_jobs_global: int = 1_000,
        max_input_bytes: int | None = None,
        max_output_bytes: int | None = None,
        max_input_bytes_per_job: int = 1_000_000,
        max_output_bytes_per_job: int = 4_000_000,
        max_input_bytes_per_user: int = 20_000_000,
        max_output_bytes_per_user: int = 40_000_000,
        max_input_bytes_global: int = 200_000_000,
        max_output_bytes_global: int = 400_000_000,
    ) -> None:
        if not 1 <= max_concurrency <= _MAX_SUPPORTED_CONCURRENCY:
            raise ValueError("max_concurrency must be between 1 and 2")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a finite positive number")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name, value in (
            ("max_jobs_per_user", max_jobs_per_user),
            ("max_jobs_global", max_jobs_global),
            ("max_input_bytes_per_job", max_input_bytes_per_job),
            ("max_output_bytes_per_job", max_output_bytes_per_job),
            ("max_input_bytes_per_user", max_input_bytes_per_user),
            ("max_output_bytes_per_user", max_output_bytes_per_user),
            ("max_input_bytes_global", max_input_bytes_global),
            ("max_output_bytes_global", max_output_bytes_global),
        ):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_input_bytes is not None:
            if not isinstance(max_input_bytes, int) or max_input_bytes <= 0:
                raise ValueError("max_input_bytes must be a positive integer")
            max_input_bytes_per_job = max_input_bytes
        if max_output_bytes is not None:
            if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
                raise ValueError("max_output_bytes must be a positive integer")
            max_output_bytes_per_job = max_output_bytes
        self.root = Path(root).expanduser().resolve()
        # ``root`` is the private job store itself.  Keeping one level avoids
        # surprising callers that pass ``.energy-agent-state/jobs`` and then
        # find records under ``jobs/jobs``.
        self.jobs_dir = self.root
        self.max_concurrency = max_concurrency
        self.timeout_seconds = float(timeout_seconds)
        self.max_jobs_per_user = max_jobs_per_user
        self.max_jobs_global = max_jobs_global
        self.max_input_bytes_per_job = max_input_bytes_per_job
        self.max_output_bytes_per_job = max_output_bytes_per_job
        self.max_input_bytes_per_user = max_input_bytes_per_user
        self.max_output_bytes_per_user = max_output_bytes_per_user
        self.max_input_bytes_global = max_input_bytes_global
        self.max_output_bytes_global = max_output_bytes_global
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._run_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._lock_handle: Any | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.root, stat.S_IRWXU)
        self._acquire_root_lock()
        try:
            self._initialize_storage()
            self._rebase_paths()
            self.recover()
        except BaseException:
            self.close()
            raise

    def _rebase_paths(self) -> None:
        """Keep generated paths inside the current root after a verified restore."""
        with self._connect() as database:
            for row in database.execute("SELECT job_id FROM jobs").fetchall():
                job_id = str(row["job_id"])
                if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                    raise JobError("invalid_job_store", "Stored job identifier is invalid.")
                directory = self.jobs_dir / job_id
                if directory.is_symlink():
                    raise JobError("invalid_job_store", "Job directories cannot be symlinks.")
                database.execute(
                    "UPDATE jobs SET input_path=?, output_path=?, state_dir=? WHERE job_id=?",
                    (
                        str(directory / "input.json"),
                        str(directory / "output.json"),
                        str(directory / "state"),
                        job_id,
                    ),
                )

    @property
    def database_path(self) -> Path:
        return self.root / "jobs.sqlite3"

    def _initialize_storage(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.root, stat.S_IRWXU)
        _chmod_private(self.jobs_dir, stat.S_IRWXU)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2}:
                raise JobError("invalid_job_store", "The job schema version is not supported.")
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    operation TEXT NOT NULL {_OPERATION_CONSTRAINT},
                    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
                    input_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    state_dir TEXT NOT NULL,
                    input_bytes INTEGER NOT NULL,
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    pid INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    access_mode TEXT NOT NULL DEFAULT 'local'
                        CHECK (access_mode IN ('local', 'hosted'))
                );
                CREATE INDEX IF NOT EXISTS jobs_user_session_idx ON jobs (user_id, session_id, created_at);
                CREATE INDEX IF NOT EXISTS jobs_pending_idx ON jobs (status, created_at);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")}
                if "site_id" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN site_id TEXT")
                if "workspace_id" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN workspace_id TEXT")
                if "access_mode" not in columns:
                    connection.execute(
                        "ALTER TABLE jobs ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'local' "
                        "CHECK (access_mode IN ('local', 'hosted'))"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            self._migrate_operations(connection)
            connection.execute("PRAGMA user_version = 2")
        if self.database_path.exists():
            _chmod_private(self.database_path, stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _migrate_operations(connection: sqlite3.Connection) -> None:
        source_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()[0]
        if _OPERATION_CONSTRAINT in source_sql:
            return
        header = r'CREATE TABLE (?:"jobs"|jobs)\s*\('
        if _LEGACY_OPERATION_CONSTRAINT not in source_sql or not re.match(header, source_sql):
            raise JobError(
                "invalid_job_store", "The job operation schema is not a recognized version."
            )
        replacement = re.sub(
            header,
            "CREATE TABLE jobs_next (",
            source_sql.replace(_LEGACY_OPERATION_CONSTRAINT, _OPERATION_CONSTRAINT),
            count=1,
        )
        columns = ", ".join(
            '"' + str(row[1]).replace('"', '""') + '"'
            for row in connection.execute("PRAGMA table_info(jobs)")
        )
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='jobs' AND sql IS NOT NULL"
            )
        ]
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(replacement)
            connection.execute(f"INSERT INTO jobs_next ({columns}) SELECT {columns} FROM jobs")
            connection.execute("DROP TABLE jobs")
            connection.execute("ALTER TABLE jobs_next RENAME TO jobs")
            for index in indexes:
                connection.execute(index)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _acquire_root_lock(self) -> None:
        lock_path = self.root / "manager.lock"
        handle = lock_path.open("a+")
        try:
            _chmod_private(lock_path, stat.S_IRUSR | stat.S_IWUSR)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                handle.write("0")
                handle.flush()
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._lock_handle = handle
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise JobError(
                "manager_locked",
                "Another live JobManager already owns this job root.",
            ) from exc

    def _release_root_lock(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        self._lock_handle = None
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise JobError("manager_closed", "The job manager is closed.")

    def close(self) -> None:
        """Release the root lock after all workers have stopped.

        Running workers require the async :meth:`aclose` path so they can be
        terminated as process groups before another manager takes ownership.
        """

        if self._processes or self._tasks:
            raise JobError(
                "manager_busy",
                "Use await aclose() while simulation workers are running.",
            )
        self._closing = True
        self._closed = True
        self._release_root_lock()

    def __del__(self) -> None:
        # OS locks are released when the descriptor closes after a crash.  This
        # best-effort path also makes normal garbage collection unsurprising.
        try:
            self._release_root_lock()
        except Exception:
            pass

    def _connect(self, *, timeout: float = 30) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        # The service uses short IMMEDIATE transactions, so the rollback
        # journal keeps the durable directory to one mode-0600 database file.
        # WAL/SHM sidecars would otherwise inherit a platform umask that may
        # be less restrictive than the job directory policy.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def recover(self) -> int:
        """Mark jobs left running by a dead manager as interrupted.

        Pending jobs are intentionally preserved for the next ``run_pending``
        call.  A recovery pass never retries a job that was already claimed by
        a previous process because numerical jobs may have partially written a
        result before the process disappeared.
        """

        now = _utcnow()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, error_code = ?,
                    error_message = ?, pid = NULL
                WHERE status = ?
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    now,
                    "manager_restarted",
                    "The hosting process restarted while this job was running.",
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount

    def _row(self, job_id: str, *, timeout: float = 30) -> sqlite3.Row:
        with self._connect(timeout=timeout) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound()
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        try:
            access_mode = _validate_access_mode(row["access_mode"])
        except JobError:
            raise JobAccessDenied() from None
        return JobRecord(
            job_id=str(row["job_id"]),
            user_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            site_id=row["site_id"],
            workspace_id=row["workspace_id"],
            access_mode=access_mode,
            operation=SimulationOperation(str(row["operation"])),
            status=JobStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            input_bytes=int(row["input_bytes"]),
            output_bytes=int(row["output_bytes"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _authorise(row: sqlite3.Row, user_id: str, session_id: str) -> None:
        if row["user_id"] != user_id or row["session_id"] != session_id:
            raise JobAccessDenied()

    def submit(
        self,
        user_id: str,
        session_id: str,
        operation: SimulationOperation | str,
        arguments: Mapping[str, Any],
        *,
        site_id: str | None = None,
        access_mode: AccessMode = "local",
        workspace_id: str | None = None,
    ) -> JobRecord:
        """Persist a validated pending job and return its scoped identifier."""

        self._ensure_open()
        access_mode = _validate_access_mode(access_mode)
        user_id = _validate_identity(user_id, "user_id")
        session_id = _validate_identity(session_id, "session_id")
        if workspace_id is not None:
            workspace_id = _validate_identity(workspace_id, "workspace_id")
        if site_id is not None:
            site_id = _validate_identity(site_id, "site_id")
        try:
            operation = SimulationOperation(operation)
        except (TypeError, ValueError) as exc:
            raise JobError(
                "invalid_operation", "The simulation operation is not allowlisted."
            ) from exc
        if not isinstance(arguments, Mapping):
            raise JobError("invalid_arguments", "Simulation arguments must be a JSON object.")
        payload = _canonical_payload(operation, arguments)
        if len(payload) > self.max_input_bytes_per_job:
            raise JobQuotaExceeded("input_quota", "The simulation input exceeds the per-job quota.")
        job_id = uuid4().hex
        job_dir = self.jobs_dir / job_id
        state_dir = job_dir / "state"
        input_path = job_dir / "input.json"
        output_path = job_dir / "output.json"
        job_dir.mkdir(mode=stat.S_IRWXU, parents=False)
        _chmod_private(job_dir, stat.S_IRWXU)
        state_dir.mkdir(mode=stat.S_IRWXU)
        _chmod_private(state_dir, stat.S_IRWXU)
        _write_private(input_path, payload)
        now = _utcnow()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                user_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                )
                global_count = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                user_input = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(input_bytes), 0) FROM jobs WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                )
                global_input = int(
                    connection.execute("SELECT COALESCE(SUM(input_bytes), 0) FROM jobs").fetchone()[
                        0
                    ]
                )
                if user_count >= self.max_jobs_per_user or global_count >= self.max_jobs_global:
                    raise JobQuotaExceeded("job_quota", "The durable job count quota is exhausted.")
                if user_input + len(payload) > self.max_input_bytes_per_user:
                    raise JobQuotaExceeded(
                        "input_quota", "The user's durable input quota is exhausted."
                    )
                if global_input + len(payload) > self.max_input_bytes_global:
                    raise JobQuotaExceeded(
                        "input_quota", "The global durable input quota is exhausted."
                    )
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, user_id, session_id, site_id, operation, status,
                        input_path, output_path, state_dir, input_bytes, created_at, access_mode, workspace_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        user_id,
                        session_id,
                        site_id,
                        operation.value,
                        JobStatus.PENDING.value,
                        str(input_path),
                        str(output_path),
                        str(state_dir),
                        len(payload),
                        now,
                        access_mode,
                        workspace_id,
                    ),
                )
                connection.commit()
        except Exception:
            input_path.unlink(missing_ok=True)
            try:
                state_dir.rmdir()
                job_dir.rmdir()
            except OSError:
                pass
            raise
        return self._record(self._row(job_id))

    def status(self, job_id: str, user_id: str, session_id: str) -> JobRecord:
        """Return status only when both ownership dimensions match."""

        _validate_identity(user_id, "user_id")
        _validate_identity(session_id, "session_id")
        row = self._row(job_id)
        self._authorise(row, user_id, session_id)
        return self._record(row)

    get = status

    def result(self, job_id: str, user_id: str, session_id: str) -> dict[str, Any]:
        """Read a completed result through the same user and session boundary."""

        record = self.status(job_id, user_id, session_id)
        if record.status is not JobStatus.COMPLETED:
            raise JobNotReady("job_not_ready", f"The job is {record.status.value}.")
        row = self._row(job_id)
        path = Path(str(row["output_path"]))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(
                "result_unavailable", "The completed job result is unavailable."
            ) from exc
        if not isinstance(data, dict):
            raise JobError("result_unavailable", "The completed job result is invalid.")
        return data

    get_result = result

    def resume_scope(
        self,
        job_id: str,
        user_id: str,
        *,
        access_mode: AccessMode = "local",
    ) -> dict[str, str | None]:
        """Recover only the original session/site scope after a host restart.

        This deliberately returns no arguments, result, or private paths.  The
        hosting layer must still check that ``site_id`` is currently permitted
        for ``user_id`` before recreating a session.
        """

        try:
            access_mode = _validate_access_mode(access_mode)
        except JobError:
            raise JobAccessDenied() from None
        _validate_identity(user_id, "user_id")
        row = self._row(job_id)
        try:
            stored_access_mode = _validate_access_mode(row["access_mode"])
        except JobError:
            raise JobAccessDenied() from None
        if row["user_id"] != user_id or stored_access_mode != access_mode:
            raise JobAccessDenied()
        return {
            "job_id": str(row["job_id"]),
            "session_id": str(row["session_id"]),
            "site_id": row["site_id"],
        }

    def _remove_job_files(self, row: sqlite3.Row) -> None:
        job_id = str(row["job_id"])
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            return
        job_dir = self.jobs_dir / job_id
        try:
            if job_dir.resolve().parent != self.jobs_dir.resolve():
                return
        except OSError:
            return
        if job_dir.is_symlink():
            job_dir.unlink(missing_ok=True)
        elif job_dir.exists():
            shutil.rmtree(job_dir)

    def delete(self, job_id: str, user_id: str, session_id: str) -> None:
        """Delete one terminal job and its private files within its scope."""

        self._ensure_open()
        _validate_identity(user_id, "user_id")
        _validate_identity(session_id, "session_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound()
            self._authorise(row, user_id, session_id)
            if row["status"] not in _TERMINAL_STATES:
                raise JobError("job_active", "Cancel the job before deleting it.")
            connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            connection.commit()
        self._remove_job_files(row)

    def cleanup(
        self,
        user_id: str,
        session_id: str,
        *,
        older_than_seconds: float = 0.0,
        limit: int = 100,
    ) -> int:
        """Delete scoped terminal jobs older than a caller-selected age."""

        self._ensure_open()
        _validate_identity(user_id, "user_id")
        _validate_identity(session_id, "session_id")
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, (int, float))
            or not math.isfinite(float(older_than_seconds))
            or older_than_seconds < 0
        ):
            raise JobError(
                "invalid_retention", "older_than_seconds must be finite and non-negative."
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise JobError("invalid_retention", "limit must be between 1 and 1000.")
        cutoff = datetime.now(UTC).timestamp() - float(older_than_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE user_id = ? AND session_id = ? AND status IN (?, ?, ?, ?)
                ORDER BY created_at LIMIT ?
                """,
                (
                    user_id,
                    session_id,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.INTERRUPTED.value,
                    limit,
                ),
            ).fetchall()
        selected: builtins.list[sqlite3.Row] = []
        for row in rows:
            try:
                finished = datetime.fromisoformat(str(row["finished_at"])).timestamp()
            except (TypeError, ValueError):
                continue
            if finished <= cutoff:
                selected.append(row)
        for row in selected:
            self.delete(str(row["job_id"]), user_id, session_id)
        return len(selected)

    prune = cleanup

    def list(self, user_id: str, session_id: str) -> builtins.list[JobRecord]:
        """List only jobs belonging to the exact user and session."""

        _validate_identity(user_id, "user_id")
        _validate_identity(session_id, "session_id")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND session_id = ? ORDER BY created_at",
                (user_id, session_id),
            ).fetchall()
        return [self._record(row) for row in rows]

    def metadata(self, job_id: str, scope: JobReadScope) -> JobMetadata:
        from .job_contracts import JobMetadata

        row = self._row(job_id, timeout=0.1)
        if (
            row["user_id"] != scope.user_id
            or row["workspace_id"] != scope.workspace_id
            or row["access_mode"] != scope.access_mode
            or row["site_id"] not in scope.site_ids
        ):
            raise JobAccessDenied()
        try:
            data = self._record(row).as_dict()
            data.pop("error_message")
            return JobMetadata.model_validate_json(json.dumps(data))
        except (ValueError, KeyError) as exc:
            raise JobError("job_history_unavailable", "Job history is unavailable.") from exc

    def metadata_page(self, scope: JobReadScope, query: JobListQuery) -> JobMetadataPage:
        from pydantic import ValidationError

        from .job_contracts import JobCursor, JobMetadata, JobMetadataPage

        self._ensure_open()
        clauses = ["user_id = ?", "workspace_id IS ?", "access_mode = ?"]
        parameters: list[str | int | None] = [scope.user_id, scope.workspace_id, scope.access_mode]
        if query.status is not None:
            clauses.append("status = ?")
            parameters.append(query.status)
        if query.before is not None:
            try:
                raw = base64.b64decode(
                    query.before + "=" * (-len(query.before) % 4), altchars=b"-_", validate=True
                )
                cursor = JobCursor.model_validate_json(raw)
            except (ValueError, ValidationError) as exc:
                raise JobError("invalid_cursor", "Job history cursor is invalid.") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
            timestamp = cursor.created_at.astimezone(UTC).isoformat()
            parameters.extend([timestamp, timestamp, cursor.job_id])
        sql = (
            "SELECT * FROM jobs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, job_id DESC"
        )
        entries: list[JobMetadata] = []
        more = False
        try:
            with self._connect(timeout=0.1) as connection:
                rows = connection.execute(sql, parameters)
                while batch := rows.fetchmany(128):
                    for row in batch:
                        if row["site_id"] not in scope.site_ids:
                            continue
                        if len(entries) == query.limit:
                            more = True
                            break
                        data = self._record(row).as_dict()
                        data.pop("error_message")
                        entries.append(JobMetadata.model_validate_json(json.dumps(data)))
                    if more:
                        break
        except (sqlite3.Error, ValueError, KeyError, OSError) as exc:
            raise JobError("job_history_unavailable", "Job history is unavailable.") from exc
        next_before = None
        if more:
            last = entries[-1]
            cursor = JobCursor(created_at=last.created_at, job_id=last.job_id)
            next_before = (
                base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")
            )
        return JobMetadataPage(jobs=entries, next_before=next_before)

    def adopt_legacy_hosted_workspace(self, site_id: str, workspace_id: str) -> None:
        """Attach legacy rows only after the host verifies an immutable managed site."""
        self._ensure_open()
        site_id = _validate_identity(site_id, "site_id")
        workspace_id = _validate_identity(workspace_id, "workspace_id")
        try:
            with self._connect(timeout=0.1) as connection:
                connection.execute(
                    """UPDATE jobs SET workspace_id = ?
                   WHERE site_id = ? AND workspace_id IS NULL AND access_mode = 'hosted'""",
                    (workspace_id, site_id),
                )
        except (sqlite3.Error, OSError) as exc:
            raise JobError("job_history_unavailable", "Job history is unavailable.") from exc

    def _claim_pending(self, limit: int) -> builtins.list[sqlite3.Row]:
        if limit <= 0:
            return []
        now = _utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at LIMIT ?",
                (JobStatus.PENDING.value, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET status = ?, started_at = ?, pid = NULL WHERE job_id = ? AND status = ?",
                    (JobStatus.RUNNING.value, now, row["job_id"], JobStatus.PENDING.value),
                )
            connection.commit()
        return [self._row(str(row["job_id"])) for row in rows]

    def _raw_status(self, job_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return str(row[0]) if row else JobStatus.INTERRUPTED.value

    def _set_pid(self, job_id: str, pid: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET pid = ? WHERE job_id = ? AND status = ?",
                (pid, job_id, JobStatus.RUNNING.value),
            )

    def _interrupt_running(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, error_code = ?,
                    error_message = ?, pid = NULL
                WHERE status = ?
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    _utcnow(),
                    "manager_closed",
                    message,
                    JobStatus.RUNNING.value,
                ),
            )

    @staticmethod
    def _safe_environment(state_dir: Path) -> dict[str, str]:
        home = state_dir / "home"
        home.mkdir(mode=stat.S_IRWXU, exist_ok=True)
        _chmod_private(home, stat.S_IRWXU)
        # Deliberately construct a small environment.  In particular, provider
        # tokens and vault keys are not inherited by numerical workers.
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHONNOUSERSITE": "1",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MPL_IGNORE_SYSTEM_FONTS": "1",
        }
        return environment

    @staticmethod
    def _command(row: sqlite3.Row) -> builtins.list[str]:
        # This list is intentionally constant apart from manager-generated
        # private paths.  User arguments are read from input.json by the worker.
        return [
            sys.executable,
            "-m",
            "energy_agent_tools.job_worker",
            "--job-id",
            str(row["job_id"]),
            "--input",
            str(row["input_path"]),
            "--output",
            str(row["output_path"]),
            "--state-dir",
            str(row["state_dir"]),
        ]

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=0.75)
        except TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def aclose(self) -> None:
        """Stop known workers, drain their tasks, and release the root lock."""

        if self._closed:
            return
        self._closing = True
        try:
            self._interrupt_running("The manager closed before this worker completed.")
            processes = builtins.list(self._processes.values())
            tasks = builtins.list(self._tasks)
            for process in processes:
                try:
                    await self._terminate(process)
                except Exception:
                    # Continue draining every known task before releasing the
                    # root lock.  A process that cannot be signalled is still
                    # represented as interrupted and the next manager will
                    # recover it safely.
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
            self._processes.clear()
            self._closed = True
            self._release_root_lock()

    async def __aenter__(self) -> JobManager:
        if self._closed:
            raise JobError("manager_closed", "The job manager is closed.")
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        await self.aclose()

    @staticmethod
    def _signal_now(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _mark_failed(self, job_id: str, code: str, message: str, *, output_bytes: int = 0) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, output_bytes = ?,
                    error_code = ?, error_message = ?, pid = NULL
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED.value,
                    _utcnow(),
                    output_bytes,
                    code,
                    message,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )

    def _mark_completed(self, job_id: str, output_bytes: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != JobStatus.RUNNING.value:
                connection.commit()
                return False
            user_total = int(
                connection.execute(
                    "SELECT COALESCE(SUM(output_bytes), 0) FROM jobs WHERE user_id = ?",
                    (row["user_id"],),
                ).fetchone()[0]
            )
            global_total = int(
                connection.execute("SELECT COALESCE(SUM(output_bytes), 0) FROM jobs").fetchone()[0]
            )
            if user_total + output_bytes > self.max_output_bytes_per_user:
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, finished_at = ?, error_code = ?,
                        error_message = ?, pid = NULL WHERE job_id = ? AND status = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        _utcnow(),
                        "output_quota",
                        "The user's durable output quota is exhausted.",
                        job_id,
                        JobStatus.RUNNING.value,
                    ),
                )
                connection.commit()
                return False
            if global_total + output_bytes > self.max_output_bytes_global:
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, finished_at = ?, error_code = ?,
                        error_message = ?, pid = NULL WHERE job_id = ? AND status = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        _utcnow(),
                        "output_quota",
                        "The global durable output quota is exhausted.",
                        job_id,
                        JobStatus.RUNNING.value,
                    ),
                )
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs SET status = ?, finished_at = ?, output_bytes = ?, pid = NULL
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    _utcnow(),
                    output_bytes,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )
            connection.commit()
            return True

    def _cleanup_input(self, row: sqlite3.Row) -> None:
        Path(str(row["input_path"])).unlink(missing_ok=True)

    async def _run_one(self, row: sqlite3.Row) -> None:
        job_id = str(row["job_id"])
        output_path = Path(str(row["output_path"]))
        state_dir = Path(str(row["state_dir"]))
        process: asyncio.subprocess.Process | None = None
        try:
            if self._raw_status(job_id) != JobStatus.RUNNING.value:
                self._cleanup_input(row)
                return
            state_dir.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
            _chmod_private(state_dir, stat.S_IRWXU)
            process = await asyncio.create_subprocess_exec(
                *self._command(row),
                cwd=str(self.root),
                env=self._safe_environment(state_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
            self._processes[job_id] = process
            self._set_pid(job_id, process.pid)
            if self._raw_status(job_id) != JobStatus.RUNNING.value:
                self._signal_now(process)
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
            except TimeoutError:
                await self._terminate(process)
                if self._raw_status(job_id) == JobStatus.RUNNING.value:
                    self._mark_failed(
                        job_id, "timeout", "The numerical worker exceeded its time limit."
                    )
                return
            current_status = self._raw_status(job_id)
            if current_status != JobStatus.RUNNING.value:
                return
            if return_code != 0:
                self._mark_failed(
                    job_id, "worker_failed", "The numerical worker exited without a result."
                )
                return
            try:
                output_bytes = output_path.stat().st_size
            except OSError:
                self._mark_failed(
                    job_id, "result_unavailable", "The numerical worker did not write a result."
                )
                return
            if output_bytes > self.max_output_bytes_per_job:
                self._mark_failed(
                    job_id, "output_limit", "The numerical result exceeds the per-job output quota."
                )
                output_path.unlink(missing_ok=True)
                return
            try:
                parsed = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._mark_failed(
                    job_id,
                    "invalid_result",
                    "The numerical worker wrote invalid JSON.",
                    output_bytes=output_bytes,
                )
                return
            if not isinstance(parsed, dict):
                self._mark_failed(
                    job_id,
                    "invalid_result",
                    "The numerical worker wrote an invalid result object.",
                    output_bytes=output_bytes,
                )
                return
            if not parsed.get("ok", False):
                error = parsed.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code", "worker_error"))[:80]
                    message = str(error.get("message", "The numerical operation failed."))[:500]
                else:
                    code = "worker_error"
                    message = "The numerical operation failed."
                self._mark_failed(job_id, code, message, output_bytes=output_bytes)
                return
            self._mark_completed(job_id, output_bytes)
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate(process)
            raise
        except (OSError, ValueError):
            if self._raw_status(job_id) == JobStatus.RUNNING.value:
                self._mark_failed(
                    job_id, "worker_start_failed", "The numerical worker could not be started."
                )
        finally:
            self._processes.pop(job_id, None)
            current = self._raw_status(job_id)
            if current in _TERMINAL_STATES:
                self._cleanup_input(row)

    async def run_pending(self) -> builtins.list[JobRecord]:
        """Run all pending jobs, maintaining the two-process concurrency bound."""

        self._ensure_open()
        records: builtins.list[JobRecord] = []
        async with self._run_lock:
            while True:
                if self._closing:
                    break
                available = self.max_concurrency - len(self._tasks)
                for row in self._claim_pending(available):
                    task = asyncio.create_task(self._run_one(row))
                    self._tasks.add(task)
                if not self._tasks:
                    break
                done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    self._tasks.discard(task)
                    try:
                        await task
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # _run_one converts expected worker failures into a
                        # durable state.  Keep the queue moving if an
                        # unexpected process API error escapes that boundary.
                        continue
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status != ? ORDER BY created_at",
                    (JobStatus.PENDING.value,),
                ).fetchall()
            records = [self._record(row) for row in rows]
        return records

    def cancel(self, job_id: str, user_id: str, session_id: str) -> JobRecord:
        """Cancel a pending or running job within its exact scope."""

        self._ensure_open()
        _validate_identity(user_id, "user_id")
        _validate_identity(session_id, "session_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound()
            self._authorise(row, user_id, session_id)
            if row["status"] in _TERMINAL_STATES:
                return self._record(row)
            connection.execute(
                """
                UPDATE jobs SET status = ?, finished_at = ?, error_code = ?,
                    error_message = ?, pid = NULL WHERE job_id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    _utcnow(),
                    "cancelled",
                    "The simulation job was cancelled by its owner.",
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
            connection.commit()
            process = self._processes.get(job_id)
        if process is not None:
            self._signal_now(process)
        return self.status(job_id, user_id, session_id)


__all__ = [
    "JobAccessDenied",
    "JobError",
    "JobManager",
    "JobNotFound",
    "JobNotReady",
    "JobQuotaExceeded",
    "JobRecord",
    "JobStatus",
    "SimulationOperation",
]
