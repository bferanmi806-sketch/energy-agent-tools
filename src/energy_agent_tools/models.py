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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    provider: str | None = None
    site_id: str | None = None
    asset_id: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    original_unit: str | None = None
    field_units: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    quality: str = "unknown"
    provenance: list[Json] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_coverage(self) -> EnergyResult:
        for instant in (self.time_start, self.time_end, self.retrieved_at):
            if instant is not None and instant.tzinfo is None:
                raise ValueError("Result timestamps require explicit UTC offsets")
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("Result time coverage is reversed")
        return self

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class AuthConfig(StrictModel):
    scheme: Literal["none", "api-key", "bearer", "basic", "oauth", "mcp", "local"] = "none"
    credential_env: str | None = None
    secret_id: str | None = None
    header: str = "Authorization"


class ConnectedAccount(StrictModel):
    id: str
    user_id: str
    toolkit: str
    site_id: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    settings: Json = Field(default_factory=dict)
    enabled: bool = True
    state: str = "active"
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("last_verified_at", "expires_at")
    @classmethod
    def aware_account_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Connection timestamps require explicit UTC offsets")
        return value

    @field_validator("settings")
    @classmethod
    def nonsecret_settings(cls, value: Json) -> Json:
        secret_keys = {
            "authorization",
            "password",
            "api_key",
            "apikey",
            "credential",
            "client_secret",
            "access_token",
            "refresh_token",
            "token",
            "secret",
        }

        def contains(item: Any) -> bool:
            if isinstance(item, dict):
                return any(
                    str(key).lower().replace("-", "_") in secret_keys or contains(nested)
                    for key, nested in item.items()
                )
            if isinstance(item, list):
                return any(contains(nested) for nested in item)
            return False

        if contains(value):
            raise ValueError(
                "Settings cannot contain credential fields; use environment references or encrypted storage"
            )
        return value

    def public(self) -> Json:
        return {
            "id": self.id,
            "toolkit": self.toolkit,
            "site_id": self.site_id,
            "enabled": self.enabled,
            "auth_scheme": self.auth.scheme,
            "state": self.state,
            "verified": self.last_verified_at is not None,
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
    kind: str = Field(min_length=1, max_length=80)
    name: str
    metadata: Json = Field(default_factory=dict)
    account_ids: list[str] = Field(default_factory=list)
    parent_id: str | None = None


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
    version: str = "1.0.0"
    categories: list[str] = Field(default_factory=list)


class Tool(StrictModel):
    name: str
    toolkit: str
    description: str
    input_schema: Json
    capabilities: list[str]
    actions: set[Action] = Field(default_factory=lambda: {Action.READ})
    idempotent: bool = True
    version: str = "1.0.0"
    reviewed: bool = True
    result_kind: DataKind | None = None
    result_unit: str | None = None
    dependencies: list[str] = Field(default_factory=list)

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
