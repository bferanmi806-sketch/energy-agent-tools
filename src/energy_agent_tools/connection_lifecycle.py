"""Scoped lifecycle operations for an already configured Octopus connection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import httpx

from .auth import AuthStore
from .models import ConnectedAccount, EnergyError, Json
from .onboarding import ConnectionHealth, probe_provider

_OCTOPUS_TOOLKIT = "octopus-energy-account"


class OctopusConnectionLifecycle:
    """Verify and disconnect Octopus accounts without owning injected clients."""

    def __init__(self, auth_store: AuthStore, http: httpx.AsyncClient) -> None:
        self.auth_store = auth_store
        self.http = http

    async def verify(self, *, user_id: str, site_id: str, connection_id: str) -> Json:
        self._scoped_octopus_account(user_id, site_id, connection_id)

        async def octopus_probe(checked_account: ConnectedAccount, credential: str) -> bool:
            # Recheck the toolkit passed by AuthStore in case the record changed
            # after the public scope lookup and before the request begins.
            if checked_account.toolkit != _OCTOPUS_TOOLKIT:
                raise EnergyError(
                    "unsupported_provider", "This connection provider is unsupported."
                )
            return await probe_provider(self.http, checked_account, credential)

        try:
            verified = await self.auth_store.verify_provider(
                user_id,
                connection_id,
                octopus_probe,
                site_id,
            )
        except EnergyError as error:
            if error.code != "provider_verification_failed":
                raise
            current = self.auth_store.get_account(user_id, connection_id, site_id)
            return self._health_response(
                current,
                status="unhealthy",
                checked_at=datetime.now(UTC),
                message="Provider verification failed.",
            )

        return self._health_response(
            verified,
            status="healthy",
            checked_at=verified.last_verified_at or datetime.now(UTC),
            message="Provider read succeeded.",
        )

    def disconnect(self, *, user_id: str, site_id: str, connection_id: str) -> Json:
        self._scoped_octopus_account(user_id, site_id, connection_id)
        account = self.auth_store.revoke(user_id, connection_id, site_id)
        return {"ok": True, "account": account.public()}

    def _scoped_octopus_account(
        self, user_id: str, site_id: str, connection_id: str
    ) -> ConnectedAccount:
        account = self.auth_store.get_account(user_id, connection_id, site_id)
        if account.toolkit != _OCTOPUS_TOOLKIT:
            raise EnergyError("unsupported_provider", "This connection provider is unsupported.")
        return account

    @staticmethod
    def _health_response(
        account: ConnectedAccount,
        *,
        status: Literal["healthy", "unhealthy"],
        checked_at: datetime,
        message: str,
    ) -> Json:
        health = ConnectionHealth(
            connection_id=account.id,
            provider="octopus",
            status=status,
            checked_at=checked_at.astimezone(UTC),
            probe="provider-read",
            message=message,
        )
        return {"ok": True, "account": account.public(), "health": health.public()}
