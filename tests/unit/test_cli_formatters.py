"""Unit tests for CLI terminal and Markdown PR comment report formatters."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import click
import pytest
from typer.testing import CliRunner

from regrip.cli.app import app
from regrip.cli.formatters import format_ci_terminal_report
from regrip.core.report_exporters import render_markdown_pr_comment
from regrip.types.baseline import BaselineCase, BehavioralBaseline, ReplayRun, ReplaySummary
from regrip.types.config import RegripConfig, StorageConfig
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


def test_format_ci_terminal_report_no_color() -> None:
    baseline, run, diff, impact = _sample_fixtures()

    rendered = format_ci_terminal_report(
        baseline,
        run,
        diff,
        impact,
        report_dir=".regrip/reports",
        no_color=True,
    )
    styled = format_ci_terminal_report(baseline, run, diff, impact, report_dir=".regrip/reports")

    assert "\x1b" not in rendered
    assert "\x1b[" in styled
    assert rendered == click.unstyle(styled)
    assert "FAIL" in rendered
    assert "get_balance" in rendered


@pytest.mark.parametrize(
    ("no_color_env", "extra_args", "expect_color"),
    [
        pytest.param(None, [], True, id="default"),
        pytest.param(None, ["--no-color"], False, id="flag"),
        pytest.param("1", [], False, id="environment"),
        pytest.param("0", [], False, id="nonempty-zero"),
        pytest.param("", [], True, id="empty-environment"),
        pytest.param("1", ["--no-color"], False, id="both"),
        pytest.param("", ["--no-color"], False, id="flag-with-empty-environment"),
    ],
)
def test_ci_terminal_color_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_color_env: str | None,
    extra_args: list[str],
    expect_color: bool,
) -> None:
    baseline, run, diff, _ = _sample_fixtures()
    config = RegripConfig(
        storage=StorageConfig(
            dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs")
        )
    )
    config_path = tmp_path / "regrip.yaml"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color_env is not None:
        monkeypatch.setenv("NO_COLOR", no_color_env)
    monkeypatch.setattr("regrip.cli.ci_cmd.ReplayRunner.run", AsyncMock(return_value=run))
    monkeypatch.setattr("regrip.cli.ci_cmd.diff_baseline_against_run", lambda *_: diff)

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--config",
            str(config_path),
            "--baseline",
            str(baseline_path),
            "--target",
            run.target_uri,
            "--out-dir",
            str(tmp_path / "reports"),
            *extra_args,
        ],
        color=True,
    )

    assert result.exit_code == 1, result.output
    assert ("\x1b" in result.stdout) is expect_color
    assert "FAIL" in result.stdout
    assert "commerce-mcp@v1.0.0" in result.stdout
    assert "get_balance" in result.stdout
    assert "Reports written to" in result.stdout
