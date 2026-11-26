"""Configuration schemas for regrip.yaml (SRS Section 4.4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from regrip.types.baseline import SafetyTier


class StorageMode(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class RedactionAction(StrEnum):
    DROP = "drop"
    MASK = "mask"
    HMAC_TOKEN = "hmac_token"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: StorageMode = StorageMode.LOCAL
    dsn: str = "sqlite:///.regrip/regrip.db"
    blob_dir: str = ".regrip/blobs"
    s3_bucket: str | None = None
    s3_prefix: str = "regrip/blobs"
    api_token_env: str = "REGRIP_API_TOKEN"
    project_token_envs: dict[str, str] = Field(default_factory=dict)


class RedactionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: RedactionAction = RedactionAction.MASK


class RedactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    hmac_secret_env: str = "REGRIP_HMAC_SECRET"
    rules: list[RedactionRule] = Field(default_factory=list)
    builtin_detectors: list[str] = Field(
        default_factory=lambda: ["jwt", "bearer", "aws_key", "credit_card", "email", "ssn"]
    )


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    strip_headers: list[str] = Field(
        default_factory=lambda: ["authorization", "cookie", "set-cookie", "x-api-key"]
    )


class ToolPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: SafetyTier = SafetyTier.WRITE_BLOCKED
    probe_idempotency: bool = False
    dry_run_arg: dict[str, JsonValue] = Field(default_factory=dict)
    state_probe_tool: str | None = None
    state_probe_args: dict[str, JsonValue] = Field(default_factory=dict)


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trust_mcp_annotations: Literal[False] = False
    default_tier: SafetyTier = SafetyTier.WRITE_BLOCKED
    blocked_target_patterns: list[str] = Field(default_factory=lambda: ["*prod*", "*.live.*"])
    tool_policies: dict[str, ToolPolicyConfig] = Field(default_factory=dict)


class ToolComparisonOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_sensitive: bool | None = None
    top_k_critical: int = Field(default=1, ge=1)
    kendall_tau_min: float = Field(default=0.70, ge=-1.0, le=1.0)
    numeric_relative_tolerance: float | None = Field(default=None, ge=0.0)
    assertions: list[str] = Field(default_factory=list)


class AdvisoryLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-5-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "REGRIP_LLM_API_KEY"


class ComparisonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numeric_relative_tolerance: float = Field(default=0.05, ge=0.0)
    detect_unit_scaling: bool = True
    default_order_sensitive: bool = False
    text_similarity_min: float = Field(default=0.75, ge=0.0, le=1.0)
    tool_overrides: dict[str, ToolComparisonOverride] = Field(default_factory=dict)
    advisory_llm: AdvisoryLLMConfig = Field(default_factory=AdvisoryLLMConfig)


class CIGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox: Literal["auto", "docker", "subprocess"] = "auto"
    sandbox_image: str | None = None
    egress_audit: bool = True
    fail_on_severities: list[str] = Field(default_factory=lambda: ["FAIL"])
    warn_on_severities: list[str] = Field(default_factory=lambda: ["WARN"])
    require_min_replay_coverage: float = Field(default=0.80, ge=0.0, le=1.0)


class ConsumerToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumed_jsonpaths: list[str] = Field(default_factory=list)
    order_sensitive: bool = False


class ConsumerAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    tools: dict[str, ConsumerToolConfig] = Field(default_factory=dict)


class RegripConfig(BaseModel):
    """Top-level regrip.yaml configuration model."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    project: str = "default-project"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    consumers: list[ConsumerAgentConfig] = Field(default_factory=list)
    ci_gate: CIGateConfig = Field(default_factory=CIGateConfig)
