"""Shared contracts. Credentials belong to execution context, never tool schemas."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

Json = dict[str, Any]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataKind(StrEnum):
    METERED = "metered"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    SIMULATED = "simulated"
    FORECAST = "forecast"


class Action(StrEnum):
    READ = "read-only"
    CALCULATE = "calculation"
    SIMULATE = "simulation"
    EXTERNAL = "external-data"
    WRITE = "configuration-write"
    CONTROL = "physical-control"
    CRITICAL = "safety-critical"


class EnergyResult(StrictModel):
    data: Any
    kind: DataKind
    unit: str
    source: str
    timezone: str = "UTC"
    resolution: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    quality: str = "unknown"
    provenance: list[Json] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class AuthConfig(StrictModel):
    scheme: Literal["none", "api-key", "bearer", "basic", "oauth", "mcp", "local"] = "none"
    credential_env: str | None = None
    header: str = "Authorization"


class ConnectedAccount(StrictModel):
    id: str
    user_id: str
    toolkit: str
    site_id: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    settings: Json = Field(default_factory=dict)
    enabled: bool = True

    def public(self) -> Json:
        return {
            "id": self.id,
            "toolkit": self.toolkit,
            "site_id": self.site_id,
            "enabled": self.enabled,
            "auth_scheme": self.auth.scheme,
        }


class Site(StrictModel):
    id: str
    user_id: str
    name: str
    timezone: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class Asset(StrictModel):
    id: str
    site_id: str
    kind: Literal["meter", "pv", "battery", "heat-pump", "ev-charger", "equipment"]
    name: str
    metadata: Json = Field(default_factory=dict)


class Session(StrictModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: str
    site_id: str | None = None
    toolkits: set[str] | None = None
    allowed_actions: set[Action] = Field(
        default_factory=lambda: {Action.READ, Action.CALCULATE, Action.SIMULATE, Action.EXTERNAL}
    )
    account_ids: dict[str, str] = Field(default_factory=dict)


class Toolkit(StrictModel):
    id: str
    name: str
    description: str
    runtime: Literal["http", "python", "native", "executable", "mcp-local", "mcp-remote"]
    status: Literal["stable", "experimental", "requires credentials", "unavailable"]
    auth_required: bool = False
    docs_url: str | None = None


class Tool(StrictModel):
    name: str
    toolkit: str
    description: str
    input_schema: Json
    capabilities: list[str]
    actions: set[Action] = Field(default_factory=lambda: {Action.READ})
    idempotent: bool = True

    def public(self) -> Json:
        return self.model_dump(mode="json")


class EnergyError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ExecutionContext:
    session: Session
    account: ConnectedAccount | None
    credential: str | None
    http: httpx.AsyncClient
    workbench: Any


Handler = Callable[[Json, ExecutionContext], Awaitable[EnergyResult]]


def schema(properties: Json, required: list[str] | None = None) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
