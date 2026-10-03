"""Explicit access contracts for private workspace keys."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from .models import StrictModel


class _KeyAccess(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ManageKeyAccess(_KeyAccess):
    kind: Literal["manage"] = "manage"


class AgentKeyAccess(_KeyAccess):
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


class LegacyKeyAccess(_KeyAccess):
    """Existing v1 execution keys, never issuable through managed control routes."""

    kind: Literal["legacy-agent"] = "legacy-agent"


KeyAccess = Annotated[
    ManageKeyAccess | AgentKeyAccess | LegacyKeyAccess, Field(discriminator="kind")
]
IssuableKeyAccess = Annotated[ManageKeyAccess | AgentKeyAccess, Field(discriminator="kind")]
