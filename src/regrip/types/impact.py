"""Consumer-aware field lineage and agent impact models (SRS Section 3.4)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ContractDiscoverySource(StrEnum):
    """Priority-ordered sources for ConsumerContract field lineage (FR-IMP-001)."""

    EXPLICIT_YAML = "explicit_yaml"
    EXPLICIT_SDK = "explicit_sdk"
    DYNAMIC_ACCESS = "dynamic_access"
    SESSION_CORRELATION = "session_correlation"


class AgentImpactStatus(StrEnum):
    """Per-agent impact classification verdict (FR-IMP-002)."""

    UNAFFECTED = "UNAFFECTED"
    POTENTIALLY_BROKEN = "POTENTIALLY_BROKEN"
    CONFIRMED_REGRESSION = "CONFIRMED_REGRESSION"


class ConsumerContract(BaseModel):
    """Field-level dependency mapping between an agent and a tool."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    tool_name: str
    consumed_jsonpaths: list[str] = Field(default_factory=list)
    order_sensitive: bool = False
    discovery_source: ContractDiscoverySource = ContractDiscoverySource.EXPLICIT_YAML


class AgentImpactEntry(BaseModel):
    """Evaluated impact of findings on a specific registered consumer agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    tool_name: str
    impact_status: AgentImpactStatus
    matched_jsonpaths: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ToolBlastRadius(BaseModel):
    """Tool blast radius & call-volume weighted risk score (FR-IMP-003)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    affected_agents: int = 0
    total_agents: int = 0
    blast_radius: float = Field(default=0.0, ge=0.0, le=1.0)
    call_volume: int = 0
    risk_score: float = Field(default=0.0, ge=0.0)


class ImpactReport(BaseModel):
    """Complete per-agent compatibility matrix and blast radius report."""

    model_config = ConfigDict(extra="forbid")

    baseline_tag: str
    target_label: str
    agent_impacts: list[AgentImpactEntry] = Field(default_factory=list)
    blast_radii: list[ToolBlastRadius] = Field(default_factory=list)
