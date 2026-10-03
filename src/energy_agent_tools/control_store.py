"""Owner-private SQLite control-plane records for a self-hosted instance.

This first storage layer gives each user private workspaces, keys, sites, and
assets. It does not implement workspace membership or shared access control.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .control_contracts import (
    AgentKeyAccess,
    IssuableKeyAccess,
    KeyAccess,
    ManageKeyAccess,
    WorkspaceAssetRequest,
    WorkspaceMode,
    WorkspaceSiteRequest,
)
from .models import Asset, EnergyError, Site, StrictModel

__all__ = [
    "BootstrapWorkspace",
    "ControlStore",
    "IssuedKey",
    "KeyIdentity",
    "KeyRecord",
    "UserRecord",
    "WorkspaceRecord",
]

_SCHEMA_VERSION = 3
_LEGACY_ACCESS_JSON = '{"kind":"legacy-agent"}'
_ISSUABLE_ACCESS_ADAPTER: TypeAdapter[IssuableKeyAccess] = TypeAdapter(IssuableKeyAccess)
_KEY_ACCESS_ADAPTER: TypeAdapter[KeyAccess] = TypeAdapter(KeyAccess)
_TOKEN_PATTERN = re.compile(r"eat_[A-Za-z0-9_-]{43}\Z")
_MAX_TEXT_LENGTH = 256
_MAX_TOKEN_LENGTH = 4096


class _ControlRecord(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UserRecord(_ControlRecord):
    id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    name: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)


class WorkspaceRecord(_ControlRecord):
    id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    user_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    name: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    mode: WorkspaceMode


class KeyRecord(_ControlRecord):
    id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    user_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    workspace_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    name: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    created_at: datetime
    expires_at: datetime | None
    revoked: bool
    token_prefix: str = Field(min_length=1, max_length=16)
    access: KeyAccess

    @field_validator("created_at", "expires_at")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Key timestamps must be timezone-aware.")
        return value.astimezone(UTC)


class IssuedKey(_ControlRecord):
    key: KeyRecord
    token: str = Field(min_length=47, max_length=47, repr=False)


class BootstrapWorkspace(_ControlRecord):
    user: UserRecord
    workspace: WorkspaceRecord
    key: IssuedKey


class KeyIdentity(_ControlRecord):
    user_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    workspace_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    key_id: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    access: KeyAccess
    workspace_mode: WorkspaceMode


def _error(code: str, message: str) -> EnergyError:
    return EnergyError(code, message)


def _not_found() -> NoReturn:
    raise _error("not_found", "Resource was not found.")


def _invalid_request() -> NoReturn:
    raise _error("invalid_request", "Request is invalid.")


def _conflict() -> NoReturn:
    raise _error("conflict", "Resource already exists.")


def _validate_text(value: str, *, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _invalid_request()
    return value


def _parse_workspace_mode(value: object) -> WorkspaceMode:
    if value == "operator":
        return "operator"
    if value == "managed":
        return "managed"
    raise RuntimeError("Control store contains invalid workspace mode.") from None


def _validate_workspace_mode(value: object) -> WorkspaceMode:
    if value == "operator":
        return "operator"
    if value == "managed":
        return "managed"
    _invalid_request()


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid_request()
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("Control store contains an invalid timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("Control store contains a timezone-naive timestamp.")
    return parsed.astimezone(UTC)


def _access_json(access: KeyAccess) -> str:
    """Serialize an access grant deterministically without secret material."""
    return json.dumps(access.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _parse_access(value: str) -> KeyAccess:
    try:
        return _KEY_ACCESS_ADAPTER.validate_json(value)
    except (ValidationError, TypeError, ValueError):
        raise RuntimeError("Control store contains invalid key access.") from None


def _validate_issuable_access(access: IssuableKeyAccess) -> ManageKeyAccess | AgentKeyAccess:
    # The public API accepts the explicit grant models, never dictionaries or a
    # legacy grant. Reparse the JSON so even model_construct() instances cannot
    # bypass the grant constraints.
    if type(access) not in (ManageKeyAccess, AgentKeyAccess):
        _invalid_request()
    try:
        validated = _ISSUABLE_ACCESS_ADAPTER.validate_json(access.model_dump_json())
    except (ValidationError, TypeError, ValueError):
        _invalid_request()
    if type(validated) not in (ManageKeyAccess, AgentKeyAccess):
        _invalid_request()
    return validated


class ControlStore:
    """Persistent owner-private identities and energy site records."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._chmod(self.root, 0o700)
        self.path = self.root / "control.sqlite3"
        self._closed = False
        self._initialize()
        self._chmod(self.path, 0o600)

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            # Match AuthStore: POSIX modes are not available on every platform.
            pass

    def _initialize(self) -> None:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout = 30000")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError("Control store schema is newer than this version supports.")
            if version not in (0, 1, 2, _SCHEMA_VERSION):
                raise RuntimeError("Control store schema version is unsupported.")

            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError("Control store schema is newer than this version supports.")
            if version == 0:
                for statement in _SCHEMA_V1:
                    db.execute(statement)
                db.execute("PRAGMA user_version = 1")
                version = 1
            elif version not in (1, 2, _SCHEMA_VERSION):
                raise RuntimeError("Control store schema version is unsupported.")
            if version == 1:
                db.execute(
                    "ALTER TABLE api_keys ADD COLUMN access_json TEXT NOT NULL "
                    f"DEFAULT '{_LEGACY_ACCESS_JSON}'"
                )
                # SQLite fills existing rows with the column default. Keep the
                # explicit update to make the migration intent clear and to
                # converge databases created by any compatible v1 writer.
                db.execute("UPDATE api_keys SET access_json = ?", (_LEGACY_ACCESS_JSON,))
                db.execute("PRAGMA user_version = 2")
                version = 2
            if version == 2:
                db.execute(
                    "ALTER TABLE workspaces ADD COLUMN mode TEXT NOT NULL "
                    "DEFAULT 'operator' CHECK(mode IN ('operator', 'managed'))"
                )
                # A v2 workspace has no managed marker. Keep migration
                # conservative even if a compatible writer used another default.
                db.execute("UPDATE workspaces SET mode = 'operator'")
                db.execute("PRAGMA user_version = 3")
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("ControlStore is closed.")
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _require_user(db: sqlite3.Connection, user_id: str) -> None:
        if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            _not_found()

    @staticmethod
    def _require_workspace(db: sqlite3.Connection, user_id: str, workspace_id: str) -> None:
        if (
            db.execute(
                "SELECT 1 FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            ).fetchone()
            is None
        ):
            _not_found()

    @staticmethod
    def _key_record(row: sqlite3.Row) -> KeyRecord:
        created_at = _datetime(row["created_at"])
        if created_at is None:
            raise RuntimeError("Control store contains an invalid key timestamp.")
        expires_at = _datetime(row["expires_at"])
        return KeyRecord(
            id=row["id"],
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            created_at=created_at,
            expires_at=expires_at,
            revoked=bool(row["revoked"]),
            token_prefix=row["token_prefix"],
            access=_parse_access(row["access_json"]),
        )

    @staticmethod
    def _new_issued_key(
        user_id: str,
        workspace_id: str,
        name: str,
        expires_at: datetime | None,
        access: KeyAccess,
    ) -> tuple[IssuedKey, str]:
        token = f"eat_{secrets.token_urlsafe(32)}"
        key = KeyRecord(
            id=uuid4().hex,
            user_id=user_id,
            workspace_id=workspace_id,
            name=name,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
            revoked=False,
            token_prefix=token[:12],
            access=access,
        )
        issued = IssuedKey(key=key, token=token)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        return issued, token_hash

    @staticmethod
    def _insert_key(db: sqlite3.Connection, issued: IssuedKey, token_hash: str) -> None:
        record = issued.key
        db.execute(
            """INSERT INTO api_keys(
                id, user_id, workspace_id, name, token_hash, token_prefix,
                created_at, expires_at, revoked, access_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                record.id,
                record.user_id,
                record.workspace_id,
                record.name,
                token_hash,
                record.token_prefix,
                _iso(record.created_at),
                _iso(record.expires_at) if record.expires_at is not None else None,
                _access_json(record.access),
            ),
        )

    def create_user(self, user_id: str, name: str) -> UserRecord:
        user_id = _validate_text(user_id)
        name = _validate_text(name)
        record = UserRecord(id=user_id, name=name)
        with self._connection(write=True) as db:
            try:
                db.execute("INSERT INTO users(id, name) VALUES (?, ?)", (record.id, record.name))
            except sqlite3.IntegrityError as exc:
                raise _error("conflict", "User already exists.") from exc
        return record

    def create_workspace(
        self, user_id: str, name: str, *, mode: WorkspaceMode = "operator"
    ) -> WorkspaceRecord:
        user_id = _validate_text(user_id)
        name = _validate_text(name)
        mode = _validate_workspace_mode(mode)
        with self._connection(write=True) as db:
            self._require_user(db, user_id)
            record = WorkspaceRecord(id=uuid4().hex, user_id=user_id, name=name, mode=mode)
            db.execute(
                "INSERT INTO workspaces(id, user_id, name, mode) VALUES (?, ?, ?, ?)",
                (record.id, record.user_id, record.name, record.mode),
            )
        return record

    def bootstrap_workspace(
        self, owner_name: str, workspace_name: str, key_name: str = "Management key"
    ) -> BootstrapWorkspace:
        owner_name = _validate_text(owner_name)
        workspace_name = _validate_text(workspace_name)
        key_name = _validate_text(key_name)
        user = UserRecord(id=uuid4().hex, name=owner_name)
        workspace = WorkspaceRecord(
            id=uuid4().hex,
            user_id=user.id,
            name=workspace_name,
            mode="managed",
        )
        issued, token_hash = self._new_issued_key(
            user.id, workspace.id, key_name, None, ManageKeyAccess()
        )
        with self._connection(write=True) as db:
            db.execute("INSERT INTO users(id, name) VALUES (?, ?)", (user.id, user.name))
            db.execute(
                "INSERT INTO workspaces(id, user_id, name, mode) VALUES (?, ?, ?, ?)",
                (workspace.id, workspace.user_id, workspace.name, workspace.mode),
            )
            self._insert_key(db, issued, token_hash)
        return BootstrapWorkspace(user=user, workspace=workspace, key=issued)

    def workspace(self, user_id: str, workspace_id: str) -> WorkspaceRecord:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        with self._connection() as db:
            row = db.execute(
                """SELECT id, user_id, name, mode FROM workspaces
                   WHERE id = ? AND user_id = ?""",
                (workspace_id, user_id),
            ).fetchone()
        if row is None:
            _not_found()
        return WorkspaceRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            mode=_parse_workspace_mode(row["mode"]),
        )

    def workspaces(self, user_id: str) -> list[WorkspaceRecord]:
        user_id = _validate_text(user_id)
        with self._connection() as db:
            self._require_user(db, user_id)
            rows = db.execute(
                "SELECT id, user_id, name, mode FROM workspaces WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [
            WorkspaceRecord(
                id=row["id"],
                user_id=row["user_id"],
                name=row["name"],
                mode=_parse_workspace_mode(row["mode"]),
            )
            for row in rows
        ]

    def create_key(
        self,
        user_id: str,
        workspace_id: str,
        name: str,
        expires_at: datetime | None = None,
        *,
        access: IssuableKeyAccess,
    ) -> IssuedKey:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        name = _validate_text(name)
        expiry = _aware_utc(expires_at)
        access = _validate_issuable_access(access)
        if expiry is not None and expiry <= datetime.now(UTC):
            _invalid_request()

        issued, token_hash = self._new_issued_key(user_id, workspace_id, name, expiry, access)

        with self._connection(write=True) as db:
            self._require_workspace(db, user_id, workspace_id)
            if isinstance(access, AgentKeyAccess):
                for site_id in access.site_ids:
                    if (
                        db.execute(
                            """SELECT 1 FROM sites
                               WHERE id = ? AND user_id = ? AND workspace_id = ?""",
                            (site_id, user_id, workspace_id),
                        ).fetchone()
                        is None
                    ):
                        _not_found()
            self._insert_key(db, issued, token_hash)
        return issued

    def authenticate(self, raw_token: str) -> KeyIdentity | None:
        if (
            not isinstance(raw_token, str)
            or len(raw_token) > _MAX_TOKEN_LENGTH
            or _TOKEN_PATTERN.fullmatch(raw_token) is None
        ):
            return None
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = _iso(datetime.now(UTC))
        with self._connection() as db:
            row = db.execute(
                """SELECT k.id, k.user_id, k.workspace_id, k.access_json, w.mode
                   FROM api_keys AS k
                   JOIN workspaces AS w
                     ON w.id = k.workspace_id AND w.user_id = k.user_id
                   WHERE k.token_hash = ? AND k.revoked = 0
                     AND (k.expires_at IS NULL OR k.expires_at > ?)""",
                (token_hash, now),
            ).fetchone()
        if row is None:
            return None
        try:
            access = _parse_access(row["access_json"])
            workspace_mode = _parse_workspace_mode(row["mode"])
        except RuntimeError:
            return None
        return KeyIdentity(
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            key_id=row["id"],
            access=access,
            workspace_mode=workspace_mode,
        )

    def keys(self, user_id: str, workspace_id: str) -> list[KeyRecord]:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        with self._connection() as db:
            self._require_workspace(db, user_id, workspace_id)
            rows = db.execute(
                """SELECT id, user_id, workspace_id, name, token_prefix,
                          created_at, expires_at, revoked, access_json
                   FROM api_keys WHERE user_id = ? AND workspace_id = ? ORDER BY created_at, id""",
                (user_id, workspace_id),
            ).fetchall()
        return [self._key_record(row) for row in rows]

    def revoke_key(self, user_id: str, workspace_id: str, key_id: str) -> None:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        key_id = _validate_text(key_id)
        with self._connection(write=True) as db:
            self._require_workspace(db, user_id, workspace_id)
            cursor = db.execute(
                """UPDATE api_keys SET revoked = 1
                   WHERE id = ? AND user_id = ? AND workspace_id = ?""",
                (key_id, user_id, workspace_id),
            )
            if cursor.rowcount == 0:
                _not_found()

    def put_site(self, user_id: str, workspace_id: str, site: Site) -> Site:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        if not isinstance(site, Site):
            _invalid_request()
        _validate_text(site.id)
        _validate_text(site.user_id)
        if site.user_id != user_id:
            _not_found()
        payload = site.model_dump_json()

        with self._connection(write=True) as db:
            self._require_workspace(db, user_id, workspace_id)
            existing = db.execute(
                "SELECT user_id, workspace_id FROM sites WHERE id = ?", (site.id,)
            ).fetchone()
            if existing is not None and (
                existing["user_id"] != user_id or existing["workspace_id"] != workspace_id
            ):
                _not_found()
            if existing is None:
                db.execute(
                    "INSERT INTO sites(id, user_id, workspace_id, site_json) VALUES (?, ?, ?, ?)",
                    (site.id, user_id, workspace_id, payload),
                )
            else:
                db.execute(
                    """UPDATE sites SET site_json = ?
                       WHERE id = ? AND user_id = ? AND workspace_id = ?""",
                    (payload, site.id, user_id, workspace_id),
                )
        return site

    def create_site(
        self,
        user_id: str,
        workspace_id: str,
        *,
        name: str,
        timezone: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> Site:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        try:
            request = WorkspaceSiteRequest(
                name=name,
                timezone=timezone,
                latitude=latitude,
                longitude=longitude,
            )
            site = Site(
                id=uuid4().hex,
                user_id=user_id,
                name=request.name,
                timezone=request.timezone,
                latitude=request.latitude,
                longitude=request.longitude,
            )
        except ValidationError:
            _invalid_request()

        try:
            with self._connection(write=True) as db:
                self._require_workspace(db, user_id, workspace_id)
                db.execute(
                    "INSERT INTO sites(id, user_id, workspace_id, site_json) VALUES (?, ?, ?, ?)",
                    (site.id, user_id, workspace_id, site.model_dump_json()),
                )
        except sqlite3.IntegrityError:
            _conflict()
        return site

    def sites(self, user_id: str, workspace_id: str) -> list[Site]:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        with self._connection() as db:
            self._require_workspace(db, user_id, workspace_id)
            rows = db.execute(
                """SELECT site_json FROM sites
                   WHERE user_id = ? AND workspace_id = ? ORDER BY id""",
                (user_id, workspace_id),
            ).fetchall()
        return [Site.model_validate_json(row["site_json"]) for row in rows]

    def put_asset(self, user_id: str, workspace_id: str, asset: Asset) -> Asset:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        if not isinstance(asset, Asset):
            _invalid_request()
        _validate_text(asset.id)
        _validate_text(asset.site_id)
        if asset.parent_id is not None:
            _validate_text(asset.parent_id)
        payload = asset.model_dump_json()

        with self._connection(write=True) as db:
            self._require_workspace(db, user_id, workspace_id)
            site = db.execute(
                """SELECT 1 FROM sites
                   WHERE id = ? AND user_id = ? AND workspace_id = ?""",
                (asset.site_id, user_id, workspace_id),
            ).fetchone()
            if site is None:
                _not_found()

            existing = db.execute(
                "SELECT user_id, workspace_id, site_id FROM assets WHERE id = ?", (asset.id,)
            ).fetchone()
            if existing is not None and (
                existing["user_id"] != user_id
                or existing["workspace_id"] != workspace_id
                or existing["site_id"] != asset.site_id
            ):
                _not_found()

            self._check_parent(db, user_id, workspace_id, asset)
            if existing is None:
                db.execute(
                    """INSERT INTO assets(
                        id, user_id, workspace_id, site_id, parent_id, asset_json
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (asset.id, user_id, workspace_id, asset.site_id, asset.parent_id, payload),
                )
            else:
                db.execute(
                    """UPDATE assets SET parent_id = ?, asset_json = ?
                       WHERE id = ? AND user_id = ? AND workspace_id = ? AND site_id = ?""",
                    (
                        asset.parent_id,
                        payload,
                        asset.id,
                        user_id,
                        workspace_id,
                        asset.site_id,
                    ),
                )
        return asset

    def create_asset(
        self,
        user_id: str,
        workspace_id: str,
        *,
        site_id: str,
        name: str,
        kind: str,
        parent_id: str | None = None,
        account_ids: list[str] | tuple[str, ...] = (),
    ) -> Asset:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        if not isinstance(account_ids, (list, tuple)):
            _invalid_request()
        try:
            request = WorkspaceAssetRequest(
                site_id=site_id,
                name=name,
                kind=kind,
                parent_id=parent_id,
                account_ids=list(account_ids),
            )
            asset = Asset(
                id=uuid4().hex,
                site_id=request.site_id,
                kind=request.kind,
                name=request.name,
                parent_id=request.parent_id,
                account_ids=request.account_ids,
            )
        except ValidationError:
            _invalid_request()

        try:
            with self._connection(write=True) as db:
                self._require_workspace(db, user_id, workspace_id)
                site = db.execute(
                    """SELECT 1 FROM sites
                       WHERE id = ? AND user_id = ? AND workspace_id = ?""",
                    (asset.site_id, user_id, workspace_id),
                ).fetchone()
                if site is None:
                    _not_found()
                self._check_parent(db, user_id, workspace_id, asset)
                db.execute(
                    """INSERT INTO assets(
                        id, user_id, workspace_id, site_id, parent_id, asset_json
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        asset.id,
                        user_id,
                        workspace_id,
                        asset.site_id,
                        asset.parent_id,
                        asset.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError:
            _conflict()
        return asset

    @staticmethod
    def _check_parent(
        db: sqlite3.Connection, user_id: str, workspace_id: str, asset: Asset
    ) -> None:
        parent_id = asset.parent_id
        visited = {asset.id}
        while parent_id is not None:
            if parent_id in visited:
                _invalid_request()
            visited.add(parent_id)
            row = db.execute(
                """SELECT parent_id FROM assets
                   WHERE id = ? AND user_id = ? AND workspace_id = ? AND site_id = ?""",
                (parent_id, user_id, workspace_id, asset.site_id),
            ).fetchone()
            if row is None:
                _not_found()
            parent_id = row["parent_id"]

    def assets(self, user_id: str, workspace_id: str) -> list[Asset]:
        user_id = _validate_text(user_id)
        workspace_id = _validate_text(workspace_id)
        with self._connection() as db:
            self._require_workspace(db, user_id, workspace_id)
            rows = db.execute(
                """SELECT asset_json FROM assets
                   WHERE user_id = ? AND workspace_id = ? ORDER BY id""",
                (user_id, workspace_id),
            ).fetchall()
        return [Asset.model_validate_json(row["asset_json"]) for row in rows]

    def close(self) -> None:
        self._closed = True


_SCHEMA_V1 = (
    """CREATE TABLE users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 256)
    )""",
    """CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
        UNIQUE(id, user_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
        token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
        token_prefix TEXT NOT NULL CHECK(length(token_prefix) BETWEEN 1 AND 16),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked INTEGER NOT NULL CHECK(revoked IN (0, 1)),
        FOREIGN KEY(workspace_id, user_id) REFERENCES workspaces(id, user_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX api_keys_scope ON api_keys(user_id, workspace_id, created_at, id)",
    """CREATE TABLE sites (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        site_json TEXT NOT NULL,
        UNIQUE(id, user_id, workspace_id),
        FOREIGN KEY(workspace_id, user_id) REFERENCES workspaces(id, user_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX sites_scope ON sites(user_id, workspace_id, id)",
    """CREATE TABLE assets (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        site_id TEXT NOT NULL,
        parent_id TEXT,
        asset_json TEXT NOT NULL,
        UNIQUE(id, user_id, workspace_id, site_id),
        FOREIGN KEY(workspace_id, user_id) REFERENCES workspaces(id, user_id) ON DELETE CASCADE,
        FOREIGN KEY(site_id, user_id, workspace_id)
            REFERENCES sites(id, user_id, workspace_id) ON DELETE CASCADE,
        FOREIGN KEY(parent_id, user_id, workspace_id, site_id)
            REFERENCES assets(id, user_id, workspace_id, site_id)
    )""",
    "CREATE INDEX assets_scope ON assets(user_id, workspace_id, id)",
)
