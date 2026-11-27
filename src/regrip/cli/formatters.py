"""Output formatting helpers for the Regrip CLI."""

from __future__ import annotations

import click
import typer

from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.diff import DiffReport, RegressionFinding
from regrip.types.impact import ImpactReport


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


def format_diff_report(report: DiffReport) -> str:
    """Format a DiffReport into a human-readable summary."""
    lines = [
        f"Diff Verdict: {report.verdict.value} "
        f"(baseline={report.baseline_tag}, target={report.target_label})",
        f"Counts: FAIL={report.summary_counts.get('FAIL', 0)} "
        f"WARN={report.summary_counts.get('WARN', 0)}",
    ]
    for f in report.findings:
        lines.append(
            f"  [{f.severity.value}] {f.tool_name} ({f.category.value}) {f.json_path}: "
            f"{f.deterministic_reason}"
        )
    return "\n".join(lines)


_SEV_STYLE = {"FAIL": "red", "WARN": "yellow", "PASS": "green"}
_IMPACT_STYLE = {
    "CONFIRMED_REGRESSION": ("red", "CONFIRMED"),
    "POTENTIALLY_BROKEN": ("yellow", "POSSIBLE"),
    "UNAFFECTED": ("green", "OK"),
}


def format_ci_terminal_report(
    baseline: BehavioralBaseline,
    run: ReplayRun,
    diff_report: DiffReport,
    impact: ImpactReport,
    *,
    report_dir: str | None = None,
    no_color: bool = False,
) -> str:
    """Render a compact, colored CI gate summary for interactive terminals.

    Colors are emitted as ANSI codes; `typer.echo` strips them when stdout is not a TTY.
    Setting `no_color` strips all ANSI styling before the report is returned.
    """
    s = run.summary
    verdict = diff_report.verdict.value
    banner = typer.style(
        f" {verdict} ", fg=_SEV_STYLE.get(verdict, "white"), bold=True, reverse=True
    )
    out = [
        "",
        f"{banner}  regrip ci · {baseline.server_name}@{baseline.tag}",
        typer.style(f"  candidate: {run.target_uri}", dim=True),
        typer.style(
            f"  replay: {s.total - s.skipped_unsafe}/{s.total} executed · "
            f"{s.skipped_unsafe} skipped · {s.errored} errored",
            dim=True,
        ),
    ]
    _append_terminal_findings(out, diff_report.findings)
    _append_terminal_impacts(out, impact)
    if report_dir:
        out += ["", typer.style(f"  Reports written to {report_dir}", dim=True)]
    result = "\n".join(out) + "\n"

    if no_color:
        return click.unstyle(result)

    return result


def _append_terminal_findings(out: list[str], findings: list[RegressionFinding]) -> None:
    by_tool: dict[str, list[RegressionFinding]] = {}
    for f in findings:
        by_tool.setdefault(f.tool_name, []).append(f)
    if not by_tool:
        out += ["", typer.style("  No behavioral regressions detected.", fg="green")]
        return
    for tool, items in by_tool.items():
        out += ["", typer.style(f"  {tool}", bold=True)]
        for f in items:
            sev = typer.style(f"{f.severity.value:<4}", fg=_SEV_STYLE[f.severity.value], bold=True)
            out.append(f"    {sev}  {f.category.value:<10} {f.json_path}")
            out.append(typer.style(f"          {f.deterministic_reason}", dim=True))


def _append_terminal_impacts(out: list[str], impact: ImpactReport) -> None:
    affected = [e for e in impact.agent_impacts if e.impact_status.value != "UNAFFECTED"]
    if not affected:
        return
    out += ["", typer.style("  Agent impact", bold=True)]
    for e in sorted(affected, key=lambda x: (x.agent_id, x.tool_name)):
        color, label = _IMPACT_STYLE[e.impact_status.value]
        paths = ", ".join(e.matched_jsonpaths) or "no field-level lineage"
        out.append(
            f"    {typer.style(f'{label:<9}', fg=color, bold=True)}  "
            f"{e.agent_id} -> {e.tool_name}  " + typer.style(paths, dim=True)
        )
