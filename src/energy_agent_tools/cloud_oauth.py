"""Operator-approved cloud energy OAuth applications; secrets stay server-side."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from .auth import OAuthProvider
from .models import Json, StrictModel

TESLA_ORIGINS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}


class CloudEnergyOAuthConfiguration(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: StrictStr = Field(min_length=1, max_length=120)
    provider_name: Literal["tesla", "enphase"] = Field(alias="provider")
    client_id: StrictStr = Field(min_length=1, max_length=512)
    client_secret_env: StrictStr = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    redirect_uri: StrictStr = Field(min_length=1, max_length=2048)
    region: Literal["na", "eu"] = "na"
    api_key_env: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

    @field_validator("name", "client_id")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Use a nonempty application identifier or name")
        return value.strip()

    @field_validator("redirect_uri")
    @classmethod
    def approved_callback(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Cloud OAuth requires an exact registered HTTPS callback")
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def provider_options(self) -> CloudEnergyOAuthConfiguration:
        if self.provider_name == "enphase" and (not self.api_key_env or self.region != "na"):
            raise ValueError("Enphase requires an application API key environment variable")
        if self.provider_name == "tesla" and self.api_key_env is not None:
            raise ValueError("Tesla does not use an application API key")
        self.secret()
        if self.api_key_env:
            self.api_key()
        return self

    def secret(self) -> str:
        value = os.environ.get(self.client_secret_env)
        if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("The configured application secret is unavailable")
        return value

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env or "")
        if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("The configured application API key is unavailable")
        return value

    @property
    def toolkit(self) -> str:
        return "tesla-energy" if self.provider_name == "tesla" else "enphase-energy"

    def fingerprint(self) -> str:
        value = [
            self.provider_name,
            self.client_id,
            self.redirect_uri,
            self.region,
            self.secret(),
            self.api_key() if self.api_key_env else None,
        ]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    def provider(self) -> OAuthProvider:
        if self.provider_name == "tesla":
            return OAuthProvider(
                authorization_endpoint="https://auth.tesla.com/oauth2/v3/authorize",
                token_endpoint="https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token",
                client_id=self.client_id,
                client_secret=self.secret(),
                redirect_uri=self.redirect_uri,
                scopes=("openid", "offline_access", "energy_device_data"),
                protocol="oauth2_confidential",
                token_endpoint_auth_method="client_secret_post",
                refresh_endpoint_auth_method="none",
                extra_token_params={"audience": TESLA_ORIGINS[self.region]},
            )
        return OAuthProvider(
            authorization_endpoint="https://api.enphaseenergy.com/oauth/authorize",
            token_endpoint="https://api.enphaseenergy.com/oauth/token",
            client_id=self.client_id,
            client_secret=self.secret(),
            redirect_uri=self.redirect_uri,
            protocol="oauth2_confidential",
            token_endpoint_auth_method="client_secret_basic",
        )

    def public(self) -> Json:
        return {
            "id": self.id,
            "name": self.name,
            "toolkit": self.toolkit,
            "protocol": "oauth2_confidential",
        }
