"""Ordering (Kendall tau / Top-K) and omitted-arg default detectors (FR-CMP-005..006)."""

from __future__ import annotations

import json
from typing import Any

from regrip.core.payload_normalizer import flatten_json_paths
from regrip.types.baseline import BaselineCase
from regrip.types.diff import FindingCategory, FindingSeverity, RegressionFinding


def _element_key(item: Any) -> str:
    if isinstance(item, dict):
        for candidate_id in ("id", "order_id", "sku", "name", "key"):
            if candidate_id in item:
                return f"{candidate_id}:{item[candidate_id]}"
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def kendall_tau(seq_a: list[str], seq_b: list[str]) -> float:
    """Compute Kendall's tau-a rank correlation for shared elements in two sequences."""
    pos_b = {key: idx for idx, key in enumerate(seq_b)}
    common = [key for key in seq_a if key in pos_b]
    n = len(common)
    if n < 2:
        return 1.0
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if pos_b[common[i]] < pos_b[common[j]]:
                concordant += 1
            elif pos_b[common[i]] > pos_b[common[j]]:
                discordant += 1
    total_pairs = n * (n - 1) / 2
    return (concordant - discordant) / total_pairs


def detect_ordering_and_default_changes(
    case: BaselineCase,
    candidate_payload: Any,
    *,
    order_sensitive: bool,
    kendall_tau_min: float = 0.70,
    top_k_critical: int = 1,
) -> list[RegressionFinding]:
    """Detect array pagination/default length shifts and rank ordering changes."""
    if case.expected_payload is None or candidate_payload is None:
        return []

    base_flat = flatten_json_paths(case.expected_payload)
    cand_flat = flatten_json_paths(candidate_payload)
    findings: list[RegressionFinding] = []

    for path in sorted(base_flat):
        b_val = base_flat[path]
        c_val = cand_flat.get(path)
        if not (isinstance(b_val, list) and isinstance(c_val, list)):
            continue

        if len(b_val) >= 3 and 0 < len(c_val) < len(b_val) and len(c_val) * 2 <= len(b_val):
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.DEFAULT,
                    severity=FindingSeverity.WARN,
                    json_path=path,
                    baseline_excerpt=len(b_val),
                    candidate_excerpt=len(c_val),
                    deterministic_reason=(
                        f"Array length dropped from {len(b_val)} to {len(c_val)} "
                        "(possible pagination/limit default change)"
                    ),
                )
            )

        if not order_sensitive or len(b_val) < 2 or len(c_val) < 2:
            continue

        keys_a = [_element_key(x) for x in b_val]
        keys_b = [_element_key(x) for x in c_val]
        tau = kendall_tau(keys_a, keys_b)
        top_k_changed = keys_a[:top_k_critical] != keys_b[:top_k_critical]
        if tau < kendall_tau_min or top_k_changed:
            severity = FindingSeverity.FAIL if top_k_changed else FindingSeverity.WARN
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.ORDERING,
                    severity=severity,
                    json_path=path,
                    baseline_excerpt=list(keys_a[:3]),
                    candidate_excerpt=list(keys_b[:3]),
                    deterministic_reason=(
                        f"Array order shifted at '{path}' (Kendall tau={tau:.2f}, "
                        f"top_{top_k_critical}_changed={top_k_changed})"
                    ),
                )
            )
    return findings
