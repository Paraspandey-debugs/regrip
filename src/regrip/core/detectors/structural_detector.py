"""Output structure, Jaro-Winkler key rename, and runtime type detectors (FR-CMP-002..003)."""

from __future__ import annotations

from typing import Any

from regrip.core.payload_normalizer import flatten_json_paths, infer_value_type
from regrip.types.baseline import BaselineCase
from regrip.types.diff import FindingCategory, FindingSeverity, RegressionFinding


def jaro_winkler_similarity(s1: str, s2: str) -> float:
    """Compute Jaro-Winkler string similarity in [0.0, 1.0]."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_dist = max(len1, len2) // 2 - 1
    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0

    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if not s2_matches[j] and s1[i] == s2[j]:
                s1_matches[i] = s2_matches[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0

    k = 0
    transpositions = 0
    for i in range(len1):
        if s1_matches[i]:
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1

    jaro = (matches / len1 + matches / len2 + (matches - transpositions / 2) / matches) / 3.0
    prefix = 0
    for c1, c2 in zip(s1, s2, strict=False):
        if c1 == c2 and prefix < 4:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1.0 - jaro)


def _parent_and_key(json_path: str) -> tuple[str, str]:
    if "." not in json_path:
        return "$", json_path
    parent, key = json_path.rsplit(".", 1)
    return parent, key


def _match_renamed_key(
    missing_path: str,
    missing_type: str,
    added_flat: dict[str, Any],
) -> tuple[str, float] | None:
    m_parent, m_key = _parent_and_key(missing_path)
    best: tuple[str, float] | None = None
    for cand_path, cand_val in added_flat.items():
        c_parent, c_key = _parent_and_key(cand_path)
        if c_parent != m_parent:
            continue
        if infer_value_type(cand_val) != missing_type:
            continue
        sim = jaro_winkler_similarity(m_key, c_key)
        if sim >= 0.80 and (best is None or sim > best[1]):
            best = (cand_path, sim)
    return best


def detect_structural_and_type_changes(
    case: BaselineCase,
    candidate_payload: Any,
    *,
    noise_masks: set[str],
) -> list[RegressionFinding]:
    """Detect missing fields, renamed keys, and runtime type regressions."""
    if case.expected_payload is None and candidate_payload is None:
        return []
    if case.expected_payload is not None and candidate_payload is None:
        return [
            RegressionFinding(
                tool_name=case.tool_name,
                cluster_key=case.cluster_key,
                category=FindingCategory.OUTPUT,
                severity=FindingSeverity.FAIL,
                json_path="$",
                baseline_excerpt=case.expected_payload,
                candidate_excerpt=None,
                deterministic_reason="Candidate produced empty payload where baseline had data",
            )
        ]

    base_flat = flatten_json_paths(case.expected_payload)
    cand_flat = flatten_json_paths(candidate_payload)
    added_only = {k: v for k, v in cand_flat.items() if k not in base_flat and k not in noise_masks}
    findings: list[RegressionFinding] = []

    for path in sorted(base_flat):
        if path in noise_masks:
            continue
        base_val = base_flat[path]
        base_type = infer_value_type(base_val)
        if path not in cand_flat:
            profile = case.expected_schema_profile.get(path)
            presence = profile.presence_ratio if profile else 1.0
            rename = _match_renamed_key(path, base_type, added_only)
            if rename is not None:
                cand_path, sim = rename
                findings.append(
                    RegressionFinding(
                        tool_name=case.tool_name,
                        cluster_key=case.cluster_key,
                        category=FindingCategory.OUTPUT,
                        severity=FindingSeverity.FAIL,
                        json_path=path,
                        baseline_excerpt=path,
                        candidate_excerpt=cand_path,
                        deterministic_reason=(
                            f"Likely key rename '{path}' -> '{cand_path}' (Jaro-Winkler={sim:.2f})"
                        ),
                    )
                )
            elif presence >= 0.95:
                findings.append(
                    RegressionFinding(
                        tool_name=case.tool_name,
                        cluster_key=case.cluster_key,
                        category=FindingCategory.OUTPUT,
                        severity=FindingSeverity.FAIL,
                        json_path=path,
                        baseline_excerpt=base_val,
                        candidate_excerpt=None,
                        deterministic_reason=(
                            f"Field '{path}' missing (present in {presence * 100:.0f}% of baseline)"
                        ),
                    )
                )
            continue

        cand_val = cand_flat[path]
        cand_type = infer_value_type(cand_val)
        if base_type != cand_type:
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.TYPE,
                    severity=FindingSeverity.FAIL,
                    json_path=path,
                    baseline_excerpt=base_type,
                    candidate_excerpt=cand_type,
                    deterministic_reason=f"Type changed at '{path}': {base_type} -> {cand_type}",
                )
            )
    return findings
