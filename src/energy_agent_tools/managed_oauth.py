"""Approved native OAuth configurations and workspace connection enrollment."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from urllib.parse import SplitResult, urlsplit

import httpx
from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from .auth import AuthorizationRequest, AuthStore, OAuthProvider
from .models import AuthConfig, ConnectedAccount, EnergyError, Json, StrictModel
from .onboarding import probe_provider, provider_settings, reviewed_provider_bindings


class HomeAssistantOAuthConfiguration(StrictModel):
    """A deployment-approved instance and callback, never a manager URL input."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: StrictStr = Field(min_length=1, max_length=120)
    base_url: StrictStr = Field(min_length=1, max_length=2048)
    client_id: StrictStr = Field(min_length=1, max_length=2048)
    redirect_uri: StrictStr = Field(min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("A bounded display name is required")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def approved_base(cls, value: str) -> str:
        normalized = str(provider_settings("home_assistant", {"base_url": value})["base_url"])
        if urlsplit(normalized).path:
            raise ValueError("Home Assistant authorization requires an instance origin")
        return normalized

    @field_validator("client_id", "redirect_uri")
    @classmethod
    def valid_application_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Use an exact application URL without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("External application URLs require HTTPS")
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def same_application_origin(self) -> HomeAssistantOAuthConfiguration:
        client, redirect = urlsplit(self.client_id), urlsplit(self.redirect_uri)

        def origin(parsed: SplitResult) -> tuple[str, str | None, int]:
            return (
                parsed.scheme,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )

        if origin(client) != origin(redirect):
            raise ValueError("Home Assistant callback must share the client application's origin")
        return self

    def fingerprint(self) -> str:
        value = json.dumps(
            [self.base_url, self.client_id, self.redirect_uri, "home_assistant"],
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def provider(self) -> OAuthProvider:
        return OAuthProvider(
            authorization_endpoint=f"{self.base_url}/auth/authorize",
            token_endpoint=f"{self.base_url}/auth/token",
            revocation_endpoint=f"{self.base_url}/auth/revoke",
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            protocol="home_assistant",
        )

    def public(self) -> Json:
        return {
            "id": self.id,
            "name": self.name,
            "toolkit": "home-assistant",
            "protocol": "home_assistant",
        }


class ManagedHomeAssistantOAuth:
    """Select approved destinations and verify grants before publishing accounts."""

    def __init__(
        self,
        store: AuthStore,
        http: httpx.AsyncClient,
        configurations: Iterable[HomeAssistantOAuthConfiguration],
    ) -> None:
        self.store = store
        self.http = http
        self.configurations: dict[str, HomeAssistantOAuthConfiguration] = {}
        for configuration in configurations:
            if configuration.id in self.configurations:
                raise ValueError("OAuth configuration IDs must be unique")
            self.configurations[configuration.id] = configuration

    def _configuration(self, configuration_id: str) -> HomeAssistantOAuthConfiguration:
        configuration = self.configurations.get(configuration_id)
        if configuration is None:
            raise EnergyError(
                "oauth_configuration_unavailable", "Select an approved configuration."
            )
        return configuration

    async def cleanup(
        self, *, user_id: str, workspace_id: str, configuration_id: str
    ) -> dict[str, int]:
        configuration = self._configuration(configuration_id)
        return await self.store.retry_managed_oauth_cleanup(
            user_id,
            workspace_id,
            configuration_id=configuration.id,
            provider=configuration.provider(),
            limit=1,
        )

    def begin(
        self,
        *,
        user_id: str,
        workspace_id: str,
        configuration_id: str,
        entity_id: str,
        telemetry: Json | None = None,
    ) -> AuthorizationRequest:
        configuration = self._configuration(configuration_id)
        settings = provider_settings(
            "home_assistant", {"base_url": configuration.base_url, "entity_id": entity_id}
        )
        if (
            not entity_id
            or len(entity_id) > 256
            or any(character.isspace() for character in entity_id)
        ):
            raise EnergyError("connection_settings_invalid", "Select a bounded entity identifier.")
        if telemetry is not None:
            allowed = {"telemetry_role", "unit", "quantity_shape", "measurement_kind"}
            if set(telemetry) - allowed:
                raise EnergyError("connection_settings_invalid", "Telemetry mapping is invalid.")
            settings.update(telemetry)
            bindings = reviewed_provider_bindings("home_assistant", settings)
            if not bindings:
                raise EnergyError(
                    "connection_settings_invalid", "Telemetry mapping is unsupported."
                )
            settings["capability_bindings"] = bindings
        settings["managed_oauth_configuration_id"] = configuration.id
        settings["managed_oauth_configuration_digest"] = configuration.fingerprint()
        identity = json.dumps(
            [user_id, workspace_id, configuration.id, entity_id], separators=(",", ":")
        )
        account = ConnectedAccount(
            id="managed-ha-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            user_id=user_id,
            toolkit="home-assistant",
            display_name=f"{configuration.name}: {entity_id}",
            auth=AuthConfig(scheme="oauth"),
            settings=settings,
        )
        return self.store.begin_managed_oauth(
            user_id, workspace_id, account, configuration.provider()
        )

    async def complete(
        self,
        *,
        user_id: str,
        workspace_id: str,
        configuration_id: str,
        state: str,
        code: str,
        authorize_write: Callable[[], None] | None = None,
    ) -> ConnectedAccount:
        configuration = self._configuration(configuration_id)

        async def verify(account: ConnectedAccount, credential: str) -> None:
            if (
                account.toolkit != "home-assistant"
                or account.settings.get("managed_oauth_configuration_id") != configuration.id
                or account.settings.get("base_url") != configuration.base_url
            ):
                raise EnergyError("oauth_configuration_unavailable", "Configuration has changed.")
            await probe_provider(self.http, account, credential)

        return await self.store.complete_managed_oauth(
            user_id,
            workspace_id,
            state,
            code,
            configuration.redirect_uri,
            verify=verify,
            authorize_write=authorize_write,
            expected_provider=configuration.provider(),
            expected_configuration_id=configuration.id,
        )
