"""Bounded owner review requests for managed HTTP MCP connections."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from .models import Action, DataKind, StrictModel


class _MCPModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MCPConnectionInspectionRequest(_MCPModel):
    url: StrictStr = Field(min_length=1, max_length=2048)
    auth_scheme: Literal["none", "bearer", "basic", "api-key"] = "none"
    auth_header: StrictStr = Field(
        default="Authorization", pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$"
    )
    credential: StrictStr | None = Field(default=None, min_length=1, max_length=4096, repr=False)

    @model_validator(mode="after")
    def credential_scheme(self) -> MCPConnectionInspectionRequest:
        if (self.auth_scheme == "none") != (self.credential is None):
            raise ValueError("Credential must match the selected authentication scheme")
        return self


class MCPToolReview(_MCPModel):
    name: StrictStr = Field(min_length=1, max_length=256)
    reviewed: Literal[True]
    actions: list[Action] = Field(min_length=1, max_length=len(Action))
    kind: DataKind
    unit: StrictStr = Field(min_length=1, max_length=128)

    @field_validator("reviewed", mode="before")
    @classmethod
    def explicit_review(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("Selected tools require explicit review")
        return True

    @field_validator("name", "unit")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Review fields must not be blank")
        return value

    @field_validator("actions", mode="before")
    @classmethod
    def parse_actions(cls, value: Any) -> list[Action]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("Actions must be a list of canonical action names")
        actions = [Action(item) for item in value]
        if len(set(actions)) != len(actions):
            raise ValueError("Actions must be unique")
        return actions

    @field_validator("kind", mode="before")
    @classmethod
    def parse_kind(cls, value: Any) -> DataKind:
        if not isinstance(value, str):
            raise ValueError("Kind must be a canonical result kind")
        return DataKind(value)


class MCPConnectionStageRequest(MCPConnectionInspectionRequest):
    display_name: StrictStr = Field(min_length=1, max_length=256)
    schema_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    reviews: list[MCPToolReview] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_reviews(self) -> MCPConnectionStageRequest:
        if not self.display_name.strip():
            raise ValueError("A connection name is required")
        if len({review.name for review in self.reviews}) != len(self.reviews):
            raise ValueError("Select each tool only once")
        return self


class MCPToolInspection(_MCPModel):
    name: StrictStr = Field(min_length=1, max_length=256)
    schema_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class MCPInspection(_MCPModel):
    schema_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    annotations_untrusted: Literal[True] = True
    tools: list[MCPToolInspection] = Field(max_length=500)


class MCPConnectionInspectionResponse(_MCPModel):
    inspection: MCPInspection


class MCPRecoveryConnection(_MCPModel):
    connection_id: StrictStr = Field(min_length=1, max_length=256)
    status: Literal["ready", "unavailable", "skipped"]
    error_code: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class MCPConnectionRecoveryResponse(_MCPModel):
    ok: Literal[True] = True
    connections: list[MCPRecoveryConnection]
