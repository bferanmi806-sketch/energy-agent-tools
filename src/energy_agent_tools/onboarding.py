"""Small, local-first connection onboarding for Energy Agent Tools.

The normal connection journey should not require a user to hand-edit a JSON
file.  :class:`LocalProfile` owns the few pieces of local state needed for
that journey:

* a Fernet key in ``<root>/vault.key``;
* an encrypted :class:`~energy_agent_tools.auth.AuthStore` in ``<root>/vault``;
* a non-secret ``profile.json`` containing sites and connection metadata.

The class is intentionally an SDK boundary.  A CLI can collect a token with
``getpass`` and pass it to ``connect``; this module never logs or prints the
credential.  Provider verification is a real read request made through the
injected ``httpx`` client.  A failed probe is reported as a public health
status and never echoes an authenticated URL or provider response.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet

from .auth import AuthStore
from .models import Asset, AuthConfig, ConnectedAccount, DataKind, EnergyError, Json, Site

ProviderName = Literal["octopus", "home_assistant", "emoncms"]

_PROFILE_FILE = "profile.json"
_KEY_FILE = "vault.key"
_OCTOPUS_BASE = "https://api.octopus.energy/v1"
_OCTOPUS_TOOLKIT = "octopus-energy-account"
_HA_TOOLKIT = "home-assistant"
_EMON_TOOLKIT = "openenergymonitor"
_PROVIDERS: dict[str, ProviderName] = {
    "octopus": "octopus",
    "octopus_energy": "octopus",
    "octopus-energy": "octopus",
    "home_assistant": "home_assistant",
    "home-assistant": "home_assistant",
    "ha": "home_assistant",
    "emoncms": "emoncms",
    "openenergymonitor": "emoncms",
    "open-energy-monitor": "emoncms",
}
_MAX_PROBE_BYTES = 64 * 1024
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password")


def _safe_error(code: str, message: str, retryable: bool = False) -> EnergyError:
    """Return an error whose text contains no provider data."""

    return EnergyError(code, message, retryable)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require an explicit UTC offset")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _slug(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    if not normalized or len(normalized) > 80:
        raise ValueError(f"{field} must contain a short identifier")
    return normalized


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", value):
        raise ValueError(f"{field} is invalid")
    return value


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES):
                raise ValueError("metadata cannot contain credential fields")
            _reject_secret_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_fields(nested)


def _json_object(value: Mapping[str, Any] | None, field: str) -> Json:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    _reject_secret_fields(value)
    try:
        encoded = json.dumps(dict(value), separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain JSON-safe values") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{field} must be an object")
    return decoded


def _base_url(value: Any, *, required: bool, field: str = "base_url") -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    try:
        parsed = httpx.URL(value.strip())
    except Exception as exc:
        raise ValueError(f"{field} is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} is invalid")
    # Local development providers are useful in fixtures and self-hosted
    # installations.  Remote HTTP endpoints would expose bearer credentials.
    if parsed.scheme == "http" and parsed.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{field} must use HTTPS")
    return str(parsed).rstrip("/")


def _same_origin(candidate: httpx.URL, expected: str) -> bool:
    try:
        origin = httpx.URL(expected)
    except Exception:
        return False
    return (
        candidate.scheme.lower(),
        (candidate.host or "").lower(),
        candidate.port,
    ) == (
        origin.scheme.lower(),
        (origin.host or "").lower(),
        origin.port,
    )


def _provider(value: str) -> ProviderName:
    if not isinstance(value, str):
        raise ValueError("provider is required")
    normalized = value.strip().lower().replace(" ", "_")
    try:
        return _PROVIDERS[normalized]
    except KeyError as exc:
        raise ValueError("provider must be octopus, home_assistant or emoncms") from exc


def _provider_public_name(provider: ProviderName) -> str:
    return {
        "octopus": "Octopus Energy",
        "home_assistant": "Home Assistant",
        "emoncms": "OpenEnergyMonitor",
    }[provider]


def _provider_toolkit(provider: ProviderName) -> str:
    return {
        "octopus": _OCTOPUS_TOOLKIT,
        "home_assistant": _HA_TOOLKIT,
        "emoncms": _EMON_TOOLKIT,
    }[provider]


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """Public result of a provider read probe."""

    connection_id: str
    provider: str
    status: Literal["healthy", "unhealthy"]
    checked_at: datetime
    probe: str
    message: str

    def public(self) -> Json:
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "status": self.status,
            "checked_at": _iso(self.checked_at),
            "probe": self.probe,
            "message": self.message,
        }


class LocalProfile:
    """Create and use a persistent local Energy Agent Tools profile.

    ``credential`` is deliberately accepted only by the SDK method.  It is
    written to the encrypted vault and is never included in profile metadata,
    returned mappings, exception text, or health responses.
    """

    def __init__(
        self,
        root: Path,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod(self.root, 0o700)
        self.key_path = self.root / _KEY_FILE
        self.profile_path = self.root / _PROFILE_FILE
        self.vault_path = self.root / "vault"
        self._clock = clock or _now_utc
        self._closed = False
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._key = self._ensure_key()
        self.auth_store = AuthStore(self.vault_path, self._key, http=self.http, clock=self._clock)
        self._profile = self._load_profile()

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            # Encrypted blobs still protect credentials on platforms without
            # POSIX modes; operators should apply equivalent ACLs there.
            pass

    def _atomic_write(self, path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._chmod(temporary, mode)
            os.replace(temporary, path)
            self._chmod(path, mode)
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _ensure_key(self) -> bytes:
        if self.key_path.exists():
            key = self.key_path.read_bytes()
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise ValueError("vault.key does not contain a valid Fernet key") from exc
            self._chmod(self.key_path, 0o600)
            return key
        key = Fernet.generate_key()
        self._atomic_write(self.key_path, key, 0o600)
        return key

    def _empty_profile(self) -> Json:
        return {
            "user_id": "local",
            "sites": [],
            "assets": [],
            "accounts": [],
            "vault": {"master_key_file": _KEY_FILE},
        }

    def _load_profile(self) -> Json:
        if not self.profile_path.exists():
            profile = self._empty_profile()
            self._write_profile(profile)
            return profile
        try:
            raw = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("profile.json is invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError("profile.json must contain an object")
        try:
            profile = self._empty_profile()
            profile.update(
                {
                    "user_id": raw.get("user_id", "local"),
                    "sites": raw.get("sites", []),
                    "assets": raw.get("assets", []),
                    "accounts": raw.get("accounts", []),
                    "vault": raw.get("vault", {"master_key_file": _KEY_FILE}),
                }
            )
            if not isinstance(profile["user_id"], str) or not profile["user_id"]:
                raise ValueError
            profile["sites"] = [
                Site.model_validate(item).model_dump(mode="json") for item in profile["sites"]
            ]
            profile["assets"] = [
                Asset.model_validate(item).model_dump(mode="json") for item in profile["assets"]
            ]
            profile["accounts"] = [
                ConnectedAccount.model_validate(item).model_dump(mode="json")
                for item in profile["accounts"]
            ]
            vault = profile["vault"]
            if not isinstance(vault, dict) or vault.get("master_key_file") != _KEY_FILE:
                raise ValueError
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("profile.json contains invalid local configuration") from exc
        self._write_profile(profile)
        return profile

    def _write_profile(self, profile: Json | None = None) -> None:
        data = profile if profile is not None else self._profile
        # Revalidate before every write so a future caller cannot turn the
        # human-readable profile into an accidental secret store.
        sites = [
            Site.model_validate(item).model_dump(mode="json") for item in data.get("sites", [])
        ]
        assets = [
            Asset.model_validate(item).model_dump(mode="json") for item in data.get("assets", [])
        ]
        accounts = [
            ConnectedAccount.model_validate(item).model_dump(mode="json")
            for item in data.get("accounts", [])
        ]
        output = {
            "user_id": data.get("user_id", "local"),
            "sites": sites,
            "assets": assets,
            "accounts": accounts,
            "vault": {"master_key_file": _KEY_FILE},
        }
        encoded = (json.dumps(output, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._atomic_write(self.profile_path, encoded, 0o600)

    def _sync_accounts(self) -> None:
        by_id = {
            str(item["id"]): item
            for item in self._profile.get("accounts", [])
            if isinstance(item, dict)
        }
        user_ids = {str(item.get("user_id")) for item in by_id.values() if item.get("user_id")}
        user_ids.add(str(self._profile.get("user_id", "local")))
        for user_id in user_ids:
            for account in self.auth_store.accounts(user_id):
                by_id[account.id] = account.model_dump(mode="json")
        self._profile["accounts"] = sorted(by_id.values(), key=lambda item: str(item.get("id", "")))
        self._write_profile()

    def config(self) -> Json:
        """Return the non-secret object accepted by the extended ``build_agent``."""

        self._sync_accounts()
        return json.loads(json.dumps(self._profile))

    @property
    def profile(self) -> Json:
        """Return a defensive copy of the current non-secret profile."""

        return self.config()

    def sites(self, user_id: str | None = None) -> list[Json]:
        selected = self._profile.get("sites", [])
        if user_id is not None:
            selected = [item for item in selected if item.get("user_id") == user_id]
        return json.loads(json.dumps(selected))

    def connections(self, user_id: str | None = None, site_id: str | None = None) -> list[Json]:
        self._sync_accounts()
        selected = self._profile.get("accounts", [])
        if user_id is not None:
            selected = [item for item in selected if item.get("user_id") == user_id]
        if site_id is not None:
            selected = [item for item in selected if item.get("site_id") == site_id]
        return [ConnectedAccount.model_validate(item).public() for item in selected]

    def create_site(
        self,
        name: str,
        timezone: str,
        *,
        user_id: str = "local",
        site_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> Site:
        """Create a site and persist it atomically in ``profile.json``."""

        user_id = _identifier(user_id, "user_id")
        identifier = _identifier(site_id, "site_id") if site_id else _slug(name, "site_id")
        site = Site(
            id=identifier,
            user_id=user_id,
            name=name,
            timezone=timezone,
            latitude=latitude,
            longitude=longitude,
        )
        existing = next((item for item in self._profile["sites"] if item["id"] == site.id), None)
        dumped = site.model_dump(mode="json")
        if existing is not None:
            if existing != dumped:
                raise _safe_error("site_conflict", "A site with this ID already exists.")
            return site
        self._profile["sites"].append(dumped)
        self._write_profile()
        return site

    def create_asset(
        self,
        site_id: str,
        kind: str,
        name: str,
        *,
        asset_id: str | None = None,
        account_ids: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Asset:
        """Create an optional site asset without putting credentials in metadata."""

        if not any(item["id"] == site_id for item in self._profile["sites"]):
            raise _safe_error("site_not_found", "Create the site before adding an asset.")
        identifier = _identifier(asset_id, "asset_id") if asset_id else _slug(name, "asset_id")
        safe_metadata = _json_object(metadata, "metadata")
        asset = Asset(
            id=identifier,
            site_id=site_id,
            kind=kind,
            name=name,
            metadata=safe_metadata,
            account_ids=account_ids or [],
            parent_id=parent_id,
        )
        dumped = asset.model_dump(mode="json")
        existing = next((item for item in self._profile["assets"] if item["id"] == asset.id), None)
        if existing is not None:
            if existing != dumped:
                raise _safe_error("asset_conflict", "An asset with this ID already exists.")
            return asset
        self._profile["assets"].append(dumped)
        self._write_profile()
        return asset

    def _settings(self, provider: ProviderName, metadata: Mapping[str, Any]) -> Json:
        settings = _json_object(metadata, "metadata")
        if provider == "octopus":
            _identifier(settings.get("mpan"), "mpan")
            _identifier(settings.get("serial_number"), "serial_number")
            if settings.get("base_url") is not None:
                settings["base_url"] = _base_url(settings["base_url"], required=True)
        elif provider == "home_assistant":
            settings["base_url"] = _base_url(settings.get("base_url"), required=True)
            entity = settings.get("entity_id")
            if entity is not None:
                if (
                    not isinstance(entity, str)
                    or not entity
                    or any(char in entity for char in "/?#")
                ):
                    raise ValueError("entity_id is invalid")
        else:
            settings["base_url"] = _base_url(settings.get("base_url"), required=True)
            feed_id = settings.get("feed_id")
            if isinstance(feed_id, bool) or not isinstance(feed_id, (int, str)):
                raise ValueError("feed_id is required")
            if not re.fullmatch(r"[1-9][0-9]*", str(feed_id)):
                raise ValueError("feed_id is invalid")
            settings["feed_id"] = int(feed_id)
        return settings

    @staticmethod
    def _reviewed_bindings(provider: ProviderName, settings: Json) -> list[Json]:
        """Create mappings only where the operator supplied explicit semantics.

        A Home Assistant ``total_increasing`` sensor is commonly a cumulative
        counter.  The raw state is therefore never advertised as interval
        consumption by this onboarding layer.  The user must explicitly say
        that the entity represents interval energy before a consumption
        binding is emitted.
        """

        if provider == "octopus":
            return [
                {
                    "capability": "get_energy_consumption",
                    "tool": "octopus_energy.get_consumption",
                    "kind": DataKind.METERED.value,
                    "unit": "kWh",
                    "reviewed": True,
                    "quality": "verified",
                    "version": "1.0.0",
                }
            ]
        role = settings.get("telemetry_role")
        measurement_kind = settings.get("measurement_kind")
        unit = settings.get("unit")
        quantity_shape = settings.get("quantity_shape")
        entity_or_feed = (
            settings.get("entity_id") if provider == "home_assistant" else settings.get("feed_id")
        )
        if (
            not isinstance(role, str)
            or not isinstance(measurement_kind, str)
            or not isinstance(unit, str)
        ):
            return []
        if entity_or_feed is None:
            return []
        if measurement_kind != DataKind.METERED.value:
            return []
        if role == "consumption_interval":
            if (
                unit != "kWh"
                or (quantity_shape is not None and quantity_shape != "interval")
                or (
                    quantity_shape != "interval"
                    and settings.get("measurement_semantics") != "interval_energy"
                )
            ):
                return []
            capability = "get_energy_consumption"
        elif (
            role == "current_power"
            and unit in {"W", "kW", "MW"}
            and quantity_shape == "instantaneous"
        ):
            capability = "get_current_power"
        elif (
            role == "generation"
            and unit in {"W", "kW", "MW", "kWh"}
            and quantity_shape in {"instantaneous", "interval"}
        ):
            capability = "get_generation"
        else:
            return []
        tool = (
            "home_assistant.get_history"
            if provider == "home_assistant"
            else "openenergymonitor.get_feed"
        )
        fixed_key = "entity_id" if provider == "home_assistant" else "feed_id"
        return [
            {
                "capability": capability,
                "tool": tool,
                "kind": measurement_kind,
                "unit": unit,
                "fixed_arguments": {fixed_key: entity_or_feed},
                "reviewed": True,
                "quality": "operator-reviewed",
                "version": "1.0.0",
            }
        ]

    def configure_connection(
        self,
        provider: str,
        *,
        credential: str,
        user_id: str = "local",
        site_id: str | None = None,
        connection_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ConnectedAccount:
        """Store a provider credential and non-secret connection metadata.

        This method performs no output and no logging.  Call :meth:`connect`
        to store and verify in one operation.
        """

        selected_provider = _provider(provider)
        user_id = _identifier(user_id, "user_id")
        if site_id is not None:
            site_id = _identifier(site_id, "site_id")
            if not any(
                item["id"] == site_id and item["user_id"] == user_id
                for item in self._profile["sites"]
            ):
                raise _safe_error(
                    "site_not_found", "Connection site is not configured for this user."
                )
        if not isinstance(credential, str) or not credential:
            raise ValueError("credential must be a non-empty string")
        settings = self._settings(selected_provider, metadata or {})
        bindings = self._reviewed_bindings(selected_provider, settings)
        if bindings:
            settings["capability_bindings"] = bindings
        default_id = f"{selected_provider}-{site_id or user_id}"
        account_id = _identifier(connection_id, "connection_id") if connection_id else default_id
        account = ConnectedAccount(
            id=account_id,
            user_id=user_id,
            toolkit=_provider_toolkit(selected_provider),
            site_id=site_id,
            auth=AuthConfig(
                scheme=cast(
                    Literal["api-key", "bearer", "basic"],
                    {"octopus": "basic", "home_assistant": "bearer", "emoncms": "api-key"}[
                        selected_provider
                    ],
                )
            ),
            settings=settings,
        )
        self.auth_store.configure(account, credential)
        stored = self.auth_store.get_account(user_id, account.id, site_id)
        self._upsert_account(stored)
        return stored

    def _upsert_account(self, account: ConnectedAccount) -> None:
        dumped = account.model_dump(mode="json")
        self._profile["accounts"] = [
            item for item in self._profile["accounts"] if item.get("id") != account.id
        ]
        self._profile["accounts"].append(dumped)
        self._profile["accounts"].sort(key=lambda item: str(item.get("id", "")))
        self._write_profile()

    async def _probe(self, account: ConnectedAccount, credential: str) -> bool:
        provider = {
            _OCTOPUS_TOOLKIT: "octopus",
            _HA_TOOLKIT: "home_assistant",
            _EMON_TOOLKIT: "emoncms",
        }.get(account.toolkit)
        if provider is None:
            raise _safe_error("unsupported_provider", "This connection provider is unsupported.")
        settings = account.settings
        try:
            if provider == "octopus":
                base = _base_url(settings.get("base_url"), required=False) or _OCTOPUS_BASE
                assert base is not None
                expected_origin = base
                mpan = _identifier(settings.get("mpan"), "mpan")
                serial = _identifier(settings.get("serial_number"), "serial_number")
                endpoint = (
                    f"{base}/electricity-meter-points/{quote(mpan, safe='-._~')}"
                    f"/meters/{quote(serial, safe='-._~')}/consumption/"
                )
                response = await self.http.request(
                    "GET",
                    endpoint,
                    auth=httpx.BasicAuth(credential, ""),
                    follow_redirects=False,
                )
            elif provider == "home_assistant":
                ha_base = _base_url(settings.get("base_url"), required=True)
                assert ha_base is not None
                expected_origin = ha_base
                entity = settings.get("entity_id")
                if isinstance(entity, str) and entity:
                    path = f"/api/states/{quote(entity, safe='._:-')}"
                else:
                    path = "/api/config"
                response = await self.http.request(
                    "GET",
                    f"{ha_base}{path}",
                    headers={"Authorization": f"Bearer {credential}"},
                    follow_redirects=False,
                )
            else:
                emon_base = _base_url(settings.get("base_url"), required=True)
                assert emon_base is not None
                expected_origin = emon_base
                feed_id = settings.get("feed_id")
                response = await self.http.request(
                    "GET",
                    f"{emon_base}/feed/value.json",
                    params={"id": str(feed_id), "apikey": credential},
                    follow_redirects=False,
                )
        except httpx.RequestError as exc:
            raise _safe_error(
                "provider_unavailable", "Provider could not be reached.", True
            ) from exc
        if 300 <= response.status_code < 400:
            raise _safe_error("provider_verification_failed", "Provider verification failed.")
        if response.status_code in {401, 403}:
            raise _safe_error("provider_verification_failed", "Provider verification failed.")
        if response.status_code >= 400:
            raise _safe_error("provider_verification_failed", "Provider verification failed.")
        if not _same_origin(response.url, expected_origin):
            raise _safe_error("provider_verification_failed", "Provider verification failed.")
        if len(response.content) > _MAX_PROBE_BYTES:
            raise _safe_error("provider_verification_failed", "Provider returned too much data.")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise _safe_error(
                "provider_verification_failed", "Provider returned invalid data."
            ) from exc
        if provider == "octopus" and not isinstance(payload, Mapping):
            raise _safe_error("provider_verification_failed", "Provider returned invalid data.")
        if provider == "home_assistant" and not isinstance(payload, Mapping):
            raise _safe_error("provider_verification_failed", "Provider returned invalid data.")
        if provider == "emoncms" and isinstance(payload, (list, tuple)):
            raise _safe_error("provider_verification_failed", "Provider returned invalid data.")
        return True

    async def verify_connection(
        self,
        connection_id: str,
        *,
        user_id: str = "local",
        site_id: str | None = None,
    ) -> Json:
        """Run a provider read probe and return a safe health status."""

        account = self.auth_store.get_account(user_id, connection_id, site_id)
        provider = {
            _OCTOPUS_TOOLKIT: "octopus",
            _HA_TOOLKIT: "home_assistant",
            _EMON_TOOLKIT: "emoncms",
        }.get(account.toolkit, account.toolkit)
        try:
            verified = await self.auth_store.verify_provider(
                user_id,
                connection_id,
                self._probe,
                site_id,
            )
        except EnergyError:
            self._sync_accounts()
            return ConnectionHealth(
                connection_id=connection_id,
                provider=provider,
                status="unhealthy",
                checked_at=_as_utc(self._clock()),
                probe="provider-read",
                message="Provider verification failed.",
            ).public()
        self._upsert_account(verified)
        return ConnectionHealth(
            connection_id=connection_id,
            provider=provider,
            status="healthy",
            checked_at=_as_utc(self._clock()),
            probe="provider-read",
            message="Provider read succeeded.",
        ).public()

    async def connect(
        self,
        provider: str,
        *,
        credential: str,
        user_id: str = "local",
        site_id: str | None = None,
        connection_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Json:
        """Store, verify and return the complete safe onboarding result."""

        account = self.configure_connection(
            provider,
            credential=credential,
            user_id=user_id,
            site_id=site_id,
            connection_id=connection_id,
            metadata=metadata,
        )
        health = await self.verify_connection(
            account.id,
            user_id=user_id,
            site_id=site_id,
        )
        return {
            "ok": health["status"] == "healthy",
            "account": account.public(),
            "health": health,
            "config": self.config(),
        }

    def close(self) -> None:
        """Close the encrypted store; use :meth:`aclose` for owned HTTP."""

        if self._closed:
            return
        self._closed = True
        self.auth_store.close()

    async def aclose(self) -> None:
        if self._closed:
            return
        self.close()
        if self._owns_http:
            await self.http.aclose()

    def __enter__(self) -> LocalProfile:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    async def __aenter__(self) -> LocalProfile:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()


__all__ = ["ConnectionHealth", "LocalProfile", "ProviderName"]
