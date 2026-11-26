"""Golden synthetic mutation tests for the 9-category Behavioral Diff Engine."""

from __future__ import annotations

from regrip.core.detectors import diff_baseline_against_run, diff_baselines
from regrip.core.detectors.semantic_detector import evaluate_semantic_assertion
from regrip.core.detectors.value_detector import compute_psi
from regrip.types import (
    BaselineCase,
    BehavioralBaseline,
    ComparisonConfig,
    FindingCategory,
    FindingSeverity,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplayRun,
    ResponsePayload,
    ResponseStatus,
    ToolComparisonOverride,
    ToolDescriptor,
)


def _baseline_with_case(
    tool_name: str,
    payload: object,
    *,
    is_error: bool = False,
    status: ResponseStatus = ResponseStatus.OK,
    tool_schemas: dict[str, ToolDescriptor] | None = None,
) -> BehavioralBaseline:
    case = BaselineCase(
        tool_name=tool_name,
        cluster_key=f"{tool_name}:c1",
        expected_status=status,
        expected_is_error=is_error,
        expected_payload=payload,  # type: ignore[arg-type]
    )
    return BehavioralBaseline(
        server_name="svc",
        tag="v1",
        tool_schemas=tool_schemas or {},
        cases=[case],
    )


def _run_for(baseline: BehavioralBaseline, candidate: ResponsePayload) -> ReplayRun:
    case = baseline.cases[0]
    return ReplayRun(
        baseline_id=baseline.baseline_id,
        baseline_tag=baseline.tag,
        target_uri="stdio://cand",
        results=[
            ReplayCaseResult(
                case_id=case.case_id,
                tool_name=case.tool_name,
                cluster_key=case.cluster_key,
                status=ReplayCaseStatus.FAIL,
                reason="diff",
                baseline_status=case.expected_status,
                candidate_status=candidate.status,
                candidate_response=candidate,
            )
        ],
    )


def test_schema_detector_flags_removed_param_and_narrowed_enum() -> None:
    b1 = _baseline_with_case(
        "search",
        {"ok": True},
        tool_schemas={
            "search": ToolDescriptor(
                name="search",
                input_schema={
                    "properties": {
                        "query": {"type": "string"},
                        "currency": {"type": "string", "enum": ["INR", "USD"]},
                    },
                    "required": ["query"],
                },
            )
        },
    )
    b2 = _baseline_with_case(
        "search",
        {"ok": True},
        tool_schemas={
            "search": ToolDescriptor(
                name="search",
                input_schema={
                    "properties": {
                        "currency": {"type": "string", "enum": ["USD"]},
                        "region": {"type": "string"},
                    },
                    "required": ["region"],
                },
            )
        },
    )
    report = diff_baselines(b1, b2, ComparisonConfig())
    assert report.verdict == FindingSeverity.FAIL
    reasons = " ".join(f.deterministic_reason for f in report.findings)
    assert "query" in reasons
    assert "region" in reasons
    assert "Enum narrowed" in reasons


def test_structural_detector_flags_rename_missing_and_type_shift() -> None:
    b = _baseline_with_case(
        "get_user",
        {"balance": 100, "description": "vip", "age": 30},
    )
    cand = ResponsePayload(
        status=ResponseStatus.OK,
        structured_content={"balances": 100, "age": "30"},
    )
    report = diff_baseline_against_run(b, _run_for(b, cand), ComparisonConfig())
    cats = {f.category for f in report.findings}
    assert FindingCategory.OUTPUT in cats
    assert FindingCategory.TYPE in cats
    reasons = " ".join(f.deterministic_reason for f in report.findings)
    assert "Likely key rename" in reasons
    assert "$.description" in reasons
    assert "int -> numeric_string" in reasons


def test_value_detector_catches_100x_unit_shift_and_psi_drift() -> None:
    b = _baseline_with_case("get_balance", {"balance": 1000, "tax": 50})
    cand = ResponsePayload(
        status=ResponseStatus.OK,
        structured_content={"balance": 100000, "tax": 5000},
    )
    report = diff_baseline_against_run(b, _run_for(b, cand), ComparisonConfig())
    assert report.verdict == FindingSeverity.FAIL
    assert any("100x" in f.deterministic_reason for f in report.findings)

    psi = compute_psi([10.0, 11.0, 12.0, 10.5, 11.5], [100.0, 110.0, 120.0, 105.0, 115.0])
    assert psi > 0.25


def test_ordering_and_default_detectors() -> None:
    b = _baseline_with_case(
        "search_products",
        {
            "results": [
                {"id": "1"},
                {"id": "2"},
                {"id": "3"},
                {"id": "4"},
            ]
        },
    )
    cand = ResponsePayload(
        status=ResponseStatus.OK,
        structured_content={"results": [{"id": "4"}, {"id": "1"}]},
    )
    cfg = ComparisonConfig(
        tool_overrides={
            "search_products": ToolComparisonOverride(order_sensitive=True, top_k_critical=1)
        }
    )
    report = diff_baseline_against_run(b, _run_for(b, cand), cfg)
    cats = {f.category for f in report.findings}
    assert FindingCategory.DEFAULT in cats
    assert FindingCategory.ORDERING in cats
    assert report.verdict == FindingSeverity.FAIL


def test_semantic_assertions_and_error_regression() -> None:
    assert evaluate_semantic_assertion(
        "$.currency in ['INR', 'USD'] and $.total == sum($.items[*].price)",
        {"currency": "INR", "total": 150, "items": [{"price": 100}, {"price": 50}]},
    )
    b = _baseline_with_case("get_balance", {"currency": "INR", "balance": 100})
    cand = ResponsePayload(
        status=ResponseStatus.OK,
        structured_content={"currency": "EUR", "balance": -5},
    )
    cfg = ComparisonConfig(
        tool_overrides={
            "get_balance": ToolComparisonOverride(
                assertions=["$.currency in ['INR', 'USD']", "$.balance >= 0"]
            )
        }
    )
    report = diff_baseline_against_run(b, _run_for(b, cand), cfg)
    sem = [f for f in report.findings if f.category == FindingCategory.SEMANTIC]
    assert len(sem) == 2

    cand_err = ResponsePayload(status=ResponseStatus.TOOL_ERROR, is_error=True)
    report_err = diff_baseline_against_run(b, _run_for(b, cand_err), cfg)
    assert report_err.findings[0].category == FindingCategory.ERROR
