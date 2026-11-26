"""Pure Behavioral Diff Engine orchestrating all deterministic detectors (NFR-REL-002)."""

from __future__ import annotations

from regrip.core.detectors.ordering_default_detector import detect_ordering_and_default_changes
from regrip.core.detectors.schema_detector import detect_schema_changes
from regrip.core.detectors.semantic_detector import detect_error_and_semantic_changes
from regrip.core.detectors.structural_detector import detect_structural_and_type_changes
from regrip.core.detectors.value_detector import detect_value_and_unit_changes
from regrip.types.baseline import BaselineCase, BehavioralBaseline, ReplayCaseStatus, ReplayRun
from regrip.types.config import ComparisonConfig
from regrip.types.diff import DiffReport, FindingCategory, FindingSeverity, RegressionFinding
from regrip.types.envelope import ResponsePayload


def _extract_candidate_payload(resp: ResponsePayload | None) -> object:
    if resp is None:
        return None
    if resp.structured_content is not None:
        return resp.structured_content
    return resp.parsed_text_json


def _diff_case(
    case: BaselineCase,
    candidate_response: ResponsePayload,
    *,
    noise_masks: set[str],
    comparison: ComparisonConfig,
) -> list[RegressionFinding]:
    override = comparison.tool_overrides.get(case.tool_name)
    tolerance = (
        override.numeric_relative_tolerance
        if override and override.numeric_relative_tolerance is not None
        else comparison.numeric_relative_tolerance
    )
    order_sensitive = (
        override.order_sensitive
        if override and override.order_sensitive is not None
        else comparison.default_order_sensitive
    )
    assertions = override.assertions if override else []
    cand_payload = _extract_candidate_payload(candidate_response)

    err_findings = detect_error_and_semantic_changes(
        case,
        candidate_response,
        cand_payload,
        assertions=assertions,
        text_similarity_min=comparison.text_similarity_min,
    )
    if any(f.category == FindingCategory.ERROR for f in err_findings):
        return err_findings

    findings = list(err_findings)
    findings.extend(detect_structural_and_type_changes(case, cand_payload, noise_masks=noise_masks))
    findings.extend(
        detect_value_and_unit_changes(
            case,
            cand_payload,
            noise_masks=noise_masks,
            numeric_tolerance=tolerance,
            detect_unit_scaling=comparison.detect_unit_scaling,
        )
    )
    findings.extend(
        detect_ordering_and_default_changes(
            case,
            cand_payload,
            order_sensitive=order_sensitive,
            kendall_tau_min=override.kendall_tau_min if override else 0.70,
            top_k_critical=override.top_k_critical if override else 1,
        )
    )
    return findings


def _finalize_report(
    baseline_tag: str,
    target_label: str,
    findings: list[RegressionFinding],
) -> DiffReport:
    ordered = sorted(
        findings,
        key=lambda f: (
            f.tool_name,
            f.cluster_key,
            f.category.value,
            f.json_path,
            f.deterministic_reason,
        ),
    )
    counts = {sev.value: 0 for sev in FindingSeverity}
    for f in ordered:
        counts[f.severity.value] += 1
    if counts[FindingSeverity.FAIL.value] > 0:
        verdict = FindingSeverity.FAIL
    elif counts[FindingSeverity.WARN.value] > 0:
        verdict = FindingSeverity.WARN
    else:
        verdict = FindingSeverity.PASS
    return DiffReport(
        baseline_tag=baseline_tag,
        target_label=target_label,
        verdict=verdict,
        findings=ordered,
        summary_counts=counts,
    )


def diff_baseline_against_run(
    baseline: BehavioralBaseline,
    run: ReplayRun,
    comparison: ComparisonConfig,
) -> DiffReport:
    """Compare a BehavioralBaseline against a ReplayRun deterministically."""
    noise_masks = set(baseline.noise_masks)
    case_map = {c.case_id: c for c in baseline.cases}
    findings: list[RegressionFinding] = []
    if baseline.tool_schemas and run.candidate_tool_schemas:
        findings.extend(detect_schema_changes(baseline.tool_schemas, run.candidate_tool_schemas))

    for res in run.results:
        if res.status == ReplayCaseStatus.SKIPPED_UNSAFE:
            continue
        if res.side_effect_note:
            findings.append(
                RegressionFinding(
                    tool_name=res.tool_name,
                    cluster_key=res.cluster_key,
                    category=FindingCategory.SIDE_EFFECT,
                    severity=FindingSeverity.FAIL,
                    json_path="$",
                    deterministic_reason=res.side_effect_note,
                )
            )
        case = case_map.get(res.case_id)
        if case is None or res.candidate_response is None:
            continue
        findings.extend(
            _diff_case(
                case,
                res.candidate_response,
                noise_masks=noise_masks,
                comparison=comparison,
            )
        )
    return _finalize_report(baseline.tag, run.target_uri, findings)


def diff_baselines(
    baseline: BehavioralBaseline,
    candidate: BehavioralBaseline,
    comparison: ComparisonConfig,
) -> DiffReport:
    """Compare two BehavioralBaselines (schemas + matching cluster cases) deterministically."""
    findings = detect_schema_changes(baseline.tool_schemas, candidate.tool_schemas)
    noise_masks = set(baseline.noise_masks) | set(candidate.noise_masks)
    cand_by_key = {(c.tool_name, c.cluster_key): c for c in candidate.cases}

    for base_case in baseline.cases:
        cand_case = cand_by_key.get((base_case.tool_name, base_case.cluster_key))
        if cand_case is None:
            findings.append(
                RegressionFinding(
                    tool_name=base_case.tool_name,
                    cluster_key=base_case.cluster_key,
                    category=FindingCategory.OUTPUT,
                    severity=FindingSeverity.WARN,
                    json_path="$",
                    deterministic_reason="Baseline cluster missing in candidate baseline",
                )
            )
            continue
        cand_resp = ResponsePayload(
            status=cand_case.expected_status,
            is_error=cand_case.expected_is_error,
            structured_content=cand_case.expected_payload
            if isinstance(cand_case.expected_payload, (dict, list))
            else None,
        )
        findings.extend(
            _diff_case(base_case, cand_resp, noise_masks=noise_masks, comparison=comparison)
        )
    return _finalize_report(baseline.tag, candidate.tag, findings)
