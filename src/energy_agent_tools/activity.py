"""Bounded execution metadata and internally derived activity scopes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field

from .models import DataKind, StrictModel

Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class _ActivityModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExecutionSuccess(_ActivityModel):
    kind: Literal["success"]
    data_kind: DataKind | None = None


class ExecutionFailure(_ActivityModel):
    kind: Literal["failure"]
    error_code: Identifier


ExecutionOutcome = Annotated[ExecutionSuccess | ExecutionFailure, Field(discriminator="kind")]


class ExecutionEntry(_ActivityModel):
    execution_id: Identifier
    user_id: Identifier
    workspace_id: Identifier | None
    key_id: Identifier | None
    session_id: Identifier
    site_id: Identifier | None
    account_id: Identifier | None
    access_mode: Literal["local", "hosted"]
    recorded_at: AwareDatetime
    tool: Identifier
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    outcome: ExecutionOutcome


class ExecutionLogRecord(ExecutionEntry):
    sequence: int = Field(gt=0)


class ExecutionLogPage(_ActivityModel):
    entries: list[ExecutionLogRecord] = Field(max_length=100)
    next_before: int | None = Field(gt=0)
    retention_limit: int = Field(ge=1, le=2000)
    recording_status: Literal["ok", "unavailable"]


class ExecutionLogScope(_ActivityModel):
    """Trusted scope computed from current gateway identity, never an HTTP body."""

    user_id: Identifier
    workspace_id: Identifier | None
    access_mode: Literal["local", "hosted"]
    site_ids: set[Identifier | None]
    connection_ids: set[Identifier] | None


class ExecutionLogQuery(_ActivityModel):
    limit: int = Field(default=50, ge=1, le=100)
    before: int | None = Field(default=None, gt=0, le=9_007_199_254_740_991)
