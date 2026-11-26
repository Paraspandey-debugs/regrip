"""Deterministic JSON, Markdown PR comment, and JUnit XML report exporters (FR-CICD-001..002)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict

from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.diff import DiffReport, FindingSeverity
from regrip.types.impact import AgentImpactEntry, ImpactReport


def render_json_report(
    baseline: BehavioralBaseline,
    run: ReplayRun,
    diff_report: DiffReport,
    impact_report: ImpactReport,
) -> str:
    """Serialize full replay, diff, and consumer impact report as deterministic JSON."""
    payload = {
        "baseline_tag": baseline.tag,
        "server_name": baseline.server_name,
        "target_uri": run.target_uri,
        "verdict": diff_report.verdict.value,
        "replay_summary": run.summary.model_dump(mode="json"),
        "diff_summary": diff_report.summary_counts,
        "findings": [f.model_dump(mode="json") for f in diff_report.findings],
        "agent_impacts": [a.model_dump(mode="json") for a in impact_report.agent_impacts],
        "blast_radii": [b.model_dump(mode="json") for b in impact_report.blast_radii],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_markdown_pr_comment(
    baseline: BehavioralBaseline,
    run: ReplayRun,
    diff_report: DiffReport,
    impact_report: ImpactReport,
) -> str:
    """Render sticky PR comment format with `<!-- regrip-report -->` marker."""
    s = run.summary
    executed = s.total - s.skipped_unsafe
    fail_cnt = diff_report.summary_counts.get(FindingSeverity.FAIL.value, 0)
    warn_cnt = diff_report.summary_counts.get(FindingSeverity.WARN.value, 0)

    lines = [
        "<!-- regrip-report -->",
        f"## Regrip Compatibility Report — {fail_cnt} Fail, {warn_cnt} Warn",
        f"**Target:** `{run.target_uri}` vs "
        f"**Baseline:** `{baseline.server_name} ({baseline.tag})` "
        f"· **Safe Replay:** `{executed}/{s.total}` cases executed "
        f"(`{s.skipped_unsafe}` write-blocked skipped)",
        "",
        "| Tool | Status | Category | Consumer Impact | Summary |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]

    findings_by_tool = defaultdict(list)
    for f in diff_report.findings:
        findings_by_tool[f.tool_name].append(f)

    impacts_by_tool: dict[str, list[AgentImpactEntry]] = defaultdict(list)
    for entry in impact_report.agent_impacts:
        impacts_by_tool[entry.tool_name].append(entry)

    all_tools = sorted({c.tool_name for c in baseline.cases} | set(findings_by_tool))
    for tool in all_tools:
        t_findings = findings_by_tool.get(tool, [])
        if not t_findings:
            status = "PASS"
            cats = "—"
            summary = "Matched baseline within tolerance"
        else:
            status = (
                "FAIL" if any(f.severity == FindingSeverity.FAIL for f in t_findings) else "WARN"
            )
            cats = " + ".join(sorted({f.category.value for f in t_findings}))
            summary = "; ".join(f.deterministic_reason for f in t_findings[:2])

        t_impacts = impacts_by_tool.get(tool, [])
        impact_str = (
            ", ".join(f"`{e.agent_id}`: **{e.impact_status.value}**" for e in t_impacts)
            if t_impacts
            else "No registered consumers"
        )
        lines.append(f"| `{tool}` | {status} | {cats} | {impact_str} | {summary} |")

    return "\n".join(lines) + "\n"


def render_junit_xml(diff_report: DiffReport) -> str:
    """Render DiffReport findings as a standard JUnit XML test suite."""
    suite = ET.Element(
        "testsuite",
        attrib={
            "name": f"regrip-{diff_report.baseline_tag}",
            "tests": str(max(1, len(diff_report.findings))),
            "failures": str(diff_report.summary_counts.get(FindingSeverity.FAIL.value, 0)),
        },
    )
    if not diff_report.findings:
        ET.SubElement(suite, "testcase", attrib={"classname": "regrip", "name": "baseline_check"})
    else:
        for finding in diff_report.findings:
            tc = ET.SubElement(
                suite,
                "testcase",
                attrib={
                    "classname": finding.tool_name,
                    "name": f"{finding.category.value}:{finding.json_path}",
                },
            )
            if finding.severity == FindingSeverity.FAIL:
                fail_el = ET.SubElement(
                    tc, "failure", attrib={"message": finding.deterministic_reason}
                )
                fail_el.text = finding.deterministic_reason
    return ET.tostring(suite, encoding="unicode")
