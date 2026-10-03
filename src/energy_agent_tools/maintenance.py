"""Safe state backup and restore for self-hosted deployments.

The workbench database lives at the state root, the encrypted connection vault
lives below ``vault/``, and persistent identity records live below ``control/``.
This module backs up those databases with SQLite's online backup API and puts
them in a narrow, checksummed archive.  It does not copy operator configuration
or environment variables.  A vault key is included only when the caller
explicitly asks for it and supplies the key.

The running host must be stopped before a backup or restore.  SQLite's backup
API gives each database a consistent snapshot, but it cannot make a deployment
level snapshot of configuration, a mounted volume, or a credential provider.
Restore never overwrites a directory: the target must not exist before the
operation starts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import quote

_FORMAT = "energy-agent-tools-state"
_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_VAULT_KEY_NAME = "vault.key"
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_ARCHIVE_BYTES = 2_000_000_000
_MAX_ARCHIVE_MEMBERS = 1024
_COPY_CHUNK = 128 * 1024

_DATABASES: tuple[tuple[str, str, frozenset[str], str], ...] = (
    (
        "artifacts.sqlite3",
        "artifacts",
        frozenset({"artifacts"}),
        "workbench.v1",
    ),
    (
        "vault/auth.sqlite3",
        "auth",
        frozenset({"accounts", "oauth_transactions"}),
        "auth.v1",
    ),
    ("jobs/jobs.sqlite3", "jobs", frozenset({"jobs"}), "jobs.v1"),
    (
        "control/control.sqlite3",
        "control",
        frozenset({"users", "workspaces", "api_keys", "sites", "assets"}),
        "control.v1",
    ),
)


class MaintenanceError(RuntimeError):
    """A backup or restore failed a local safety or integrity check."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BackupFile:
    """One file recorded by a backup manifest."""

    path: str
    kind: str
    size: int
    sha256: str
    schema: str | None = None

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.schema is not None:
            value["schema"] = self.schema
        return value


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Validated metadata describing one state archive."""

    created_at: str
    files: tuple[BackupFile, ...]
    vault_key_included: bool
    format: str = _FORMAT
    version: int = _VERSION

    def as_json(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "created_at": self.created_at,
            "vault_key_included": self.vault_key_included,
            "files": [item.as_json() for item in self.files],
        }


def _fail(code: str, message: str) -> MaintenanceError:
    return MaintenanceError(code, message)


def _regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise _fail("missing_state", f"Required {label} is missing.") from exc
    if not stat.S_ISREG(mode):
        raise _fail("unsafe_state", f"Required {label} must be a regular file.")


def _directory(path: Path, *, label: str, must_exist: bool = True) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        if must_exist:
            raise _fail("missing_state", f"Required {label} directory is missing.") from exc
        return
    if not stat.S_ISDIR(mode):
        raise _fail("unsafe_state", f"{label} must be a directory.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now(clock: Callable[[], datetime] | None) -> str:
    value = clock() if clock else datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise _fail("invalid_clock", "Backup clock must return a timezone-aware datetime.")
    return value.astimezone(UTC).isoformat()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    # URI quoting keeps spaces and punctuation in operator paths safe while
    # mode=ro prevents a backup from creating or modifying a source database.
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise _fail("invalid_database", f"Cannot open {path.name} as SQLite.") from exc


def _validate_database(path: Path, required_tables: frozenset[str]) -> None:
    _regular_file(path, label="SQLite database")
    source = _connect_read_only(path)
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise _fail("invalid_database", "SQLite integrity check failed.")
        rows = source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {str(row[0]) for row in rows}
        if not required_tables <= tables:
            raise _fail("invalid_database", "SQLite schema is not recognized.")
    except sqlite3.Error as exc:
        raise _fail("invalid_database", "SQLite validation failed.") from exc
    finally:
        source.close()


def _copy_sqlite_snapshot(
    source_path: Path, destination: Path, required_tables: frozenset[str]
) -> None:
    """Copy one source database through SQLite's consistent backup API."""

    _validate_database(source_path, required_tables)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = _connect_read_only(source_path)
    target: sqlite3.Connection | None = None
    try:
        target = sqlite3.connect(destination)
        source.backup(target, pages=1000, sleep=0.05)
        target.commit()
    except sqlite3.Error as exc:
        raise _fail("backup_failed", "SQLite snapshot could not be created.") from exc
    finally:
        source.close()
        if target is not None:
            target.close()
    os.chmod(destination, 0o600)
    _validate_database(destination, required_tables)


def _copy_bytes(source: bytes, destination: Path) -> None:
    if not source or len(source) > 4096:
        raise _fail("invalid_vault_key", "Vault key must be a non-empty value up to 4096 bytes.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(source)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)


def _parse_manifest(value: Any) -> BackupManifest:
    if not isinstance(value, dict):
        raise _fail("invalid_manifest", "Backup manifest must be a JSON object.")
    if set(value) != {"format", "version", "created_at", "vault_key_included", "files"}:
        raise _fail("invalid_manifest", "Backup manifest fields are invalid.")
    if value["format"] != _FORMAT or value["version"] != _VERSION:
        raise _fail("unsupported_backup", "Backup format version is unsupported.")
    created_at = value["created_at"]
    if not isinstance(created_at, str):
        raise _fail("invalid_manifest", "Backup timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise _fail("invalid_manifest", "Backup timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail("invalid_manifest", "Backup timestamp must include a timezone.")
    if not isinstance(value["vault_key_included"], bool):
        raise _fail("invalid_manifest", "Vault key flag is invalid.")
    entries = value["files"]
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_ARCHIVE_MEMBERS:
        raise _fail("invalid_manifest", "Backup file list is invalid.")
    files: list[BackupFile] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"path", "kind", "size", "sha256", "schema"}:
            raise _fail("invalid_manifest", "Backup file entry is invalid.")
        path = entry.get("path")
        kind = entry.get("kind")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        schema = entry.get("schema")
        _validate_archive_name(path)
        if not isinstance(path, str):
            raise _fail("invalid_manifest", "Backup path is invalid.")
        if path == _MANIFEST_NAME or path in seen:
            raise _fail("invalid_manifest", "Backup file paths must be unique.")
        if not isinstance(kind, str) or kind not in {
            *(database_kind for _, database_kind, *_ in _DATABASES),
            "vault_key",
            "profile",
            "job_payload",
        }:
            raise _fail("invalid_manifest", "Backup file kind is invalid.")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > _MAX_ARCHIVE_BYTES
        ):
            raise _fail("invalid_manifest", "Backup file size is invalid.")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise _fail("invalid_manifest", "Backup file hash is invalid.")
        if schema is not None and (not isinstance(schema, str) or len(schema) > 100):
            raise _fail("invalid_manifest", "Backup file schema is invalid.")
        if kind == "vault_key" and path != _VAULT_KEY_NAME:
            raise _fail("invalid_manifest", "Vault key path is invalid.")
        allowed_paths = {name: database_kind for name, database_kind, *_ in _DATABASES}
        allowed_paths["profile.json"] = "profile"
        allowed_paths[_VAULT_KEY_NAME] = "vault_key"
        if re.fullmatch(r"jobs/[0-9a-f]{32}/(?:input|output)\.json", path):
            allowed_paths[path] = "job_payload"
        if allowed_paths.get(path) != kind:
            raise _fail("invalid_manifest", "Backup path does not match its declared kind.")
        files.append(BackupFile(path, kind, size, sha256, schema))
        seen.add(path)
    has_key = _VAULT_KEY_NAME in seen
    if has_key != value["vault_key_included"]:
        raise _fail("invalid_manifest", "Vault key flag does not match the file list.")
    if not any(item.kind in {kind for _, kind, *_ in _DATABASES} for item in files):
        raise _fail("invalid_manifest", "Backup contains no state database.")
    return BackupManifest(
        created_at=created_at,
        files=tuple(files),
        vault_key_included=bool(value["vault_key_included"]),
    )


def _validate_archive_name(name: Any) -> None:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise _fail("unsafe_archive", "Archive member path is invalid.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise _fail("unsafe_archive", "Archive member path must stay inside the state root.")


def _read_member(handle: IO[bytes], size: int) -> bytes:
    if size > _MAX_MANIFEST_BYTES:
        raise _fail("archive_too_large", "Backup manifest is too large.")
    value = handle.read(size + 1)
    if len(value) != size:
        raise _fail("truncated_archive", "Backup archive member is truncated.")
    return value


def _extract_member(member: tarfile.TarInfo, archive: tarfile.TarFile, destination: Path) -> None:
    if not member.isreg():
        raise _fail("unsafe_archive", "Backup archive may contain regular files only.")
    if member.size < 1 or member.size > _MAX_ARCHIVE_BYTES:
        raise _fail("archive_too_large", "Backup archive member is too large.")
    source = archive.extractfile(member)
    if source is None:
        raise _fail("truncated_archive", "Backup archive member cannot be read.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:
        remaining = member.size
        digest = hashlib.sha256()
        while remaining:
            chunk = source.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise _fail("truncated_archive", "Backup archive member is truncated.")
            target.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise _fail("unsafe_archive", "Backup archive member contains excess data.")
        target.flush()
        os.fsync(target.fileno())
    os.chmod(destination, 0o600)


def _extract_verified(archive_path: Path, destination: Path) -> BackupManifest:
    _regular_file(archive_path, label="backup archive")
    if archive_path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise _fail("archive_too_large", "Backup archive is too large.")
    try:
        archive = tarfile.open(archive_path, mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise _fail("invalid_archive", "Backup archive cannot be opened.") from exc
    try:
        members: list[tarfile.TarInfo] = []
        by_name: dict[str, tarfile.TarInfo] = {}
        total = 0
        while True:
            try:
                member = archive.next()
            except tarfile.TarError as exc:
                raise _fail("invalid_archive", "Backup archive is truncated or malformed.") from exc
            if member is None:
                break
            members.append(member)
            if len(members) > _MAX_ARCHIVE_MEMBERS + 1:
                raise _fail("unsafe_archive", "Backup archive member count is invalid.")
            _validate_archive_name(member.name)
            if member.name in by_name:
                raise _fail("unsafe_archive", "Backup archive contains duplicate members.")
            if not member.isreg():
                raise _fail("unsafe_archive", "Backup archive may contain regular files only.")
            if member.size < 1 or member.size > _MAX_ARCHIVE_BYTES:
                raise _fail("archive_too_large", "Backup archive member is too large.")
            total += member.size
            if total > _MAX_ARCHIVE_BYTES:
                raise _fail("archive_too_large", "Backup archive expands beyond its limit.")
            by_name[member.name] = member
        if not members:
            raise _fail("unsafe_archive", "Backup archive member count is invalid.")
        manifest_member = by_name.get(_MANIFEST_NAME)
        if manifest_member is None:
            raise _fail("invalid_manifest", "Backup archive has no manifest.")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise _fail("truncated_archive", "Backup manifest cannot be read.")
        try:
            manifest_value = json.loads(_read_member(manifest_stream, manifest_member.size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("invalid_manifest", "Backup manifest is not valid JSON.") from exc
        manifest = _parse_manifest(manifest_value)
        expected = {_MANIFEST_NAME, *(entry.path for entry in manifest.files)}
        if set(by_name) != expected:
            raise _fail("unsafe_archive", "Backup archive has unexpected members.")
        for entry in manifest.files:
            member = by_name[entry.path]
            if member.size != entry.size:
                raise _fail("hash_mismatch", "Backup manifest size does not match the archive.")
            target = destination / entry.path
            _extract_member(member, archive, target)
            if _sha256(target) != entry.sha256:
                raise _fail("hash_mismatch", "Backup member hash does not match the manifest.")
        for name, _kind, tables, _schema in _DATABASES:
            target = destination / name
            if target.exists():
                _validate_database(target, tables)
        return manifest
    finally:
        archive.close()


def create_backup(
    state_root: Path,
    archive_path: Path,
    *,
    vault_key: bytes | str | None = None,
    include_vault_key: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> BackupManifest:
    """Create a checksummed state backup without touching operator config.

    ``state_root`` must be the stopped deployment's state directory.  Existing
    archive paths are refused.  Passing ``include_vault_key=True`` is an
    explicit request to place the supplied raw Fernet key in the archive.
    """

    state_root = Path(state_root)
    archive_path = Path(archive_path)
    _directory(state_root, label="state root")
    _directory(archive_path.parent, label="backup parent")
    if os.path.lexists(archive_path):
        raise _fail("target_exists", "Backup archive already exists.")
    if vault_key is not None and not include_vault_key:
        raise _fail("invalid_vault_key", "Set include_vault_key to include a vault key explicitly.")
    if include_vault_key and vault_key is None:
        raise _fail("invalid_vault_key", "An explicit vault key is required when including it.")
    key_bytes = vault_key.encode() if isinstance(vault_key, str) else vault_key
    if include_vault_key and key_bytes is not None and not key_bytes:
        raise _fail("invalid_vault_key", "Vault key must be non-empty.")

    temp_root = Path(tempfile.mkdtemp(prefix=".energy-backup-", dir=archive_path.parent))
    temp_archive: Path | None = None
    try:
        os.chmod(temp_root, 0o700)
        entries: list[BackupFile] = []
        for relative, kind, tables, schema in _DATABASES:
            source = state_root / relative
            if not source.exists():
                continue
            target = temp_root / relative
            _copy_sqlite_snapshot(source, target, tables)
            if kind in {"auth", "control"}:
                with _connect_read_only(target) as database:
                    database_version = int(database.execute("PRAGMA user_version").fetchone()[0])
                schema = f"{kind}.v{database_version}" if database_version else f"{kind}.v1"
            entries.append(
                BackupFile(relative, kind, target.stat().st_size, _sha256(target), schema)
            )
        if not entries:
            raise _fail("missing_state", "State root contains no recognized SQLite database.")
        profile = state_root / "profile.json"
        if profile.exists():
            _regular_file(profile, label="profile")
            raw = profile.read_bytes()
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise _fail("profile_too_large", "Profile exceeds the backup limit.")
            json.loads(raw)
            _copy_bytes(raw, temp_root / "profile.json")
            entries.append(
                BackupFile("profile.json", "profile", len(raw), _sha256(temp_root / "profile.json"))
            )
        job_database = temp_root / "jobs/jobs.sqlite3"
        if job_database.exists():
            with _connect_read_only(job_database) as database:
                job_rows = database.execute("SELECT job_id, status FROM jobs").fetchall()
            for job_id, status in job_rows:
                if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                    raise _fail("invalid_job", "Job identifier is invalid.")
                if status == "running":
                    raise _fail("active_jobs", "Stop and close the job manager before backup.")
                for filename in ("input.json", "output.json"):
                    relative = f"jobs/{job_id}/{filename}"
                    source = state_root / relative
                    if source.exists():
                        _regular_file(source, label="job payload")
                        if source.parent.is_symlink():
                            raise _fail("unsafe_state", "Job directories cannot be symlinks.")
                        raw = source.read_bytes()
                        if len(raw) > 8_000_000:
                            raise _fail("job_too_large", "Job payload exceeds the backup limit.")
                        _copy_bytes(raw, temp_root / relative)
                        entries.append(
                            BackupFile(
                                relative, "job_payload", len(raw), _sha256(temp_root / relative)
                            )
                        )
                    elif (status == "pending" and filename == "input.json") or (
                        status == "completed" and filename == "output.json"
                    ):
                        raise _fail(
                            "missing_job_payload", "Job state is missing its required payload."
                        )
        if len(entries) > _MAX_ARCHIVE_MEMBERS:
            raise _fail("archive_too_large", "Backup contains too many files.")
        if include_vault_key and key_bytes is not None:
            key_path = temp_root / _VAULT_KEY_NAME
            _copy_bytes(key_bytes, key_path)
            entries.append(
                BackupFile(_VAULT_KEY_NAME, "vault_key", key_path.stat().st_size, _sha256(key_path))
            )
        manifest = BackupManifest(_utc_now(clock), tuple(entries), include_vault_key)
        archive_fd, archive_name = tempfile.mkstemp(
            prefix=".energy-backup-", suffix=".tar.gz", dir=archive_path.parent
        )
        os.close(archive_fd)
        temp_archive = Path(archive_name)
        os.chmod(temp_archive, 0o600)
        with tarfile.open(temp_archive, mode="w:gz") as archive:
            for entry in manifest.files:
                archive.add(temp_root / entry.path, arcname=entry.path, recursive=False)
            manifest_bytes = json.dumps(
                manifest.as_json(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            info = tarfile.TarInfo(_MANIFEST_NAME)
            info.size = len(manifest_bytes)
            info.mode = 0o600
            info.mtime = 0
            import io

            archive.addfile(info, io.BytesIO(manifest_bytes))
        os.chmod(temp_archive, 0o600)
        os.replace(temp_archive, archive_path)
        temp_archive = None
        os.chmod(archive_path, 0o600)
        return manifest
    finally:
        if temp_archive is not None:
            temp_archive.unlink(missing_ok=True)
        shutil.rmtree(temp_root, ignore_errors=True)


def inspect_backup(archive_path: Path) -> BackupManifest:
    """Validate an archive and return its manifest without restoring it."""

    temporary = Path(tempfile.mkdtemp(prefix=".energy-inspect-", dir=Path(archive_path).parent))
    try:
        return _extract_verified(Path(archive_path), temporary)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def restore_backup(archive_path: Path, target_root: Path) -> BackupManifest:
    """Restore a validated archive into a new state directory.

    The target must not exist.  The archive is fully extracted and verified in
    a sibling temporary directory before an atomic rename publishes it.
    """

    archive_path = Path(archive_path)
    target_root = Path(target_root)
    _directory(target_root.parent, label="restore parent")
    if os.path.lexists(target_root):
        raise _fail("target_exists", "Restore refuses to overwrite an existing path.")
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=".energy-restore-", dir=target_root.parent)
    )
    assert temporary is not None
    os.chmod(temporary, 0o700)
    try:
        manifest = _extract_verified(archive_path, temporary)
        if os.path.lexists(target_root):
            raise _fail("target_exists", "Restore target appeared during restore.")
        os.replace(temporary, target_root)
        temporary = None
        os.chmod(target_root, 0o700)
        for entry in manifest.files:
            os.chmod(target_root / entry.path, 0o600)
        return manifest
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


__all__ = [
    "BackupFile",
    "BackupManifest",
    "MaintenanceError",
    "create_backup",
    "inspect_backup",
    "restore_backup",
]
