"""Local, encrypted connection storage and OAuth helpers.

The store is deliberately independent of :mod:`runtime`.  It owns the
credential boundary, while the runtime remains responsible for deciding when
trusted connector code may request a credential.  Public methods return
``ConnectedAccount`` metadata only.  Access and refresh tokens live in an
encrypted SQLite blob and are never part of an account returned to an agent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .models import AuthConfig, ConnectedAccount, EnergyError, Json, Site

_DEFAULT_STATE_TTL = 600
_DEFAULT_REFRESH_SKEW = 60
_HTTP_TIMEOUT = 15.0
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_AUTH_SCHEMA_VERSION = 1
_RESERVED_AUTHORIZATION_PARAMS = {
    "client_id",
    "code_challenge",
    "code_challenge_method",
    "grant_type",
    "redirect_uri",
    "response_type",
    "state",
}
_SECRET_KEYS = {
    "access_token",
    "apikey",
    "api_key",
    "client_secret",
    "credential",
    "credential_value",
    "password",
    "refresh_token",
    "secret",
    "token",
}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid datetime")
    return datetime.fromisoformat(value).astimezone(UTC)


def _safe_error(code: str, message: str, *, retryable: bool = False) -> EnergyError:
    """Create errors whose messages contain no provider response data."""

    return EnergyError(code, message, retryable)


def _state_hash(state: str) -> str:
    try:
        encoded = state.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _safe_error("oauth_state_invalid", "OAuth state is invalid or expired.") from exc
    return hashlib.sha256(encoded).hexdigest()


def _has_secret_key(value: Any) -> bool:
    """Reject credential-shaped settings before they can become account metadata."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_token"):
                return True
            if _has_secret_key(nested):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_secret_key(item) for item in value)
    return False


def _json_safe(value: Any) -> Any:
    """Return JSON-safe values without calling arbitrary provider objects."""

    return json.loads(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _model_fields(model: type[Any]) -> Mapping[str, Any]:
    return getattr(model, "model_fields", {})


def _provider_url(value: str, *, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
        raise ValueError(f"{name} must be an HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{name} must use HTTPS")
    if parsed.fragment:
        raise ValueError(f"{name} must not contain a fragment")
    return value


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """Operator-supplied OAuth 2.0 endpoints and client configuration."""

    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    client_secret: str | None = field(default=None, repr=False)
    revocation_endpoint: str | None = None
    token_endpoint_auth_method: Literal["none", "client_secret_basic", "client_secret_post"] = (
        "none"
    )
    state_ttl_seconds: int = _DEFAULT_STATE_TTL
    refresh_skew_seconds: int = _DEFAULT_REFRESH_SKEW
    extra_authorization_params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.authorization_endpoint, str) or not isinstance(
            self.token_endpoint, str
        ):
            raise ValueError("OAuth endpoints must be strings")
        if self.client_secret is not None and not isinstance(self.client_secret, str):
            raise ValueError("client_secret must be a string")
        _provider_url(self.authorization_endpoint, name="authorization_endpoint")
        _provider_url(self.token_endpoint, name="token_endpoint")
        if self.revocation_endpoint:
            _provider_url(self.revocation_endpoint, name="revocation_endpoint")
        if not isinstance(self.client_id, str) or not self.client_id:
            raise ValueError("client_id is required")
        if not isinstance(self.redirect_uri, str) or not self.redirect_uri:
            raise ValueError("redirect_uri is required")
        _provider_url(self.redirect_uri, name="redirect_uri")
        if self.state_ttl_seconds < 1 or self.state_ttl_seconds > 3600:
            raise ValueError("state_ttl_seconds must be between 1 and 3600")
        if self.refresh_skew_seconds < 0 or self.refresh_skew_seconds > 3600:
            raise ValueError("refresh_skew_seconds must be between 0 and 3600")
        if self.token_endpoint_auth_method != "none" and not self.client_secret:
            raise ValueError("client_secret is required for client-secret auth")
        if _has_secret_key(self.extra_authorization_params):
            raise ValueError("authorization parameters cannot contain secrets")
        if any(
            key.lower() in _RESERVED_AUTHORIZATION_PARAMS for key in self.extra_authorization_params
        ):
            raise ValueError("authorization parameters cannot override OAuth protocol fields")

    @property
    def authorization_url(self) -> str:
        """Compatibility alias for callers that name OAuth endpoints as URLs."""

        return self.authorization_endpoint

    @property
    def token_url(self) -> str:
        """Compatibility alias for callers that name OAuth endpoints as URLs."""

        return self.token_endpoint


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    connection_id: str
    authorization_url: str
    state: str
    expires_at: datetime

    @property
    def url(self) -> str:
        return self.authorization_url


@dataclass(frozen=True, slots=True)
class _OAuthTransaction:
    user_id: str
    connection_id: str
    redirect_uri: str
    expires_at: datetime
    verifier: str
    provider: OAuthProvider
    account_updated_at: str


class AuthStore:
    """A scoped, encrypted local connection store.

    ``master_key`` must be a valid Fernet key supplied by the operator.  The
    key is held only in memory.  The store does not derive or persist a key and
    it never falls back to plaintext storage.
    """

    def __init__(
        self,
        root: Path,
        master_key: bytes,
        http: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(master_key, bytes):
            raise TypeError("master_key must be bytes containing a Fernet key")
        try:
            self._cipher = Fernet(master_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("master_key must be a valid Fernet key") from exc

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod(self.root, 0o700)
        self.path = self.root / "auth.sqlite3"
        self._initialize(self.path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = DELETE")
        self._db.execute("PRAGMA secure_delete = ON")
        self._chmod(self.path, 0o600)
        self.http = http
        self._clock = clock or _now_utc
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _index_names(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA index_list({table})")}

    @staticmethod
    def _index_columns(db: sqlite3.Connection, index: str) -> list[str]:
        return [str(row[2]) for row in db.execute(f"PRAGMA index_info({index})")]

    @classmethod
    def _initialize(cls, path: Path) -> None:
        db = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout = 30000")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _AUTH_SCHEMA_VERSION:
                raise RuntimeError("Auth store schema is newer than this version supports.")
            if version not in (0, _AUTH_SCHEMA_VERSION):
                raise RuntimeError("Auth store schema version is unsupported.")

            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _AUTH_SCHEMA_VERSION:
                raise RuntimeError("Auth store schema is newer than this version supports.")
            if version == 0:
                tables = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if "accounts" not in tables:
                    if tables:
                        raise RuntimeError("Auth store schema is corrupt or unsupported.")
                    db.execute(
                        """
                        CREATE TABLE accounts (
                            id TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            toolkit TEXT NOT NULL,
                            site_id TEXT,
                            account_json TEXT NOT NULL,
                            secret_blob BLOB NOT NULL,
                            state TEXT NOT NULL,
                            enabled INTEGER NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            last_verified_at TEXT,
                            expires_at TEXT,
                            last_error TEXT,
                            workspace_id TEXT,
                            managed_revision INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )
                else:
                    if tables != {"accounts", "oauth_transactions"}:
                        raise RuntimeError("Auth store schema is corrupt or unsupported.")
                    columns = cls._table_columns(db, "accounts")
                    legacy_columns = {
                        "id",
                        "user_id",
                        "toolkit",
                        "site_id",
                        "account_json",
                        "secret_blob",
                        "state",
                        "enabled",
                        "created_at",
                        "updated_at",
                        "last_verified_at",
                        "expires_at",
                        "last_error",
                    }
                    if (
                        not legacy_columns.issubset(columns)
                        or {
                            "workspace_id",
                            "managed_revision",
                        }
                        & columns
                    ):
                        raise RuntimeError("Auth store schema is corrupt or unsupported.")
                    db.execute("ALTER TABLE accounts ADD COLUMN workspace_id TEXT")
                    db.execute(
                        "ALTER TABLE accounts ADD COLUMN managed_revision INTEGER NOT NULL DEFAULT 0"
                    )
                if "accounts" not in tables:
                    db.execute(
                        """
                        CREATE TABLE oauth_transactions (
                            state_hash TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            connection_id TEXT NOT NULL,
                            redirect_uri TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            consumed_at TEXT,
                            transaction_blob BLOB NOT NULL
                        )
                        """
                    )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS accounts_scope ON accounts(user_id, site_id, toolkit)"
                )
                db.execute(
                    "CREATE INDEX accounts_workspace_scope ON accounts(user_id, workspace_id, id)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS oauth_expiry ON oauth_transactions(expires_at)"
                )
                db.execute(f"PRAGMA user_version = {_AUTH_SCHEMA_VERSION}")

            cls._validate_schema(db)
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @classmethod
    def _validate_schema(cls, db: sqlite3.Connection) -> None:
        required_account_columns = {
            "id",
            "user_id",
            "toolkit",
            "site_id",
            "account_json",
            "secret_blob",
            "state",
            "enabled",
            "created_at",
            "updated_at",
            "last_verified_at",
            "expires_at",
            "last_error",
            "workspace_id",
            "managed_revision",
        }
        required_oauth_columns = {
            "state_hash",
            "user_id",
            "connection_id",
            "redirect_uri",
            "expires_at",
            "consumed_at",
            "transaction_blob",
        }
        if (
            int(db.execute("PRAGMA user_version").fetchone()[0]) != _AUTH_SCHEMA_VERSION
            or {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            != {"accounts", "oauth_transactions"}
            or required_account_columns != cls._table_columns(db, "accounts")
            or required_oauth_columns != cls._table_columns(db, "oauth_transactions")
            or not {"accounts_scope", "accounts_workspace_scope"}.issubset(
                cls._index_names(db, "accounts")
            )
            or "oauth_expiry" not in cls._index_names(db, "oauth_transactions")
            or cls._index_columns(db, "accounts_scope") != ["user_id", "site_id", "toolkit"]
            or cls._index_columns(db, "accounts_workspace_scope")
            != ["user_id", "workspace_id", "id"]
            or cls._index_columns(db, "oauth_expiry") != ["expires_at"]
        ):
            raise RuntimeError("Auth store schema is corrupt or unsupported.")

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            # Windows ACLs do not provide POSIX modes.  The database still uses
            # encrypted blobs and the operator is responsible for ACLs there.
            pass

    def close(self) -> None:
        self._db.close()

    def _refresh_lock(self, connection_id: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(connection_id)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[connection_id] = lock
        return lock

    def _now(self) -> datetime:
        value = _as_utc(self._clock())
        if value is None:
            raise RuntimeError("clock returned no datetime")
        return value

    @staticmethod
    def _account_dump(account: ConnectedAccount) -> Json:
        try:
            data = account.model_dump(mode="json")
        except AttributeError as exc:
            raise TypeError("connection must be a ConnectedAccount") from exc
        if not isinstance(data, dict):
            raise TypeError("connection must serialize to an object")
        if _has_secret_key(data.get("settings", {})):
            raise ValueError("connection settings cannot contain credential values")
        # Future model fields are allowed, but a credential-shaped top-level
        # value must never become public account metadata.
        for key in tuple(data):
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_token"):
                data.pop(key, None)
        auth = data.get("auth")
        if not isinstance(auth, dict):
            auth = {}
        auth.pop("credential_env", None)
        if "secret_id" in _model_fields(AuthConfig):
            auth["secret_id"] = data.get("id")
        data["auth"] = auth
        return _json_safe(data)

    @staticmethod
    def _account_with_metadata(
        data: Json,
        *,
        state: str,
        enabled: bool,
        last_verified_at: datetime | None,
        expires_at: datetime | None,
    ) -> ConnectedAccount:
        payload = dict(data)
        payload["enabled"] = enabled
        fields = _model_fields(ConnectedAccount)
        if "state" in fields:
            payload["state"] = state
        if "last_verified_at" in fields:
            payload["last_verified_at"] = _iso(last_verified_at)
        if "expires_at" in fields:
            payload["expires_at"] = _iso(expires_at)
        try:
            return ConnectedAccount.model_validate(payload)
        except Exception as exc:
            raise _safe_error(
                "connection_corrupt", "Stored connection metadata is invalid."
            ) from exc

    @staticmethod
    def _provider_dump(provider: OAuthProvider) -> Json:
        return {
            "authorization_endpoint": provider.authorization_endpoint,
            "token_endpoint": provider.token_endpoint,
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "scopes": list(provider.scopes),
            "client_secret": provider.client_secret,
            "revocation_endpoint": provider.revocation_endpoint,
            "token_endpoint_auth_method": provider.token_endpoint_auth_method,
            "state_ttl_seconds": provider.state_ttl_seconds,
            "refresh_skew_seconds": provider.refresh_skew_seconds,
            "extra_authorization_params": dict(provider.extra_authorization_params),
        }

    @staticmethod
    def _provider_load(data: Mapping[str, Any]) -> OAuthProvider:
        try:
            return OAuthProvider(
                authorization_endpoint=str(data["authorization_endpoint"]),
                token_endpoint=str(data["token_endpoint"]),
                client_id=str(data["client_id"]),
                redirect_uri=str(data["redirect_uri"]),
                scopes=tuple(str(item) for item in data.get("scopes", [])),
                client_secret=data.get("client_secret"),
                revocation_endpoint=data.get("revocation_endpoint"),
                token_endpoint_auth_method=data.get("token_endpoint_auth_method", "none"),
                state_ttl_seconds=int(data.get("state_ttl_seconds", _DEFAULT_STATE_TTL)),
                refresh_skew_seconds=int(data.get("refresh_skew_seconds", _DEFAULT_REFRESH_SKEW)),
                extra_authorization_params={
                    str(k): str(v)
                    for k, v in dict(data.get("extra_authorization_params", {})).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _safe_error(
                "oauth_configuration_invalid", "Stored OAuth configuration is invalid."
            ) from exc

    def _encrypt_json(self, value: Json) -> bytes:
        return self._cipher.encrypt(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def _decrypt_json(self, value: bytes) -> Json:
        try:
            decoded = json.loads(self._cipher.decrypt(value).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _safe_error(
                "credential_unavailable", "Stored credential cannot be decrypted."
            ) from exc
        if not isinstance(decoded, dict):
            raise _safe_error("credential_unavailable", "Stored credential is invalid.")
        return decoded

    def validate_encryption_key(self) -> None:
        """Verify the current Fernet key against every persisted encrypted blob.

        The decrypted payloads are intentionally discarded here. Empty stores
        have no existing ciphertext to validate.
        """

        blobs = self._db.execute("SELECT secret_blob FROM accounts").fetchall()
        blobs.extend(self._db.execute("SELECT transaction_blob FROM oauth_transactions").fetchall())
        for row in blobs:
            try:
                self._decrypt_json(row[0])
            except (EnergyError, TypeError, ValueError) as exc:
                raise _safe_error(
                    "credential_unavailable", "Stored encrypted data cannot be decrypted."
                ) from exc

    def _row(self, user_id: str, connection_id: str, site_id: str | None) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (connection_id,)).fetchone()
        if row is None:
            raise _safe_error("connection_not_found", "Connection is not configured.")
        if row["user_id"] != user_id or (site_id is not None and row["site_id"] != site_id):
            raise _safe_error("account_forbidden", "Connection is outside this user/site scope.")
        return row

    def _view(self, row: sqlite3.Row) -> ConnectedAccount:
        try:
            data = json.loads(row["account_json"])
            if not isinstance(data, dict):
                raise ValueError
            workspace_id = row["workspace_id"]
            revision = row["managed_revision"]
            if data.get("workspace_id") != workspace_id:
                raise _safe_error("connection_corrupt", "Stored connection scope is invalid.")
            if type(revision) is not int or (workspace_id is None and revision != 0):
                raise _safe_error("connection_corrupt", "Stored connection revision is invalid.")
            if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id.strip() or revision < 1
            ):
                raise _safe_error("connection_corrupt", "Stored connection scope is invalid.")
            return self._account_with_metadata(
                data,
                state=row["state"],
                enabled=bool(row["enabled"]),
                last_verified_at=_parse_datetime(row["last_verified_at"]),
                expires_at=_parse_datetime(row["expires_at"]),
            )
        except EnergyError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _safe_error(
                "connection_corrupt", "Stored connection metadata is invalid."
            ) from exc

    def configure(self, connection: ConnectedAccount, credential: str) -> None:
        """Create or replace a connection and encrypt its credential."""

        if not isinstance(connection, ConnectedAccount):
            raise TypeError("connection must be a ConnectedAccount")
        if connection.workspace_id is not None:
            raise _safe_error(
                "managed_lifecycle_required",
                "Managed connections must use workspace-scoped lifecycle operations.",
            )
        if not isinstance(credential, str):
            raise ValueError("credential must be a non-empty string")
        if connection.auth.scheme != "oauth" and not credential:
            raise ValueError("credential must be a non-empty string")
        if not connection.id or not connection.user_id or not connection.toolkit:
            raise ValueError("connection id, user_id and toolkit are required")
        data = self._account_dump(connection)
        now = self._now()
        existing = self._db.execute(
            "SELECT * FROM accounts WHERE id = ?", (connection.id,)
        ).fetchone()
        if existing is not None:
            stored = self._view(existing)
            if existing["workspace_id"] is not None:
                raise _safe_error(
                    "managed_lifecycle_required",
                    "Managed connections must use workspace-scoped lifecycle operations.",
                )
            if (
                existing["user_id"] != connection.user_id
                or existing["site_id"] != connection.site_id
            ):
                raise _safe_error("account_forbidden", "Connection belongs to another user/site.")
            if stored.workspace_id is not None:
                raise _safe_error(
                    "managed_lifecycle_required",
                    "Managed connections must use workspace-scoped lifecycle operations.",
                )
        created_at = existing["created_at"] if existing is not None else _iso(now)
        # An OAuth connection can be registered before its authorization code
        # exchange.  It stays pending and has no executable credential until
        # the callback stores an access token.
        pending_oauth = connection.auth.scheme == "oauth" and not credential
        state = "pending" if pending_oauth else "active"
        enabled = not pending_oauth
        data["enabled"] = enabled
        if "state" in _model_fields(ConnectedAccount):
            data["state"] = state
        expires_at = _as_utc(getattr(connection, "expires_at", None))
        last_verified = _as_utc(getattr(connection, "last_verified_at", None))
        private = {
            "credential": credential or None,
            "refresh_token": None,
            "provider": None,
        }
        encrypted = self._encrypt_json(private)
        self._db.execute(
            """
            INSERT INTO accounts(
                id, user_id, toolkit, site_id, account_json, secret_blob, state,
                enabled, created_at, updated_at, last_verified_at, expires_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                account_json = excluded.account_json,
                secret_blob = excluded.secret_blob,
                state = excluded.state,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at,
                last_verified_at = excluded.last_verified_at,
                expires_at = excluded.expires_at,
                last_error = NULL
            """,
            (
                connection.id,
                connection.user_id,
                connection.toolkit,
                connection.site_id,
                json.dumps(data, separators=(",", ":"), sort_keys=True),
                encrypted,
                state,
                int(enabled),
                created_at,
                _iso(now),
                _iso(last_verified),
                _iso(expires_at),
            ),
        )
        self._db.commit()

    def get_account(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> ConnectedAccount:
        return self._view(self._row(user_id, connection_id, site_id))

    def accounts(self, user_id: str, site_id: str | None = None) -> list[ConnectedAccount]:
        if site_id is None:
            rows = self._db.execute(
                "SELECT * FROM accounts WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM accounts WHERE user_id = ? AND site_id = ? ORDER BY id",
                (user_id, site_id),
            ).fetchall()
        return [self._view(row) for row in rows]

    def workspace_accounts(self, user_id: str, workspace_id: str | None) -> list[ConnectedAccount]:
        """List accounts in exactly one namespace, including safe inactive metadata.

        ``workspace_id=None`` is an exact legacy/operator query; it does not
        include accounts belonging to managed workspaces.
        """

        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id is required")
        if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id):
            raise ValueError("workspace_id must be non-empty when provided")
        if workspace_id is None:
            rows = self._db.execute(
                "SELECT * FROM accounts WHERE user_id = ? AND workspace_id IS NULL ORDER BY id",
                (user_id,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM accounts WHERE user_id = ? AND workspace_id = ? ORDER BY id",
                (user_id, workspace_id),
            ).fetchall()
        return [self._view(row) for row in rows]

    def _managed_row(self, user_id: str, workspace_id: str, connection_id: str) -> sqlite3.Row:
        if not all(
            isinstance(value, str) and value for value in (user_id, workspace_id, connection_id)
        ):
            raise ValueError("user_id, workspace_id and connection_id are required")
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (connection_id,)).fetchone()
        if row is None:
            raise _safe_error("connection_not_found", "Connection is not configured.")
        if row["user_id"] != user_id or row["workspace_id"] != workspace_id:
            raise _safe_error(
                "account_forbidden", "Connection is outside this user/workspace scope."
            )
        return row

    @staticmethod
    def _managed_revision(row: sqlite3.Row) -> int:
        revision = row["managed_revision"]
        if type(revision) is not int or revision < 1:
            raise _safe_error("connection_corrupt", "Stored connection revision is invalid.")
        return revision

    @staticmethod
    def _expected_revision(value: int | None) -> int | None:
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("expected_version must be a positive integer")
        return value

    def managed_snapshot(
        self, user_id: str, workspace_id: str, connection_id: str
    ) -> tuple[ConnectedAccount, int]:
        """Return safe metadata and the durable revision for an exact workspace row."""

        row = self._managed_row(user_id, workspace_id, connection_id)
        account = self._view(row)
        return account, self._managed_revision(row)

    def pending_credential(self, user_id: str, workspace_id: str, connection_id: str) -> str:
        """Read a disabled pending secret for trusted connection-management code only."""

        row = self._managed_row(user_id, workspace_id, connection_id)
        account = self._view(row)
        if (
            account.state != "pending_mapping"
            or account.site_id is not None
            or account.enabled
            or account.last_verified_at is None
        ):
            raise _safe_error(
                "connection_not_pending", "Connection is not awaiting workspace mapping."
            )
        payload = self._decrypt_json(row["secret_blob"])
        credential = payload.get("credential")
        if not isinstance(credential, str) or not credential:
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        return credential

    def stage_managed(
        self,
        account: ConnectedAccount,
        credential: str,
        *,
        expected_version: int | None = None,
    ) -> ConnectedAccount:
        """Persist a verified pending credential without making it executable.

        A missing version is accepted only for an insert. Replacing an existing
        pending or revoked record requires its snapshot revision.
        """

        if not isinstance(account, ConnectedAccount):
            raise TypeError("connection must be a ConnectedAccount")
        if (
            account.workspace_id is None
            or account.state != "pending_mapping"
            or account.site_id is not None
            or account.enabled
            or account.last_verified_at is None
        ):
            raise ValueError("managed connections must be verified pending mappings")
        if not isinstance(credential, str) or not credential:
            raise ValueError("credential must be a non-empty string")
        if not account.id or not account.user_id or not account.toolkit:
            raise ValueError("connection id, user_id and toolkit are required")
        expected_version = self._expected_revision(expected_version)
        data = self._account_dump(account)
        data["enabled"] = False
        data["state"] = "pending_mapping"
        data["last_verified_at"] = _iso(account.last_verified_at)
        data["expires_at"] = _iso(account.expires_at)
        secret_blob = self._encrypt_json(
            {"credential": credential, "refresh_token": None, "provider": None}
        )
        now = self._now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                "SELECT * FROM accounts WHERE id = ?", (account.id,)
            ).fetchone()
            if existing is None:
                if expected_version is not None:
                    raise _safe_error(
                        "connection_changed", "Connection changed while it was being updated."
                    )
                self._db.execute(
                    """
                    INSERT INTO accounts(
                        id, user_id, toolkit, site_id, account_json, secret_blob, state,
                        enabled, created_at, updated_at, last_verified_at, expires_at,
                        last_error, workspace_id, managed_revision
                    ) VALUES (?, ?, ?, NULL, ?, ?, 'pending_mapping', 0, ?, ?, ?, ?, NULL, ?, 1)
                    """,
                    (
                        account.id,
                        account.user_id,
                        account.toolkit,
                        json.dumps(data, separators=(",", ":"), sort_keys=True),
                        secret_blob,
                        _iso(now),
                        _iso(now),
                        _iso(account.last_verified_at),
                        _iso(account.expires_at),
                        account.workspace_id,
                    ),
                )
            else:
                stored = self._view(existing)
                if (
                    existing["user_id"] != account.user_id
                    or existing["workspace_id"] != account.workspace_id
                ):
                    raise _safe_error(
                        "account_forbidden", "Connection is outside this user/workspace scope."
                    )
                revision = self._managed_revision(existing)
                if expected_version is None or expected_version != revision:
                    raise _safe_error(
                        "connection_changed", "Connection changed while it was being updated."
                    )
                if stored.toolkit != account.toolkit:
                    raise _safe_error("connection_conflict", "Connection identity cannot change.")
                if stored.state not in {"pending_mapping", "revoked"}:
                    raise _safe_error(
                        "connection_conflict", "An active or mapped connection cannot be restaged."
                    )
                updated = self._db.execute(
                    """
                    UPDATE accounts SET toolkit = ?, site_id = NULL, account_json = ?,
                        secret_blob = ?, state = 'pending_mapping', enabled = 0,
                        updated_at = ?, last_verified_at = ?, expires_at = ?, last_error = NULL,
                        managed_revision = managed_revision + 1
                    WHERE id = ? AND user_id = ? AND workspace_id = ? AND managed_revision = ?
                    """,
                    (
                        account.toolkit,
                        json.dumps(data, separators=(",", ":"), sort_keys=True),
                        secret_blob,
                        _iso(now),
                        _iso(account.last_verified_at),
                        _iso(account.expires_at),
                        account.id,
                        account.user_id,
                        account.workspace_id,
                        revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise _safe_error(
                        "connection_changed", "Connection changed while it was being updated."
                    )
            row = self._managed_row(account.user_id, account.workspace_id, account.id)
            view = self._view(row)
            self._managed_revision(row)
            self._db.commit()
            return view
        except BaseException:
            if self._db.in_transaction:
                self._db.rollback()
            raise

    def activate_managed(
        self,
        user_id: str,
        workspace_id: str,
        connection_id: str,
        *,
        site: Site,
        expected_version: int,
        verified_at: datetime,
    ) -> ConnectedAccount:
        """Atomically map and activate a pending managed account at an owned site."""

        if not isinstance(site, Site):
            raise TypeError("site must be a Site")
        if not site.id or site.user_id != user_id:
            raise _safe_error("account_forbidden", "Site is outside this user/workspace scope.")
        checked_version = self._expected_revision(expected_version)
        if checked_version is None:
            raise ValueError("expected_version is required")
        expected_version = checked_version
        if not isinstance(verified_at, datetime) or _as_utc(verified_at) is None:
            raise ValueError("verified_at must be an aware datetime")
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError("verified_at must be an aware datetime")

        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._managed_row(user_id, workspace_id, connection_id)
            account = self._view(row)
            revision = self._managed_revision(row)
            if account.state == "active":
                if account.site_id == site.id:
                    self._db.commit()
                    return account
                raise _safe_error(
                    "connection_conflict", "Connection is already mapped to another site."
                )
            if (
                account.state != "pending_mapping"
                or account.site_id is not None
                or account.enabled
                or account.last_verified_at is None
            ):
                raise _safe_error(
                    "connection_not_pending", "Connection is not awaiting workspace mapping."
                )
            if revision != expected_version:
                raise _safe_error(
                    "connection_changed", "Connection changed while it was being mapped."
                )

            now = self._now()
            account_data = json.loads(row["account_json"])
            account_data["site_id"] = site.id
            account_data["state"] = "active"
            account_data["enabled"] = True
            account_data["last_verified_at"] = _iso(verified_at)
            updated = self._db.execute(
                """
                UPDATE accounts SET site_id = ?, account_json = ?, state = 'active', enabled = 1,
                    updated_at = ?, last_verified_at = ?, managed_revision = managed_revision + 1
                WHERE id = ? AND user_id = ? AND workspace_id = ? AND managed_revision = ?
                """,
                (
                    site.id,
                    json.dumps(account_data, separators=(",", ":"), sort_keys=True),
                    _iso(now),
                    _iso(verified_at),
                    connection_id,
                    user_id,
                    workspace_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise _safe_error(
                    "connection_changed", "Connection changed while it was being mapped."
                )
            activated_row = self._managed_row(user_id, workspace_id, connection_id)
            activated = self._view(activated_row)
            self._managed_revision(activated_row)
            self._db.commit()
            return activated
        except BaseException:
            if self._db.in_transaction:
                self._db.rollback()
            raise

    def _credential_payload(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> tuple[sqlite3.Row, Json]:
        row = self._row(user_id, connection_id, site_id)
        if row["workspace_id"] is not None:
            self._view(row)
        if row["state"] == "pending":
            raise _safe_error("connection_pending", "Connection is awaiting OAuth authorization.")
        if row["state"] == "pending_mapping":
            raise _safe_error(
                "connection_pending", "Connection is pending and unavailable to runtime."
            )
        if not bool(row["enabled"]) or row["state"] in {"disabled", "revoked"}:
            raise _safe_error("connection_disabled", "Connection is disabled or revoked.")
        if row["expires_at"]:
            expires_at = _parse_datetime(row["expires_at"])
            if expires_at and expires_at <= self._now():
                raise _safe_error("credential_expired", "Connection credential has expired.")
        payload = self._decrypt_json(row["secret_blob"])
        credential = payload.get("credential")
        if not isinstance(credential, str) or not credential:
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        return row, payload

    def can_refresh(self, user_id: str, connection_id: str, site_id: str | None = None) -> bool:
        try:
            row, payload = self._credential_payload_allow_disabled(user_id, connection_id, site_id)
            return bool(
                row["enabled"]
                and row["state"] == "active"
                and payload.get("refresh_token")
                and payload.get("provider")
            )
        except EnergyError:
            return False

    def credential(self, user_id: str, connection_id: str, site_id: str | None = None) -> str:
        """Return a credential to trusted runtime code after scope checks."""

        _, payload = self._credential_payload(user_id, connection_id, site_id)
        credential = payload["credential"]
        if not isinstance(credential, str):
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        return credential

    def _update_state(
        self,
        user_id: str,
        connection_id: str,
        site_id: str | None,
        *,
        state: str,
        enabled: bool,
        remove_secret: bool = False,
    ) -> ConnectedAccount:
        row = self._row(user_id, connection_id, site_id)
        if row["workspace_id"] is not None:
            self._view(row)
        now = self._now()
        secret_blob = row["secret_blob"]
        account_data = json.loads(row["account_json"])
        account_data["enabled"] = enabled
        if "state" in _model_fields(ConnectedAccount):
            account_data["state"] = state
        if remove_secret:
            secret_blob = self._encrypt_json(
                {"credential": None, "refresh_token": None, "provider": None}
            )
        self._db.execute(
            "UPDATE accounts SET account_json = ?, state = ?, enabled = ?, secret_blob = ?, updated_at = ?, managed_revision = managed_revision + CASE WHEN workspace_id IS NULL THEN 0 ELSE 1 END WHERE id = ?",
            (
                json.dumps(account_data, separators=(",", ":"), sort_keys=True),
                state,
                int(enabled),
                secret_blob,
                _iso(now),
                connection_id,
            ),
        )
        self._db.commit()
        return self.get_account(user_id, connection_id, site_id)

    def disable(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> ConnectedAccount:
        self._row(user_id, connection_id, site_id)
        return self._update_state(user_id, connection_id, site_id, state="disabled", enabled=False)

    def revoke(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> ConnectedAccount:
        self._row(user_id, connection_id, site_id)
        return self._update_state(
            user_id, connection_id, site_id, state="revoked", enabled=False, remove_secret=True
        )

    def reconnect(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> ConnectedAccount:
        row, payload = self._credential_payload_allow_disabled(user_id, connection_id, site_id)
        if row["workspace_id"] is not None:
            raise _safe_error(
                "managed_lifecycle_required",
                "Managed connections must use workspace-scoped lifecycle operations.",
            )
        if row["state"] == "revoked":
            raise _safe_error("connection_revoked", "Revoked connections must be configured again.")
        if not isinstance(payload.get("credential"), str) or not payload["credential"]:
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        return self._update_state(user_id, connection_id, site_id, state="active", enabled=True)

    def _credential_payload_allow_disabled(
        self, user_id: str, connection_id: str, site_id: str | None
    ) -> tuple[sqlite3.Row, Json]:
        row = self._row(user_id, connection_id, site_id)
        if row["workspace_id"] is not None:
            self._view(row)
        payload = self._decrypt_json(row["secret_blob"])
        return row, payload

    def verify(
        self, user_id: str, connection_id: str, site_id: str | None = None
    ) -> ConnectedAccount:
        row, payload = self._credential_payload_allow_disabled(user_id, connection_id, site_id)
        if row["state"] == "revoked":
            raise _safe_error("connection_revoked", "Revoked connections must be configured again.")
        if row["state"] == "disabled" or not bool(row["enabled"]):
            raise _safe_error("connection_disabled", "Connection is disabled or revoked.")
        credential = payload.get("credential")
        if not isinstance(credential, str) or not credential:
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        return self._view(row)

    async def verify_provider(
        self,
        user_id: str,
        connection_id: str,
        probe: Callable[[ConnectedAccount, str], Awaitable[bool]],
        site_id: str | None = None,
    ) -> ConnectedAccount:
        """Run a trusted provider probe before recording verification time."""

        row, payload = self._credential_payload_allow_disabled(user_id, connection_id, site_id)
        if row["state"] == "revoked":
            raise _safe_error("connection_revoked", "Revoked connections must be configured again.")
        if row["state"] == "disabled" or not bool(row["enabled"]):
            raise _safe_error("connection_disabled", "Connection is disabled or revoked.")
        credential = payload.get("credential")
        if not isinstance(credential, str) or not credential:
            raise _safe_error("credential_missing", "Connection credential is unavailable.")
        try:
            verified = await probe(self._view(row), credential)
        except Exception as exc:
            raise _safe_error(
                "provider_verification_failed", "Provider verification failed."
            ) from exc
        if verified is not True:
            raise _safe_error("provider_verification_failed", "Provider verification failed.")
        now = self._now()
        account_data = json.loads(row["account_json"])
        if "last_verified_at" in _model_fields(ConnectedAccount):
            account_data["last_verified_at"] = _iso(now)
        where = (
            "WHERE id = ? AND user_id = ? AND updated_at = ? AND state = 'active' AND enabled = 1"
        )
        parameters: tuple[Any, ...] = (
            json.dumps(account_data, separators=(",", ":"), sort_keys=True),
            _iso(now),
            _iso(now),
            connection_id,
            user_id,
            row["updated_at"],
        )
        if row["workspace_id"] is not None:
            where += " AND workspace_id = ? AND managed_revision = ?"
            parameters += (row["workspace_id"], self._managed_revision(row))
        else:
            where += " AND workspace_id IS NULL"
        updated = self._db.execute(
            "UPDATE accounts SET account_json = ?, last_verified_at = ?, updated_at = ?, "
            "managed_revision = managed_revision + CASE WHEN workspace_id IS NULL THEN 0 ELSE 1 END "
            f"{where}",
            parameters,
        )
        if updated.rowcount != 1:
            self._db.rollback()
            raise _safe_error(
                "connection_changed", "Connection changed while provider verification was running."
            )
        self._db.commit()
        return self.get_account(user_id, connection_id, site_id)

    def redaction_values(self, user_id: str | None = None) -> list[str]:
        """Return stored secret values for the runtime's final redaction pass."""

        values: set[str] = set()
        rows = (
            self._db.execute(
                "SELECT secret_blob FROM accounts WHERE user_id = ?", (user_id,)
            ).fetchall()
            if user_id is not None
            else self._db.execute("SELECT secret_blob FROM accounts").fetchall()
        )
        for row in rows:
            try:
                payload = self._decrypt_json(row["secret_blob"])
            except EnergyError:
                continue
            for key in ("credential", "refresh_token"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    values.add(value)
            provider = payload.get("provider")
            if isinstance(provider, dict):
                value = provider.get("client_secret")
                if isinstance(value, str) and value:
                    values.add(value)
        return sorted(values)

    def _store_oauth_tokens(
        self,
        user_id: str,
        connection_id: str,
        site_id: str | None,
        *,
        payload: Json,
        expires_at: datetime | None,
        provider: OAuthProvider,
        expected_updated_at: str,
        expected_state: str,
        expected_enabled: bool,
    ) -> ConnectedAccount:
        row = self._row(user_id, connection_id, site_id)
        if row["workspace_id"] is not None:
            raise _safe_error(
                "managed_lifecycle_required",
                "Managed connections must use workspace-scoped lifecycle operations.",
            )
        now = self._now()
        row = self._db.execute(
            "SELECT account_json FROM accounts WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            raise _safe_error("connection_not_found", "Connection is not configured.")
        account_data = json.loads(row["account_json"])
        account_data["enabled"] = True
        if "state" in _model_fields(ConnectedAccount):
            account_data["state"] = "active"
        if "last_verified_at" in _model_fields(ConnectedAccount):
            account_data["last_verified_at"] = _iso(now)
        if "expires_at" in _model_fields(ConnectedAccount):
            account_data["expires_at"] = _iso(expires_at)
        updated = self._db.execute(
            """
            UPDATE accounts SET account_json = ?, secret_blob = ?, state = 'active', enabled = 1,
                updated_at = ?, last_verified_at = ?, expires_at = ?, last_error = NULL
            WHERE id = ? AND user_id = ? AND workspace_id IS NULL
                AND updated_at = ? AND state = ? AND enabled = ?
            """,
            (
                json.dumps(account_data, separators=(",", ":"), sort_keys=True),
                self._encrypt_json(
                    {
                        "credential": payload["access_token"],
                        "refresh_token": payload.get("refresh_token"),
                        "provider": self._provider_dump(provider),
                        "token_type": payload.get("token_type"),
                    }
                ),
                _iso(now),
                _iso(now),
                _iso(expires_at),
                connection_id,
                user_id,
                expected_updated_at,
                expected_state,
                int(expected_enabled),
            ),
        )
        if updated.rowcount != 1:
            self._db.rollback()
            raise _safe_error(
                "connection_changed", "Connection changed while the provider request was running."
            )
        self._db.commit()
        return self.get_account(user_id, connection_id, site_id)

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _authorization_url(self, provider: OAuthProvider, state: str, verifier: str) -> str:
        parsed = urlparse(provider.authorization_endpoint)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.update(
            {
                "response_type": "code",
                "client_id": provider.client_id,
                "redirect_uri": provider.redirect_uri,
                "state": state,
                "code_challenge": self._pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        if provider.scopes:
            params["scope"] = " ".join(provider.scopes)
        params.update(provider.extra_authorization_params)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def begin_oauth(
        self, user_id: str, connection_id: str, provider: OAuthProvider
    ) -> AuthorizationRequest:
        account = self.get_account(user_id, connection_id)
        if account.workspace_id is not None:
            raise _safe_error(
                "managed_lifecycle_required",
                "Managed connections must use workspace-scoped lifecycle operations.",
            )
        if account.auth.scheme != "oauth":
            raise _safe_error("oauth_not_configured", "Connection does not use OAuth.")
        if account.state == "revoked":
            raise _safe_error("connection_revoked", "Revoked connections must be configured again.")
        self._update_state(
            user_id,
            connection_id,
            account.site_id,
            state="pending",
            enabled=False,
            remove_secret=True,
        )
        account_row = self._row(user_id, connection_id, account.site_id)
        now = self._now()
        self._db.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (_iso(now),))
        pending_count = self._db.execute(
            "SELECT COUNT(*) FROM oauth_transactions WHERE user_id=? AND consumed_at IS NULL",
            (user_id,),
        ).fetchone()[0]
        if pending_count >= 100:
            self._db.commit()
            raise _safe_error(
                "oauth_rate_limited", "Too many pending authorizations; wait for expiry."
            )
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        expires_at = now + timedelta(seconds=provider.state_ttl_seconds)
        transaction = {
            "verifier": verifier,
            "provider": self._provider_dump(provider),
            "account_updated_at": account_row["updated_at"],
        }
        self._db.execute(
            "INSERT INTO oauth_transactions(state_hash, user_id, connection_id, redirect_uri, expires_at, transaction_blob) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _state_hash(state),
                user_id,
                connection_id,
                provider.redirect_uri,
                _iso(expires_at),
                self._encrypt_json(transaction),
            ),
        )
        self._db.commit()
        return AuthorizationRequest(
            connection_id=connection_id,
            authorization_url=self._authorization_url(provider, state, verifier),
            state=state,
            expires_at=expires_at,
        )

    def _take_oauth_transaction(
        self, user_id: str, state: str, redirect_uri: str
    ) -> _OAuthTransaction:
        if not isinstance(state, str) or not state or len(state) > 512:
            raise _safe_error("oauth_state_invalid", "OAuth state is invalid or expired.")
        row = self._db.execute(
            "SELECT * FROM oauth_transactions WHERE state_hash = ?",
            (_state_hash(state),),
        ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise _safe_error("oauth_state_invalid", "OAuth state is invalid or expired.")
        expires_at = _parse_datetime(row["expires_at"])
        if expires_at is None or expires_at <= self._now():
            raise _safe_error("oauth_state_expired", "OAuth state is invalid or expired.")
        if redirect_uri != row["redirect_uri"]:
            raise _safe_error("oauth_redirect_mismatch", "OAuth redirect URI does not match.")
        consumed = self._db.execute(
            "UPDATE oauth_transactions SET consumed_at = ? WHERE state_hash = ? AND consumed_at IS NULL",
            (_iso(self._now()), row["state_hash"]),
        )
        if consumed.rowcount != 1:
            self._db.commit()
            raise _safe_error("oauth_state_replayed", "OAuth state is invalid or expired.")
        self._db.commit()
        payload = self._decrypt_json(row["transaction_blob"])
        try:
            verifier = payload["verifier"]
            provider_data = payload["provider"]
            if not isinstance(verifier, str) or not isinstance(provider_data, dict):
                raise ValueError
            provider = self._provider_load(provider_data)
            account_updated_at = payload["account_updated_at"]
            if not isinstance(account_updated_at, str) or not account_updated_at:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise _safe_error("oauth_state_invalid", "OAuth state is invalid or expired.") from exc
        return _OAuthTransaction(
            user_id=user_id,
            connection_id=row["connection_id"],
            redirect_uri=redirect_uri,
            expires_at=expires_at,
            verifier=verifier,
            provider=provider,
            account_updated_at=account_updated_at,
        )

    @staticmethod
    async def _read_bounded_response(response: httpx.Response) -> tuple[int, bytes]:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_TOKEN_RESPONSE_BYTES:
                raise _safe_error("oauth_response_invalid", "OAuth provider response is too large.")
        return response.status_code, bytes(body)

    async def _token_request(
        self, provider: OAuthProvider, data: Mapping[str, str]
    ) -> Mapping[str, Any]:
        payload = dict(data)
        auth: httpx.BasicAuth | None = None
        if provider.token_endpoint_auth_method == "client_secret_basic":
            auth = httpx.BasicAuth(provider.client_id, provider.client_secret or "")
        elif provider.token_endpoint_auth_method == "client_secret_post":
            payload["client_secret"] = provider.client_secret or ""

        async def request(client: httpx.AsyncClient) -> tuple[int, bytes]:
            if auth is None:
                async with client.stream(
                    "POST",
                    provider.token_endpoint,
                    data=payload,
                    auth=None,
                    timeout=_HTTP_TIMEOUT,
                    follow_redirects=False,
                ) as response:
                    return await self._read_bounded_response(response)
            async with client.stream(
                "POST",
                provider.token_endpoint,
                data=payload,
                auth=auth,
                timeout=_HTTP_TIMEOUT,
                follow_redirects=False,
            ) as response:
                return await self._read_bounded_response(response)

        try:
            if self.http is None:
                async with httpx.AsyncClient(
                    timeout=_HTTP_TIMEOUT, follow_redirects=False
                ) as client:
                    status_code, content = await request(client)
            else:
                status_code, content = await request(self.http)
        except httpx.TimeoutException as exc:
            raise _safe_error(
                "oauth_timeout", "OAuth provider request timed out.", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise _safe_error(
                "oauth_request_failed", "OAuth provider request failed.", retryable=True
            ) from exc
        if status_code >= 400:
            raise _safe_error(
                "oauth_exchange_failed",
                "OAuth provider rejected the token request.",
                retryable=status_code == 429 or status_code >= 500,
            )
        try:
            result = json.loads(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned invalid JSON."
            ) from exc
        if not isinstance(result, Mapping):
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid response."
            )
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise _safe_error("oauth_response_invalid", "OAuth provider returned no access token.")
        refresh_token = result.get("refresh_token")
        if refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token):
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid refresh token."
            )
        token_type = result.get("token_type")
        if token_type is not None and (not isinstance(token_type, str) or not token_type):
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid token type."
            )
        return result

    @staticmethod
    def _expiry(result: Mapping[str, Any], now: datetime) -> datetime | None:
        expires_in = result.get("expires_in")
        if expires_in is None:
            return None
        if isinstance(expires_in, bool):
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid expiry."
            )
        try:
            seconds = float(expires_in)
        except (TypeError, ValueError) as exc:
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid expiry."
            ) from exc
        if not math.isfinite(seconds) or seconds < 0 or seconds > 365 * 24 * 60 * 60:
            raise _safe_error(
                "oauth_response_invalid", "OAuth provider returned an invalid expiry."
            )
        return now + timedelta(seconds=seconds)

    async def complete_oauth(
        self, user_id: str, state: str, code: str, redirect_uri: str
    ) -> ConnectedAccount:
        transaction = self._take_oauth_transaction(user_id, state, redirect_uri)
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise _safe_error("oauth_code_invalid", "OAuth authorization code is invalid.")
        result = await self._token_request(
            transaction.provider,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": transaction.provider.client_id,
                "code_verifier": transaction.verifier,
            },
        )
        expires_at = self._expiry(result, self._now())
        payload = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
        }
        return self._store_oauth_tokens(
            user_id,
            transaction.connection_id,
            None,
            payload=payload,
            expires_at=expires_at,
            provider=transaction.provider,
            expected_updated_at=transaction.account_updated_at,
            expected_state="pending",
            expected_enabled=False,
        )

    async def refresh(self, user_id: str, connection_id: str) -> ConnectedAccount:
        async with self._refresh_lock(connection_id):
            row, payload = self._credential_payload_allow_disabled(user_id, connection_id, None)
            if row["workspace_id"] is not None:
                raise _safe_error(
                    "managed_lifecycle_required",
                    "Managed connections must use workspace-scoped lifecycle operations.",
                )
            if row["state"] in {"disabled", "revoked"} or not bool(row["enabled"]):
                raise _safe_error("connection_disabled", "Connection is disabled or revoked.")
            provider_data = payload.get("provider")
            refresh_token = payload.get("refresh_token")
            if (
                not isinstance(provider_data, dict)
                or not isinstance(refresh_token, str)
                or not refresh_token
            ):
                raise _safe_error("refresh_unavailable", "No OAuth refresh token is configured.")
            provider = self._provider_load(provider_data)
            expires_at = _parse_datetime(row["expires_at"])
            if expires_at and expires_at > self._now() + timedelta(
                seconds=provider.refresh_skew_seconds
            ):
                return self._view(row)
            result = await self._token_request(
                provider,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": provider.client_id,
                },
            )
            new_payload = {
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token") or refresh_token,
            }
            return self._store_oauth_tokens(
                user_id,
                connection_id,
                row["site_id"],
                payload=new_payload,
                expires_at=self._expiry(result, self._now()),
                provider=provider,
                expected_updated_at=row["updated_at"],
                expected_state="active",
                expected_enabled=True,
            )
