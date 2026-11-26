"""Pure historical behavioral observability analyzer (FR-OBS-001)."""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from regrip.core.payload_normalizer import canonical_json
from regrip.types.baseline import ReplayCaseStatus, ReplayRun
from regrip.types.envelope import InteractionEnvelope


class ToolHistoryEvent(BaseModel):
    """Single chronological replay observation for a tool."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    baseline_tag: str
    target_git_sha: str | None = None
    started_at: str
    total_cases: int
    failed_cases: int
    failure_rate: float


class ToolHistoryReport(BaseModel):
    """Aggregated historical timeline, parameter patterns, and consumers for a tool."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    total_recorded_calls: int = 0
    error_rate: float = 0.0
    dependent_agents: list[str] = Field(default_factory=list)
    top_parameter_combinations: list[dict[str, object]] = Field(default_factory=list)
    timeline: list[ToolHistoryEvent] = Field(default_factory=list)


def build_tool_history(
    tool_name: str,
    interactions: list[InteractionEnvelope],
    replay_runs: list[ReplayRun],
    *,
    top_n_params: int = 5,
) -> ToolHistoryReport:
    """Aggregate chronological replay drift, error rates, and consumers for a tool."""
    tool_envs = [e for e in interactions if e.operation.tool_name == tool_name]
    total_calls = len(tool_envs)
    err_calls = sum(1 for e in tool_envs if e.response.is_error)
    error_rate = round(err_calls / total_calls, 4) if total_calls > 0 else 0.0
    agents = sorted({e.actor.agent_id for e in tool_envs})

    param_counts = Counter(canonical_json(e.request.arguments) for e in tool_envs)
    top_params = [
        {"arguments": raw_args, "count": cnt}
        for raw_args, cnt in param_counts.most_common(top_n_params)
    ]

    timeline: list[ToolHistoryEvent] = []
    for run in replay_runs:
        tool_results = [r for r in run.results if r.tool_name == tool_name]
        if not tool_results:
            continue
        failed = sum(1 for r in tool_results if r.status == ReplayCaseStatus.FAIL)
        total = len(tool_results)
        timeline.append(
            ToolHistoryEvent(
                run_id=str(run.run_id),
                baseline_tag=run.baseline_tag,
                target_git_sha=run.target_git_sha,
                started_at=run.started_at.isoformat(),
                total_cases=total,
                failed_cases=failed,
                failure_rate=round(failed / total, 4) if total > 0 else 0.0,
            )
        )

    return ToolHistoryReport(
        tool_name=tool_name,
        total_recorded_calls=total_calls,
        error_rate=error_rate,
        dependent_agents=agents,
        top_parameter_combinations=top_params,
        timeline=timeline,
    )
