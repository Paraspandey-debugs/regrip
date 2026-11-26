"""Numeric tolerance, scale-factor/unit shift, and distribution drift detectors (FR-CMP-004)."""

from __future__ import annotations

import math
import statistics
from typing import Any

from regrip.core.payload_normalizer import flatten_json_paths
from regrip.types.baseline import BaselineCase
from regrip.types.diff import FindingCategory, FindingSeverity, RegressionFinding

_POWER_OF_10_FACTORS = (10.0, 100.0, 1000.0, 0.1, 0.01, 0.001)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nearest_power_of_10(ratio: float) -> float | None:
    for factor in _POWER_OF_10_FACTORS:
        if abs(ratio - factor) / factor <= 0.05:
            return factor
    return None


def compute_psi(baseline_vals: list[float], candidate_vals: list[float], bins: int = 5) -> float:
    """Compute Population Stability Index (PSI) across two numeric sample lists."""
    if len(baseline_vals) < 5 or len(candidate_vals) < 5:
        return 0.0
    lo = min(min(baseline_vals), min(candidate_vals))
    hi = max(max(baseline_vals), max(candidate_vals))
    if hi <= lo:
        return 0.0

    step = (hi - lo) / bins
    eps = 1e-6
    psi = 0.0
    for i in range(bins):
        low_b = lo + i * step
        high_b = lo + (i + 1) * step + (1e-9 if i == bins - 1 else 0.0)
        b_pct = max(sum(1 for x in baseline_vals if low_b <= x < high_b) / len(baseline_vals), eps)
        c_pct = max(
            sum(1 for x in candidate_vals if low_b <= x < high_b) / len(candidate_vals), eps
        )
        psi += (c_pct - b_pct) * math.log(c_pct / b_pct)
    return psi


def detect_value_and_unit_changes(
    case: BaselineCase,
    candidate_payload: Any,
    *,
    noise_masks: set[str],
    numeric_tolerance: float,
    detect_unit_scaling: bool = True,
) -> list[RegressionFinding]:
    """Detect numeric tolerance violations and consistent power-of-10 unit shifts."""
    if case.expected_payload is None or candidate_payload is None:
        return []

    base_flat = flatten_json_paths(case.expected_payload)
    cand_flat = flatten_json_paths(candidate_payload)
    findings: list[RegressionFinding] = []
    ratios: list[tuple[str, float, float, float]] = []

    for path in sorted(base_flat):
        if path in noise_masks or path not in cand_flat:
            continue
        b_val, c_val = base_flat[path], cand_flat[path]
        if not (_is_number(b_val) and _is_number(c_val)):
            continue
        bf, cf = float(b_val), float(c_val)
        denom = max(abs(bf), 1e-9)
        rel_delta = abs(cf - bf) / denom
        if bf != 0.0 and cf != 0.0:
            ratios.append((path, cf / bf, bf, cf))
        if rel_delta > numeric_tolerance:
            factor = _nearest_power_of_10(cf / bf) if (detect_unit_scaling and bf != 0.0) else None
            reason = (
                f"Likely unit/scale shift ({factor:g}x): {bf} -> {cf}"
                if factor is not None
                else f"Numeric delta {rel_delta * 100:.1f}% exceeds tolerance "
                f"{numeric_tolerance * 100:.1f}% ({bf} -> {cf})"
            )
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.VALUE_UNIT,
                    severity=FindingSeverity.FAIL,
                    json_path=path,
                    baseline_excerpt=b_val,
                    candidate_excerpt=c_val,
                    deterministic_reason=reason,
                )
            )

    if detect_unit_scaling and len(ratios) >= 2:
        raw_ratios = [r[1] for r in ratios]
        mean_r = statistics.mean(raw_ratios)
        cv = statistics.pstdev(raw_ratios) / abs(mean_r) if mean_r != 0 else 1.0
        factor = _nearest_power_of_10(mean_r)
        if factor is not None and cv < 0.05:
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.VALUE_UNIT,
                    severity=FindingSeverity.FAIL,
                    json_path=ratios[0][0],
                    baseline_excerpt=ratios[0][2],
                    candidate_excerpt=ratios[0][3],
                    deterministic_reason=(
                        f"Consistent {factor:g}x scale factor across {len(ratios)} numeric fields "
                        f"(CV={cv:.3f})"
                    ),
                )
            )
    return findings
