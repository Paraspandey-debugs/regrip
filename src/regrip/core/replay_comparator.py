"""Pure Phase-1 structural and behavioral comparison for replay cases."""

from __future__ import annotations

from typing import Any

from regrip.core.payload_normalizer import flatten_json_paths, infer_value_type
from regrip.types.baseline import (
    BaselineCase,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplaySummary,
)
from regrip.types.config import ComparisonConfig
from regrip.types.envelope import ResponsePayload


def _effective_candidate_payload(candidate: ResponsePayload) -> Any:
    if candidate.structured_content is not None:
        return candidate.structured_content
    return candidate.parsed_text_json


def _values_compatible(
    baseline_val: Any,
    candidate_val: Any,
    tolerance: float,
) -> bool:
    if isinstance(baseline_val, (int, float)) and not isinstance(baseline_val, bool):
        if isinstance(candidate_val, (int, float)) and not isinstance(candidate_val, bool):
            denom = max(abs(float(baseline_val)), 1e-9)
            return abs(float(candidate_val) - float(baseline_val)) / denom <= tolerance
        return False
    return baseline_val == candidate_val


def _compare_payloads(
    baseline_payload: Any,
    candidate_payload: Any,
    noise_masks: set[str],
    tolerance: float,
) -> list[str]:
    if baseline_payload is None and candidate_payload is None:
        return []
    if (baseline_payload is None) != (candidate_payload is None):
        return ["$"]

    base_flat = flatten_json_paths(baseline_payload)
    cand_flat = flatten_json_paths(candidate_payload)
    diff_paths: list[str] = []

    for path, base_val in base_flat.items():
        if path in noise_masks:
            continue
        if path not in cand_flat:
            diff_paths.append(path)
            continue
        cand_val = cand_flat[path]
        if infer_value_type(base_val) != infer_value_type(cand_val):
            diff_paths.append(path)
            continue
        if not isinstance(base_val, (dict, list)) and not _values_compatible(
            base_val, cand_val, tolerance
        ):
            diff_paths.append(path)

    return sorted(set(diff_paths))


def compare_case_response(
    case: BaselineCase,
    candidate: ResponsePayload,
    *,
    noise_masks: list[str],
    comparison: ComparisonConfig,
    duration_ms: float,
) -> ReplayCaseResult:
    """Compare a BaselineCase against a candidate ResponsePayload."""
    if case.expected_is_error != candidate.is_error or case.expected_status != candidate.status:
        return ReplayCaseResult(
            case_id=case.case_id,
            tool_name=case.tool_name,
            cluster_key=case.cluster_key,
            status=ReplayCaseStatus.FAIL,
            reason=(
                f"Status/error mismatch: expected {case.expected_status.value} "
                f"(is_error={case.expected_is_error}), got {candidate.status.value} "
                f"(is_error={candidate.is_error})"
            ),
            duration_ms=duration_ms,
            baseline_status=case.expected_status,
            candidate_status=candidate.status,
            diff_paths=["$.status"],
        )

    cand_payload = _effective_candidate_payload(candidate)
    diff_paths = _compare_payloads(
        case.expected_payload,
        cand_payload,
        set(noise_masks),
        comparison.numeric_relative_tolerance,
    )
    if diff_paths:
        return ReplayCaseResult(
            case_id=case.case_id,
            tool_name=case.tool_name,
            cluster_key=case.cluster_key,
            status=ReplayCaseStatus.FAIL,
            reason=f"Payload/type regression at {len(diff_paths)} path(s)",
            duration_ms=duration_ms,
            baseline_status=case.expected_status,
            candidate_status=candidate.status,
            diff_paths=diff_paths,
        )

    return ReplayCaseResult(
        case_id=case.case_id,
        tool_name=case.tool_name,
        cluster_key=case.cluster_key,
        status=ReplayCaseStatus.PASS,
        reason="Matched baseline structure and values within tolerance",
        duration_ms=duration_ms,
        baseline_status=case.expected_status,
        candidate_status=candidate.status,
    )


def summarize_results(results: list[ReplayCaseResult]) -> ReplaySummary:
    """Aggregate case results into a ReplaySummary."""
    counts = {status: 0 for status in ReplayCaseStatus}
    for res in results:
        counts[res.status] += 1
    return ReplaySummary(
        total=len(results),
        passed=counts[ReplayCaseStatus.PASS],
        failed=counts[ReplayCaseStatus.FAIL],
        warned=counts[ReplayCaseStatus.WARN],
        skipped_unsafe=counts[ReplayCaseStatus.SKIPPED_UNSAFE],
        errored=counts[ReplayCaseStatus.ERROR],
    )
