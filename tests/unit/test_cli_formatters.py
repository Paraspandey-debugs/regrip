"""Unit tests for CLI terminal and Markdown PR comment report formatters."""

from __future__ import annotations

import click

from regrip.cli.formatters import format_ci_terminal_report
from regrip.core.report_exporters import render_markdown_pr_comment
from regrip.types.baseline import BaselineCase, BehavioralBaseline, ReplayRun, ReplaySummary
from regrip.types.diff import DiffReport, FindingCategory, FindingSeverity, RegressionFinding
from regrip.types.impact import AgentImpactEntry, AgentImpactStatus, ImpactReport


def _sample_fixtures() -> tuple[BehavioralBaseline, ReplayRun, DiffReport, ImpactReport]:
    baseline = BehavioralBaseline(
        server_name="commerce-mcp",
        tag="v1.0.0",
        cases=[
            BaselineCase(tool_name="get_balance", cluster_key="c1"),
            BaselineCase(tool_name="ping", cluster_key="c2"),
        ],
    )
    run = ReplayRun(
        baseline_id=baseline.baseline_id,
        baseline_tag="v1.0.0",
        target_uri="stdio://python|v2.py",
        summary=ReplaySummary(total=2, passed=1, failed=1),
    )
    diff = DiffReport(
        baseline_tag="v1.0.0",
        target_label="stdio://python|v2.py",
        verdict=FindingSeverity.FAIL,
        summary_counts={"FAIL": 1, "WARN": 0},
        findings=[
            RegressionFinding(
                tool_name="get_balance",
                category=FindingCategory.VALUE_UNIT,
                severity=FindingSeverity.FAIL,
                json_path="$.balance",
                deterministic_reason="100x scale shift: 1250 -> 125000",
            )
        ],
    )
    impact = ImpactReport(
        baseline_tag="v1.0.0",
        target_label="stdio://python|v2.py",
        agent_impacts=[
            AgentImpactEntry(
                agent_id="support-agent",
                tool_name="get_balance",
                impact_status=AgentImpactStatus.CONFIRMED_REGRESSION,
                matched_jsonpaths=["$.balance"],
            )
        ],
    )
    return baseline, run, diff, impact


def test_format_ci_terminal_report_fail_and_pass() -> None:
    baseline, run, diff, impact = _sample_fixtures()
    rendered = click.unstyle(
        format_ci_terminal_report(baseline, run, diff, impact, report_dir=".regrip/reports")
    )
    assert "FAIL" in rendered
    assert "commerce-mcp@v1.0.0" in rendered
    assert "get_balance" in rendered
    assert "support-agent -> get_balance" in rendered
    assert "Reports written to .regrip/reports" in rendered

    pass_diff = DiffReport(baseline_tag="v1.0.0", target_label="v2", verdict=FindingSeverity.PASS)
    pass_impact = ImpactReport(baseline_tag="v1.0.0", target_label="v2")
    pass_rendered = click.unstyle(format_ci_terminal_report(baseline, run, pass_diff, pass_impact))
    assert "PASS" in pass_rendered
    assert "No behavioral regressions detected." in pass_rendered


def test_render_markdown_pr_comment_sections_and_escaping() -> None:
    baseline, run, diff, impact = _sample_fixtures()
    md = render_markdown_pr_comment(baseline, run, diff, impact)
    assert md.startswith("<!-- regrip-report -->")
    assert "stdio://python\\|v2.py" in md
    assert "### Tools" in md
    assert "### What changed" in md
    assert "<details open>" in md
    assert "### Agent impact" in md
    assert "`support-agent` | `get_balance` | 🔴 CONFIRMED_REGRESSION | `$.balance`" in md
