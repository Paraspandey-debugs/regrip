"""Unit tests for tools/list tool description drift detection (#17)."""

from __future__ import annotations

from regrip.core.detectors import diff_baseline_against_run
from regrip.types.baseline import (
    BaselineCase,
    BehavioralBaseline,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplayRun,
    ReplaySummary,
    ToolDescriptor,
)
from regrip.types.config import ComparisonConfig, ToolComparisonOverride
from regrip.types.diff import FindingCategory, FindingSeverity
from regrip.types.envelope import ResponsePayload, ResponseStatus


def _run_description_diff(
    base_desc: str,
    cand_desc: str,
    comparison: ComparisonConfig | None = None,
) -> tuple[FindingSeverity, list[tuple[FindingSeverity, str, str]]]:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    baseline = BehavioralBaseline(
        server_name="fs-mcp",
        tag="v1",
        tool_schemas={
            "read_file": ToolDescriptor(
                name="read_file", description=base_desc, input_schema=schema
            )
        },
        cases=[
            BaselineCase(
                tool_name="read_file",
                cluster_key="c1",
                expected_status=ResponseStatus.OK,
                expected_payload={"ok": True},
            )
        ],
    )
    run = ReplayRun(
        baseline_id=baseline.baseline_id,
        baseline_tag="v1",
        target_uri="stdio://cand",
        candidate_tool_schemas={
            "read_file": ToolDescriptor(
                name="read_file", description=cand_desc, input_schema=schema
            )
        },
        results=[
            ReplayCaseResult(
                case_id=baseline.cases[0].case_id,
                tool_name="read_file",
                cluster_key="c1",
                status=ReplayCaseStatus.PASS,
                baseline_status=ResponseStatus.OK,
                reason="Matched",
                candidate_response=ResponsePayload(
                    status=ResponseStatus.OK, structured_content={"ok": True}
                ),
            )
        ],
        summary=ReplaySummary(total=1, passed=1),
    )
    report = diff_baseline_against_run(baseline, run, comparison or ComparisonConfig())
    items = [
        (f.severity, f.json_path, f.deterministic_reason)
        for f in report.findings
        if f.category == FindingCategory.SCHEMA and f.json_path == "$.description"
    ]
    return report.verdict, items


def test_identical_and_whitespace_descriptions_pass() -> None:
    verdict, items = _run_description_diff("Read a file from disk.", "Read  a  file\nfrom disk.")
    assert verdict == FindingSeverity.PASS
    assert items == []


def test_reworded_description_triggers_warn() -> None:
    verdict, items = _run_description_diff(
        "Read the complete contents of a file as UTF-8 text.",
        "Fetch remote inventory metrics from billing API.",
    )
    assert verdict == FindingSeverity.WARN
    assert len(items) == 1
    assert items[0][0] == FindingSeverity.WARN
    assert items[0][1] == "$.description"
    assert "similarity=" in items[0][2]


def test_deprecation_notice_detected_even_with_high_similarity() -> None:
    verdict, items = _run_description_diff(
        "Read the complete contents of a file from the filesystem as text.",
        "Read the complete contents of a file from the filesystem as text. "
        "DEPRECATED: Use read_text_file.",
    )
    assert verdict == FindingSeverity.WARN
    assert len(items) == 1
    assert "deprecation" in items[0][2].lower()


def test_description_removed_detected() -> None:
    verdict, items = _run_description_diff("Read a file.", "")
    assert verdict == FindingSeverity.WARN
    assert len(items) == 1
    assert "removed" in items[0][2].lower()


def test_policy_fail_and_override_ignore() -> None:
    fail_cfg = ComparisonConfig(description_drift_policy="fail")
    verdict, items = _run_description_diff("Read file.", "Delete file permanently.", fail_cfg)
    assert verdict == FindingSeverity.FAIL
    assert items[0][0] == FindingSeverity.FAIL

    ignore_cfg = ComparisonConfig(
        description_drift_policy="fail",
        tool_overrides={"read_file": ToolComparisonOverride(description_drift_policy="ignore")},
    )
    verdict_ign, items_ign = _run_description_diff(
        "Read file.", "Delete file permanently.", ignore_cfg
    )
    assert verdict_ign == FindingSeverity.PASS
    assert items_ign == []
