"""Shared Pydantic v2 models for baselines, capabilities, safety policies, and replay."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from regrip.types.envelope import JsonObject, ResponseStatus, ToolAnnotations


class SafetyTier(StrEnum):
    """Three-tier tool execution policy (SRS FR-SAF-001)."""

    READ_SAFE = "READ_SAFE"
    DRY_RUN_ONLY = "DRY_RUN_ONLY"
    WRITE_BLOCKED = "WRITE_BLOCKED"


class ReplayCaseStatus(StrEnum):
    """Outcome for a single replayed baseline case."""

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIPPED_UNSAFE = "SKIPPED_UNSAFE"
    ERROR = "ERROR"


class ToolDescriptor(BaseModel):
    """Normalized tool capability descriptor discovered from a target server."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject | None = None
    input_schema_hash: str | None = None
    output_schema_hash: str | None = None
    annotations: ToolAnnotations | None = None


class CapabilityManifest(BaseModel):
    """Protocol-agnostic capability manifest returned by a ProtocolDriver."""

    model_config = ConfigDict(extra="forbid")

    server_name: str
    server_version: str | None = None
    spec_version: str = "2025-11-25"
    tools: list[ToolDescriptor] = Field(default_factory=list)


class FieldTypeProfile(BaseModel):
    """Inferred runtime schema profile for a single JSONPath across samples."""

    model_config = ConfigDict(extra="forbid")

    json_path: str
    observed_types: list[str] = Field(default_factory=list)
    presence_ratio: float = Field(ge=0.0, le=1.0)
    nullable: bool = False
    sample_count: int = Field(ge=0)


class BaselineCase(BaseModel):
    """Representative clustered test case within a BehavioralBaseline."""

    model_config = ConfigDict(extra="forbid")

    case_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    method: str = "tools/call"
    cluster_key: str
    representative_input: JsonObject = Field(default_factory=dict)
    omitted_optional_keys: list[str] = Field(default_factory=list)
    expected_status: ResponseStatus = ResponseStatus.OK
    expected_is_error: bool = False
    expected_payload: JsonObject | list[JsonValue] | None = None
    expected_schema_profile: dict[str, FieldTypeProfile] = Field(default_factory=dict)
    sample_count: int = Field(ge=1, default=1)
    source_interaction_ids: list[UUID] = Field(default_factory=list)


class BehavioralBaseline(BaseModel):
    """Versioned, taggable behavioral baseline built from recorded interactions."""

    model_config = ConfigDict(extra="forbid")

    baseline_version: str = "1.0"
    baseline_id: UUID = Field(default_factory=uuid4)
    server_name: str
    tag: str
    git_sha: str | None = None
    content_hash: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    noise_masks: list[str] = Field(default_factory=list)
    tool_schemas: dict[str, ToolDescriptor] = Field(default_factory=dict)
    cases: list[BaselineCase] = Field(default_factory=list)


class ReplayCaseResult(BaseModel):
    """Comparison result for a single replayed BaselineCase."""

    model_config = ConfigDict(extra="forbid")

    case_id: UUID
    tool_name: str
    cluster_key: str
    status: ReplayCaseStatus
    reason: str
    duration_ms: float = Field(ge=0.0, default=0.0)
    baseline_status: ResponseStatus
    candidate_status: ResponseStatus | None = None
    diff_paths: list[str] = Field(default_factory=list)


class ReplaySummary(BaseModel):
    """Aggregate pass/fail/warn/skipped counters for a replay run."""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    skipped_unsafe: int = 0
    errored: int = 0


class ReplayRun(BaseModel):
    """Complete record of a replay execution against a candidate target."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    baseline_id: UUID
    baseline_tag: str
    target_uri: str
    target_git_sha: str | None = None
    execution_mode: str = "subprocess"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    summary: ReplaySummary = Field(default_factory=ReplaySummary)
    results: list[ReplayCaseResult] = Field(default_factory=list)
