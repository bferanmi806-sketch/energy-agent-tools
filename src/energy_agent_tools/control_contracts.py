"""Explicit access contracts for private workspace keys."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .models import StrictModel

WorkspaceMode = Literal["operator", "managed"]


class _ControlModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ManageKeyAccess(_ControlModel):
    kind: Literal["manage"] = "manage"


class AgentKeyAccess(_ControlModel):
    kind: Literal["agent"] = "agent"
    site_ids: list[str] = Field(min_length=1, max_length=256)

    @field_validator("site_ids")
    @classmethod
    def valid_sites(cls, value: list[str]) -> list[str]:
        if any(not site.strip() or len(site) > 256 for site in value):
            raise ValueError("Site grants must be non-empty bounded identifiers.")
        if len(set(value)) != len(value):
            raise ValueError("Site grants must be unique.")
        return sorted(value)


class LegacyKeyAccess(_ControlModel):
    """Existing v1 execution keys, never issuable through managed control routes."""

    kind: Literal["legacy-agent"] = "legacy-agent"


KeyAccess = Annotated[
    ManageKeyAccess | AgentKeyAccess | LegacyKeyAccess, Field(discriminator="kind")
]
IssuableKeyAccess = Annotated[ManageKeyAccess | AgentKeyAccess, Field(discriminator="kind")]


class WorkspaceDetails(_ControlModel):
    id: StrictStr
    user_id: StrictStr
    name: StrictStr
    mode: WorkspaceMode


class WorkspaceSiteRequest(_ControlModel):
    name: StrictStr = Field(min_length=1, max_length=256)
    timezone: StrictStr = Field(min_length=1, max_length=80)
    latitude: StrictFloat | None = Field(default=None, ge=-90, le=90)
    longitude: StrictFloat | None = Field(default=None, ge=-180, le=180)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A name is required")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone") from None
        return value


class WorkspaceAssetRequest(_ControlModel):
    site_id: StrictStr = Field(min_length=1, max_length=256)
    name: StrictStr = Field(min_length=1, max_length=256)
    kind: StrictStr = Field(min_length=1, max_length=80)
    parent_id: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    account_ids: list[StrictStr] = Field(default_factory=list, max_length=32)

    @field_validator("name", "kind", "site_id", "parent_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Fields must not be blank")
        return value.strip() if value is not None else None

    @field_validator("account_ids")
    @classmethod
    def valid_accounts(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not item.strip() or len(item) > 256 for item in value
        ):
            raise ValueError("Account references must be unique bounded identifiers")
        return value


class WorkspaceMapRequest(_ControlModel):
    site_id: StrictStr = Field(min_length=1, max_length=256)


class WorkspaceAgentKeyRequest(_ControlModel):
    name: StrictStr = Field(min_length=1, max_length=256)
    site_ids: list[StrictStr] = Field(min_length=1, max_length=256)

    @field_validator("site_ids")
    @classmethod
    def valid_grants(cls, value: list[str]) -> list[str]:
        return AgentKeyAccess(site_ids=value).site_ids

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A key name is required")
        return value.strip()


class ReviewedHomeAssistantMapping(_ControlModel):
    telemetry_role: Literal[
        "consumption_interval", "current_power", "generation", "export", "storage_state"
    ]
    unit: Literal["kWh", "W", "kW", "MW", "%"]
    quantity_shape: Literal["interval", "instantaneous"]
    measurement_kind: Literal["metered"] = "metered"

    @model_validator(mode="after")
    def supported_mapping(self) -> ReviewedHomeAssistantMapping:
        from .onboarding import reviewed_provider_bindings

        if not reviewed_provider_bindings(
            "home_assistant", {"entity_id": "sensor.reviewed", **self.model_dump()}
        ):
            raise ValueError("Select a compatible telemetry role, unit and quantity shape")
        return self


class WorkspaceHomeAssistantAuthorizationRequest(_ControlModel):
    configuration_id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    entity_id: StrictStr = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")
    mapping: ReviewedHomeAssistantMapping | None = None


class WorkspaceProviderAuthorizationRequest(_ControlModel):
    configuration_id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    resource_id: StrictStr = Field(pattern=r"^[0-9]{1,32}$")


class WorkspaceOAuthCompleteRequest(_ControlModel):
    configuration_id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    state: StrictStr = Field(min_length=1, max_length=512, repr=False)
    code: StrictStr = Field(min_length=1, max_length=2048, repr=False)


class WorkspaceOAuthConfiguration(_ControlModel):
    id: StrictStr
    name: StrictStr
    toolkit: Literal["home-assistant", "tesla-energy", "enphase-energy"] = "home-assistant"
    protocol: Literal["home_assistant", "oauth2_confidential"] = "home_assistant"
    pending_cleanup: StrictInt = Field(default=0, ge=0)


class WorkspaceOAuthConfigurationsResponse(_ControlModel):
    configurations: list[WorkspaceOAuthConfiguration]


class WorkspaceAuthorization(_ControlModel):
    connection_id: StrictStr
    authorization_url: StrictStr = Field(repr=False)
    state: StrictStr = Field(repr=False)
    expires_at: datetime


class WorkspaceAuthorizationResponse(_ControlModel):
    authorization: WorkspaceAuthorization


class WorkspaceOAuthCleanupRequest(_ControlModel):
    configuration_id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class WorkspaceOAuthCleanup(_ControlModel):
    attempted: StrictInt = Field(ge=0)
    succeeded: StrictInt = Field(ge=0)
    pending: StrictInt = Field(ge=0)


class WorkspaceOAuthCleanupResponse(_ControlModel):
    cleanup: WorkspaceOAuthCleanup
