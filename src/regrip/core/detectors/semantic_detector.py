"""Error, safe AST semantic assertion, and unstructured text drift detectors (FR-CMP-007..008)."""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from typing import Any

from regrip.types.baseline import BaselineCase
from regrip.types.diff import FindingCategory, FindingSeverity, RegressionFinding
from regrip.types.envelope import ResponsePayload

_JSONPATH_TOKEN_RE = re.compile(r"\$\.[A-Za-z0-9_.\[\]*]+")
_SUM_CALL_RE = re.compile(r"sum\((\$\.[A-Za-z0-9_.\[\]*]+)\)")


def _resolve_jsonpath(payload: Any, path: str) -> Any:
    tokens = [t for t in path.removeprefix("$.").split(".") if t]
    cur: Any = payload
    for tok in tokens:
        if tok.endswith("[*]"):
            key = tok[:-3]
            if key:
                if not isinstance(cur, dict) or key not in cur:
                    return []
                cur = cur[key]
            if not isinstance(cur, list):
                return []
            continue
        if isinstance(cur, list):
            cur = [item.get(tok) for item in cur if isinstance(item, dict) and tok in item]
        elif isinstance(cur, dict):
            if tok not in cur:
                return None
            cur = cur[tok]
        else:
            return None
    return cur


def _eval_ast_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.List):
        return [_eval_ast_node(e, env) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast_node(node.operand, env)
    if isinstance(node, ast.BinOp):
        left, right = _eval_ast_node(node.left, env), _eval_ast_node(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.BoolOp):
        vals = [_eval_ast_node(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left = _eval_ast_node(node.left, env)
        for op, comp in zip(node.ops, node.comparators, strict=True):
            right = _eval_ast_node(comp, env)
            ok = _apply_compare(op, left, right)
            if not ok:
                return False
            left = right
        return True
    raise ValueError(f"Unsupported assertion AST node: {type(node).__name__}")


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Lt):
        return bool(left < right)
    if isinstance(op, ast.LtE):
        return bool(left <= right)
    if isinstance(op, ast.Gt):
        return bool(left > right)
    if isinstance(op, ast.GtE):
        return bool(left >= right)
    if isinstance(op, ast.In):
        return bool(left in right)
    if isinstance(op, ast.NotIn):
        return bool(left not in right)
    raise ValueError(f"Unsupported comparison operator: {type(op).__name__}")


def evaluate_semantic_assertion(expression: str, payload: Any) -> bool:
    """Safely evaluate a JSONPath invariant like `$.balance >= 0` without eval()."""
    env: dict[str, Any] = {}
    counter = 0

    def _replace_sum(match: re.Match[str]) -> str:
        nonlocal counter
        var = f"__v{counter}"
        counter += 1
        items = _resolve_jsonpath(payload, match.group(1))
        env[var] = sum(float(x) for x in items) if isinstance(items, list) else 0.0
        return var

    transformed = _SUM_CALL_RE.sub(_replace_sum, expression)

    def _replace_path(match: re.Match[str]) -> str:
        nonlocal counter
        var = f"__v{counter}"
        counter += 1
        env[var] = _resolve_jsonpath(payload, match.group(0))
        return var

    transformed = _JSONPATH_TOKEN_RE.sub(_replace_path, transformed)
    tree = ast.parse(transformed, mode="eval")
    return bool(_eval_ast_node(tree.body, env))


def text_cosine_similarity(text_a: str, text_b: str) -> float:
    """Deterministic bag-of-words cosine similarity for unstructured TextContent."""
    tokens_a = Counter(re.findall(r"\w+", text_a.lower()))
    tokens_b = Counter(re.findall(r"\w+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 1.0 if text_a.strip() == text_b.strip() else 0.0
    shared = set(tokens_a) & set(tokens_b)
    dot = sum(tokens_a[t] * tokens_b[t] for t in shared)
    mag_a = math.sqrt(sum(v * v for v in tokens_a.values()))
    mag_b = math.sqrt(sum(v * v for v in tokens_b.values()))
    return dot / (mag_a * mag_b) if mag_a > 0 and mag_b > 0 else 0.0


def detect_error_and_semantic_changes(
    case: BaselineCase,
    candidate_response: ResponsePayload,
    candidate_payload: Any,
    *,
    assertions: list[str],
    text_similarity_min: float = 0.75,
) -> list[RegressionFinding]:
    """Detect error status regressions, assertion failures, and text drift."""
    findings: list[RegressionFinding] = []
    if case.expected_is_error != candidate_response.is_error or (
        case.expected_status != candidate_response.status
    ):
        findings.append(
            RegressionFinding(
                tool_name=case.tool_name,
                cluster_key=case.cluster_key,
                category=FindingCategory.ERROR,
                severity=FindingSeverity.FAIL,
                json_path="$.status",
                baseline_excerpt=case.expected_status.value,
                candidate_excerpt=candidate_response.status.value,
                deterministic_reason=(
                    f"Error state changed: expected {case.expected_status.value} "
                    f"(is_error={case.expected_is_error}), got {candidate_response.status.value} "
                    f"(is_error={candidate_response.is_error})"
                ),
            )
        )
        return findings

    for expr in assertions:
        try:
            passed = evaluate_semantic_assertion(expr, candidate_payload)
        except Exception as exc:  # fail explicitly as semantic assertion failure
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.SEMANTIC,
                    severity=FindingSeverity.FAIL,
                    json_path="$",
                    deterministic_reason=f"Semantic assertion '{expr}' errored: {exc}",
                )
            )
            continue
        if not passed:
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.SEMANTIC,
                    severity=FindingSeverity.FAIL,
                    json_path="$",
                    candidate_excerpt=candidate_payload,
                    deterministic_reason=f"Semantic assertion failed: {expr}",
                )
            )

    if isinstance(case.expected_payload, str) and candidate_response.content_blocks:
        cand_text = " ".join(
            str(b.get("text", ""))
            for b in candidate_response.content_blocks
            if b.get("type") == "text"
        )
        sim = text_cosine_similarity(case.expected_payload, cand_text)
        if sim < text_similarity_min:
            findings.append(
                RegressionFinding(
                    tool_name=case.tool_name,
                    cluster_key=case.cluster_key,
                    category=FindingCategory.SEMANTIC,
                    severity=FindingSeverity.WARN,
                    json_path="$.content[0].text",
                    deterministic_reason=(
                        f"Unstructured text similarity {sim:.2f} below threshold "
                        f"{text_similarity_min:.2f}"
                    ),
                )
            )
    return findings
