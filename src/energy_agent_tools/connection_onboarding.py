"""Provider connection services built on the shared encrypted auth store."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime

import httpx

from .auth import AuthStore
from .models import AuthConfig, ConnectedAccount, EnergyError, Json, Site
from .onboarding import (
    ConnectionHealth,
    probe_provider,
    provider_settings,
    reviewed_provider_bindings,
)


class OctopusConnectionService:
    """Verify and save Octopus credentials without owning the supplied clients."""

    def __init__(self, auth_store: AuthStore, http: httpx.AsyncClient) -> None:
        self.auth_store = auth_store
        self.http = http

    async def stage_managed(
        self,
        *,
        user_id: str,
        workspace_id: str,
        credential: str,
        mpan: str,
        serial_number: str,
    ) -> Json:
        if not self._valid_credential(credential):
            raise EnergyError("credential_invalid", "Credential is invalid.")
        try:
            settings = provider_settings("octopus", {"mpan": mpan, "serial_number": serial_number})
        except (TypeError, ValueError):
            raise EnergyError(
                "connection_settings_invalid", "Octopus meter details are invalid."
            ) from None
        bindings = reviewed_provider_bindings("octopus", settings)
        if bindings:
            settings["capability_bindings"] = bindings
        identity = json.dumps([user_id, workspace_id, mpan, serial_number], separators=(",", ":"))
        account_id = "managed-octopus-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        expected_version = None
        try:
            existing, expected_version = self.auth_store.managed_snapshot(
                user_id, workspace_id, account_id
            )
        except EnergyError as exc:
            if exc.code != "connection_not_found":
                raise
        else:
            if existing.state == "active":
                raise EnergyError(
                    "connection_conflict", "Disconnect the existing connection before reconnecting."
                )
        probe_account = ConnectedAccount(
            id=account_id,
            user_id=user_id,
            toolkit="octopus-energy-account",
            auth=AuthConfig(scheme="basic"),
            settings=settings,
        )
        try:
            await probe_provider(self.http, probe_account, credential)
        except Exception:
            raise EnergyError(
                "provider_verification_failed", "Provider verification failed."
            ) from None
        verified_at = datetime.now(UTC)
        pending = ConnectedAccount(
            **{
                **probe_account.model_dump(),
                "workspace_id": workspace_id,
                "state": "pending_mapping",
                "enabled": False,
                "last_verified_at": verified_at,
            }
        )
        stored = self.auth_store.stage_managed(
            pending, credential, expected_version=expected_version
        )
        return self._managed_outcome(stored, verified_at)

    async def map_managed(
        self,
        *,
        user_id: str,
        workspace_id: str,
        connection_id: str,
        site: Site,
    ) -> Json:
        if site.user_id != user_id:
            raise EnergyError(
                "connection_site_forbidden", "Connection site is outside this user scope."
            )
        account, version = self.auth_store.managed_snapshot(user_id, workspace_id, connection_id)
        if account.state == "active":
            if account.site_id != site.id:
                raise EnergyError(
                    "connection_conflict", "Connection is already mapped to another site."
                )
            return self._managed_outcome(account, account.last_verified_at or datetime.now(UTC))
        credential = self.auth_store.pending_credential(user_id, workspace_id, connection_id)
        try:
            await probe_provider(self.http, account, credential)
        except Exception:
            raise EnergyError(
                "provider_verification_failed", "Provider verification failed."
            ) from None
        verified_at = datetime.now(UTC)
        stored = self.auth_store.activate_managed(
            user_id,
            workspace_id,
            connection_id,
            site=site,
            expected_version=version,
            verified_at=verified_at,
        )
        return self._managed_outcome(stored, verified_at)

    @staticmethod
    def _managed_outcome(account: ConnectedAccount, verified_at: datetime) -> Json:
        health = ConnectionHealth(
            connection_id=account.id,
            provider="octopus",
            status="healthy",
            checked_at=verified_at,
            probe="provider-read",
            message="Provider read succeeded.",
        )
        return {"ok": True, "account": account.public(), "health": health.public()}

    async def connect(
        self,
        *,
        user_id: str,
        site: Site,
        credential: str,
        mpan: str,
        serial_number: str,
    ) -> Json:
        """Probe Octopus before persisting an encrypted credential."""

        if site.user_id != user_id:
            raise EnergyError(
                "connection_site_forbidden", "Connection site is outside this user scope."
            )
        if not self._valid_credential(credential):
            raise EnergyError(
                "credential_invalid",
                "Credential must be 1 to 4096 characters with no whitespace or controls.",
            )
        try:
            settings = provider_settings("octopus", {"mpan": mpan, "serial_number": serial_number})
        except (TypeError, ValueError):
            raise EnergyError(
                "connection_settings_invalid", "Octopus meter details are invalid."
            ) from None

        bindings = reviewed_provider_bindings("octopus", settings)
        if bindings:
            settings["capability_bindings"] = bindings
        identity = json.dumps([user_id, site.id, mpan, serial_number], separators=(",", ":"))
        account_id = "octopus-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        account = ConnectedAccount(
            id=account_id,
            user_id=user_id,
            toolkit="octopus-energy-account",
            site_id=site.id,
            auth=AuthConfig(scheme="basic"),
            settings=settings,
        )
        try:
            await probe_provider(self.http, account, credential)
        except Exception:
            raise EnergyError(
                "provider_verification_failed", "Provider verification failed."
            ) from None

        verified_at = datetime.now(UTC)
        account = account.model_copy(update={"last_verified_at": verified_at})
        try:
            self.auth_store.configure(account, credential)
            stored = self.auth_store.get_account(user_id, account.id, site.id)
        except Exception:
            raise EnergyError(
                "connection_storage_failed", "Connection could not be saved."
            ) from None

        health = ConnectionHealth(
            connection_id=stored.id,
            provider="octopus",
            status="healthy",
            checked_at=verified_at,
            probe="provider-read",
            message="Provider read succeeded.",
        )
        return {"ok": True, "account": stored.public(), "health": health.public()}

    @staticmethod
    def _valid_credential(value: object) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 4096
            and all(
                not character.isspace() and unicodedata.category(character) not in {"Cc", "Cf"}
                for character in value
            )
        )
