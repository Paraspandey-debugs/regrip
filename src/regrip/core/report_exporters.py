"""Deterministic JSON, Markdown PR comment, and JUnit XML report exporters (FR-CICD-001..002)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict

from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.diff import DiffReport, FindingSeverity, RegressionFinding
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


_SEV_ICON = {"FAIL": "\u274c", "WARN": "\u26a0\ufe0f", "PASS": "\u2705"}
_IMPACT_ICON = {
    "CONFIRMED_REGRESSION": "\U0001f534",
    "POTENTIALLY_BROKEN": "\U0001f7e1",
    "UNAFFECTED": "\U0001f7e2",
}


def _cell(text: object) -> str:
    """Escape a value for use inside a markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _render_tools_section(
    all_tools: list[str],
    findings_by_tool: dict[str, list[RegressionFinding]],
    impacts_by_tool: dict[str, list[AgentImpactEntry]],
) -> list[str]:
    lines = [
        "### Tools",
        "",
        "| Tool | Status | Findings | Consumers |",
        "| :--- | :---: | :---: | :--- |",
    ]
    for tool in all_tools:
        t_findings = findings_by_tool.get(tool, [])
        if any(f.severity == FindingSeverity.FAIL for f in t_findings):
            status = "FAIL"
        elif t_findings:
            status = "WARN"
        else:
            status = "PASS"
        consumers = (
            " \u00b7 ".join(
                f"{_IMPACT_ICON[e.impact_status.value]} `{e.agent_id}`"
                for e in impacts_by_tool.get(tool, [])
            )
            or "\u2014"
        )
        count = str(len(t_findings)) if t_findings else "\u2014"
        lines.append(f"| `{tool}` | {_SEV_ICON[status]} {status} | {count} | {consumers} |")
    return lines


def _render_findings_details(findings_by_tool: dict[str, list[RegressionFinding]]) -> list[str]:
    lines = ["", "### What changed", ""]
    for tool in sorted(findings_by_tool):
        items = findings_by_tool[tool]
        lines += [
            "<details open>",
            f"<summary><b><code>{tool}</code></b> \u2014 {_plural(len(items), 'finding')}"
            "</summary>",
            "",
            "| Severity | Category | Path | Detail |",
            "| :---: | :--- | :--- | :--- |",
        ]
        for f in items:
            lines.append(
                f"| {_SEV_ICON[f.severity.value]} {f.severity.value} | {f.category.value} "
                f"| `{_cell(f.json_path)}` | {_cell(f.deterministic_reason)} |"
            )
        lines += ["", "</details>", ""]
    return lines


def _render_agent_impact_section(impact_report: ImpactReport) -> list[str]:
    broken = [e for e in impact_report.agent_impacts if e.impact_status.value != "UNAFFECTED"]
    if not broken:
        return []
    lines = ["### Agent impact", "", "| Agent | Tool | Verdict | Fields relied on |"]
    lines.append("| :--- | :--- | :--- | :--- |")
    for e in sorted(broken, key=lambda x: (x.agent_id, x.tool_name)):
        fields = ", ".join(f"`{p}`" for p in e.matched_jsonpaths) or "\u2014"
        lines.append(
            f"| `{e.agent_id}` | `{e.tool_name}` | "
            f"{_IMPACT_ICON[e.impact_status.value]} {e.impact_status.value} | {fields} |"
        )
    lines.append("")
    return lines


def render_markdown_pr_comment(
    baseline: BehavioralBaseline,
    run: ReplayRun,
    diff_report: DiffReport,
    impact_report: ImpactReport,
) -> str:
    """Render sticky PR comment with the `<!-- regrip-report -->` marker (FR-CICD-002)."""
    s = run.summary
    executed = s.total - s.skipped_unsafe
    fail_cnt = diff_report.summary_counts.get(FindingSeverity.FAIL.value, 0)
    warn_cnt = diff_report.summary_counts.get(FindingSeverity.WARN.value, 0)
    verdict = diff_report.verdict.value

    findings_by_tool: dict[str, list[RegressionFinding]] = defaultdict(list)
    for f in diff_report.findings:
        findings_by_tool[f.tool_name].append(f)
    impacts_by_tool: dict[str, list[AgentImpactEntry]] = defaultdict(list)
    for entry in impact_report.agent_impacts:
        impacts_by_tool[entry.tool_name].append(entry)
    affected_agents = {
        e.agent_id for e in impact_report.agent_impacts if e.impact_status.value != "UNAFFECTED"
    }
    all_tools = sorted({c.tool_name for c in baseline.cases} | set(findings_by_tool))

    if verdict == "PASS":
        title = f"{_SEV_ICON['PASS']} Regrip: no behavioral regressions"
    else:
        counts = [_plural(fail_cnt, "failure")] if fail_cnt else []
        if warn_cnt:
            counts.append(_plural(warn_cnt, "warning"))
        title = (
            f"{_SEV_ICON[verdict]} Regrip: {' and '.join(counts)} in "
            f"{_plural(len(findings_by_tool), 'tool')} \u00b7 "
            f"{_plural(len(affected_agents), 'agent')} affected"
        )

    lines = [
        "<!-- regrip-report -->",
        f"## {title}",
        "",
        "| | |",
        "| :--- | :--- |",
        f"| **Baseline** | `{baseline.server_name} @ {baseline.tag}` |",
        f"| **Candidate** | `{_cell(run.target_uri)}` |",
        f"| **Safe replay** | {executed}/{s.total} cases executed"
        f" \u00b7 {s.skipped_unsafe} write-blocked skipped |",
        "",
        *_render_tools_section(all_tools, findings_by_tool, impacts_by_tool),
    ]
    if diff_report.findings:
        lines.extend(_render_findings_details(findings_by_tool))
    lines.extend(_render_agent_impact_section(impact_report))
    lines.append("<sub>Deterministic result \u2014 no LLM was used to decide pass/fail.</sub>")
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
