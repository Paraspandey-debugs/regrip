"""Output formatting helpers for the Regrip CLI."""

from __future__ import annotations

from regrip.types.baseline import BehavioralBaseline, ReplayRun


def format_baseline_summary(baseline: BehavioralBaseline) -> str:
    """Format a concise human-readable summary of a BehavioralBaseline."""
    lines = [
        f"Baseline: {baseline.server_name}@{baseline.tag}",
        f"Hash:     {baseline.content_hash[:16]}",
        f"Cases:    {len(baseline.cases)}",
        f"Masks:    {len(baseline.noise_masks)} ({', '.join(baseline.noise_masks) or 'none'})",
    ]
    for case in baseline.cases:
        lines.append(
            f"  - {case.tool_name} [{case.cluster_key}] samples={case.sample_count} "
            f"status={case.expected_status.value}"
        )
    return "\n".join(lines)


def format_replay_report(run: ReplayRun) -> str:
    """Format a ReplayRun into a concise CLI report."""
    s = run.summary
    lines = [
        f"Replay Run {run.run_id} (baseline={run.baseline_tag}, target={run.target_uri})",
        f"Summary: total={s.total} passed={s.passed} failed={s.failed} "
        f"warned={s.warned} skipped_unsafe={s.skipped_unsafe} errored={s.errored}",
    ]
    for item in run.results:
        paths = f" paths={','.join(item.diff_paths)}" if item.diff_paths else ""
        lines.append(
            f"  [{item.status.value}] {item.tool_name} ({item.duration_ms:.1f}ms): "
            f"{item.reason}{paths}"
        )
    return "\n".join(lines)
