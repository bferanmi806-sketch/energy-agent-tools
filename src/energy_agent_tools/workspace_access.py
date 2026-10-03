"""Explicit actor and resource grants for managed workspace sharing."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .models import StrictModel


class _AccessModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceMemberGrants(_AccessModel):
    site_ids: list[str] = Field(default_factory=list, max_length=256)
    connection_ids: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("site_ids", "connection_ids")
    @classmethod
    def identifiers(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("Grants must contain bounded nonblank identifiers.")
        if len(set(values)) != len(values):
            raise ValueError("Grants must contain unique identifiers.")
        return sorted(values)


class WorkspaceMember(_AccessModel):
    user_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    role: Literal["member"] = "member"
    grants: WorkspaceMemberGrants = Field(default_factory=WorkspaceMemberGrants)


class WorkspaceMemberRequest(_AccessModel):
    user_id: str = Field(min_length=1, max_length=256)


class WorkspaceKeyScope(_AccessModel):
    """Current authorization computed from durable key and membership state.

    This is an internal control-store result. It is never accepted from an HTTP
    caller. A member's explicit empty connection grant is distinct from the
    owner's unrestricted connection set.
    """

    actor_user_id: str = Field(min_length=1, max_length=256)
    resource_owner_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    site_ids: list[str]
    connection_ids: list[str] | None = Field(max_length=256)

    @field_validator("site_ids", "connection_ids")
    @classmethod
    def identifiers(cls, values: list[str] | None) -> list[str] | None:
        return WorkspaceMemberGrants.identifiers(values) if values is not None else None

    @model_validator(mode="after")
    def explicit_member_connections(self) -> WorkspaceKeyScope:
        if self.actor_user_id != self.resource_owner_id and self.connection_ids is None:
            raise ValueError("Members require explicit connection grants.")
        if self.actor_user_id == self.resource_owner_id and self.connection_ids is not None:
            raise ValueError("Owners use the unrestricted connection set.")
        return self
