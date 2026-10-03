"""Explicit access contracts for private workspace keys."""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, StrictFloat, StrictStr, field_validator

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
