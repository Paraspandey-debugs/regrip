"""Canonical InteractionEnvelope v1.0 schema models (SRS Section 2.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

JsonObject = dict[str, JsonValue]


class ProtocolDriverKind(StrEnum):
    MCP = "mcp"
    HTTP = "http"
    GRAPHQL = "graphql"
    GRPC = "grpc"
    A2A = "a2a"


class TransportKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    SDK_INPROC = "sdk_inproc"


class EnvironmentKind(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class ResponseStatus(StrEnum):
    OK = "ok"
    TOOL_ERROR = "tool_error"
    PROTOCOL_ERROR = "protocol_error"
    TIMEOUT = "timeout"


class ProtocolInfo(BaseModel):
    """Protocol transport metadata for a captured interaction."""

    model_config = ConfigDict(extra="forbid")

    driver: ProtocolDriverKind
    spec_version: str = "2025-11-25"
    transport: TransportKind


class ActorInfo(BaseModel):
    """Calling agent and session attribution."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = "default-agent"
    agent_version: str | None = None
    session_id: str | None = None
    environment: EnvironmentKind = EnvironmentKind.LOCAL


class TargetInfo(BaseModel):
    """Target server identity and version metadata."""

    model_config = ConfigDict(extra="forbid")

    server_name: str
    server_version: str | None = None
    git_sha: str | None = None


class ToolAnnotations(BaseModel):
    """Untrusted MCP ToolAnnotations hints (never relied upon for replay safety)."""

    model_config = ConfigDict(extra="allow")

    readOnlyHint: bool | None = None  # noqa: N815
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815


class OperationInfo(BaseModel):
    """Invoked operation and tool schema metadata."""

    model_config = ConfigDict(extra="forbid")

    method: str = "tools/call"
    tool_name: str
    input_schema_hash: str | None = None
    output_schema_hash: str | None = None
    annotations: ToolAnnotations | None = None


class RequestPayload(BaseModel):
    """Normalized request arguments and redaction metadata."""

    model_config = ConfigDict(extra="forbid")

    arguments: JsonObject = Field(default_factory=dict)
    omitted_optional_keys: list[str] = Field(default_factory=list)
    redacted_paths: list[str] = Field(default_factory=list)


class ResponsePayload(BaseModel):
    """Normalized response payload supporting structuredContent and JSON TextContent."""

    model_config = ConfigDict(extra="forbid")

    status: ResponseStatus
    is_error: bool = False
    error_code: int | str | None = None
    error_message: str | None = None
    structured_content: JsonObject | list[JsonValue] | None = None
    content_blocks: list[JsonObject] = Field(default_factory=list)
    parsed_text_json: JsonObject | list[JsonValue] | None = None
    inferred_types: dict[str, str] = Field(default_factory=dict)


class TelemetryInfo(BaseModel):
    """Execution timing and W3C trace context telemetry."""

    model_config = ConfigDict(extra="forbid")

    duration_ms: float = Field(ge=0.0)
    traceparent: str | None = None
    tracestate: str | None = None
    redaction_applied: bool = False


class InteractionEnvelope(BaseModel):
    """Protocol-agnostic canonical interaction record."""

    model_config = ConfigDict(extra="forbid")

    envelope_version: Literal["1.0"] = "1.0"
    interaction_id: UUID = Field(default_factory=uuid4)
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    protocol: ProtocolInfo
    actor: ActorInfo
    target: TargetInfo
    operation: OperationInfo
    request: RequestPayload
    response: ResponsePayload
    telemetry: TelemetryInfo
