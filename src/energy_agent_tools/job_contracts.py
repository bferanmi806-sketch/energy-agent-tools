"""Public job metadata and scopes derived from current gateway authorization."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field

from .jobs import JobStatus, SimulationOperation
from .models import StrictModel

JobIdentifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
ScopeIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
JobCursorToken = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]
JobErrorCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
JobState = Literal["pending", "running", "completed", "failed", "cancelled", "interrupted"]


class _JobModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobMetadata(_JobModel):
    job_id: JobIdentifier
    user_id: ScopeIdentifier
    session_id: ScopeIdentifier
    workspace_id: ScopeIdentifier | None
    site_id: ScopeIdentifier | None
    access_mode: Literal["local", "hosted"]
    operation: SimulationOperation
    status: JobStatus
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    error_code: JobErrorCode | None


class JobMetadataPage(_JobModel):
    jobs: list[JobMetadata] = Field(max_length=100)
    next_before: JobCursorToken | None


class JobCursor(_JobModel):
    created_at: AwareDatetime
    job_id: JobIdentifier


class JobListQuery(_JobModel):
    limit: int = Field(default=50, ge=1, le=100)
    before: JobCursorToken | None = None
    status: JobState | None = None


class JobActionQuery(_JobModel):
    operation: Literal["status", "result", "cancel", "delete", "start"]


class JobReadScope(_JobModel):
    user_id: ScopeIdentifier
    workspace_id: ScopeIdentifier | None
    access_mode: Literal["local", "hosted"]
    site_ids: set[ScopeIdentifier | None]
    operations: set[SimulationOperation] | None = None
