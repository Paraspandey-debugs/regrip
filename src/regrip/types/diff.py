"""Regression finding and diff report schemas (SRS Section 3.3)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class FindingCategory(StrEnum):
    """Nine-category deterministic behavioral regression taxonomy (FR-CMP-001..009)."""

    SCHEMA = "Schema"
    OUTPUT = "Output"
    TYPE = "Type"
    VALUE_UNIT = "Value/Unit"
    DEFAULT = "Default"
    ORDERING = "Ordering"
    ERROR = "Error"
    SEMANTIC = "Semantic"
    SIDE_EFFECT = "Side-Effect"


class FindingSeverity(StrEnum):
    """Severity and overall verdict level."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class RegressionFinding(BaseModel):
    """Single deterministic behavioral regression finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    cluster_key: str = ""
    category: FindingCategory
    severity: FindingSeverity
    json_path: str = "$"
    baseline_excerpt: JsonValue = None
    candidate_excerpt: JsonValue = None
    deterministic_reason: str
    llm_advisory_note: str | None = None


class DiffReport(BaseModel):
    """Complete deterministic diff report between baseline and candidate."""

    model_config = ConfigDict(extra="forbid")

    report_id: UUID = Field(default_factory=uuid4)
    baseline_tag: str
    target_label: str
    verdict: FindingSeverity = FindingSeverity.PASS
    findings: list[RegressionFinding] = Field(default_factory=list)
    summary_counts: dict[str, int] = Field(default_factory=dict)
