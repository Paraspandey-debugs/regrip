"""Validation tests for the composite GitHub Action definition."""

from __future__ import annotations

from pathlib import Path

import yaml

from regrip.core.report_exporters import render_markdown_pr_comment
from regrip.types import (
    BaselineCase,
    BehavioralBaseline,
    DiffReport,
    FindingSeverity,
    ImpactReport,
    ReplayRun,
)


def test_action_yml_schema_and_sticky_marker_alignment() -> None:
    action_path = Path(__file__).resolve().parents[2] / "action.yml"
    data = yaml.safe_load(action_path.read_text(encoding="utf-8"))

    assert data["runs"]["using"] == "composite"
    assert {"baseline", "target"}.issubset(data["inputs"].keys())
    assert {"verdict", "report-json", "report-markdown", "exit-code"}.issubset(
        data["outputs"].keys()
    )

    baseline = BehavioralBaseline(
        server_name="svc",
        tag="v1",
        cases=[BaselineCase(tool_name="ping", cluster_key="ping:1")],
    )
    run = ReplayRun(baseline_id=baseline.baseline_id, baseline_tag="v1", target_uri="stdio://cand")
    diff = DiffReport(baseline_tag="v1", target_label="cand", verdict=FindingSeverity.PASS)
    impact = ImpactReport(baseline_tag="v1", target_label="cand")
    md = render_markdown_pr_comment(baseline, run, diff, impact)

    comment_step = next(s for s in data["runs"]["steps"] if "sticky PR comment" in s["name"])
    assert "<!-- regrip-report -->" in comment_step["with"]["script"]
    assert md.startswith("<!-- regrip-report -->")
