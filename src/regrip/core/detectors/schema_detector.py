"""Schema diff detector for tools/list inputSchema and outputSchema (FR-CMP-001)."""

from __future__ import annotations

from typing import Any

from regrip.types.baseline import ToolDescriptor
from regrip.types.diff import FindingCategory, FindingSeverity, RegressionFinding


def _extract_properties(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not schema:
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    return {k: v for k, v in props.items() if isinstance(k, str) and isinstance(v, dict)}


def _extract_required(schema: dict[str, Any] | None) -> set[str]:
    if not schema:
        return set()
    req = schema.get("required")
    if not isinstance(req, list):
        return set()
    return {x for x in req if isinstance(x, str)}


def _check_input_schema(
    tool_name: str, base_in: dict[str, Any], cand_in: dict[str, Any]
) -> list[RegressionFinding]:
    findings: list[RegressionFinding] = []
    base_props = _extract_properties(base_in)
    cand_props = _extract_properties(cand_in)
    base_req = _extract_required(base_in)
    cand_req = _extract_required(cand_in)

    for removed in sorted(set(base_props) - set(cand_props)):
        findings.append(
            RegressionFinding(
                tool_name=tool_name,
                category=FindingCategory.SCHEMA,
                severity=FindingSeverity.FAIL,
                json_path=f"$.inputSchema.properties.{removed}",
                baseline_excerpt=base_props[removed],
                candidate_excerpt=None,
                deterministic_reason=f"Input parameter '{removed}' was removed",
            )
        )

    for newly_req in sorted(cand_req - base_req):
        findings.append(
            RegressionFinding(
                tool_name=tool_name,
                category=FindingCategory.SCHEMA,
                severity=FindingSeverity.FAIL,
                json_path=f"$.inputSchema.required.{newly_req}",
                baseline_excerpt=False,
                candidate_excerpt=True,
                deterministic_reason=f"Input parameter '{newly_req}' became required",
            )
        )

    for shared in sorted(set(base_props) & set(cand_props)):
        b_enum = base_props[shared].get("enum")
        c_enum = cand_props[shared].get("enum")
        if isinstance(b_enum, list) and isinstance(c_enum, list):
            dropped_vals = [v for v in b_enum if v not in c_enum]
            if dropped_vals:
                findings.append(
                    RegressionFinding(
                        tool_name=tool_name,
                        category=FindingCategory.SCHEMA,
                        severity=FindingSeverity.FAIL,
                        json_path=f"$.inputSchema.properties.{shared}.enum",
                        baseline_excerpt=b_enum,
                        candidate_excerpt=c_enum,
                        deterministic_reason=f"Enum narrowed on '{shared}'; dropped {dropped_vals}",
                    )
                )
    return findings


def _check_output_schema(
    tool_name: str, base_out: dict[str, Any] | None, cand_out: dict[str, Any] | None
) -> list[RegressionFinding]:
    if not base_out or not cand_out:
        return []
    findings: list[RegressionFinding] = []
    base_props = _extract_properties(base_out)
    cand_props = _extract_properties(cand_out)
    for removed in sorted(set(base_props) - set(cand_props)):
        findings.append(
            RegressionFinding(
                tool_name=tool_name,
                category=FindingCategory.SCHEMA,
                severity=FindingSeverity.FAIL,
                json_path=f"$.outputSchema.properties.{removed}",
                baseline_excerpt=base_props[removed],
                candidate_excerpt=None,
                deterministic_reason=f"Declared output property '{removed}' was removed",
            )
        )
    for added in sorted(set(cand_props) - set(base_props)):
        findings.append(
            RegressionFinding(
                tool_name=tool_name,
                category=FindingCategory.SCHEMA,
                severity=FindingSeverity.WARN,
                json_path=f"$.outputSchema.properties.{added}",
                baseline_excerpt=None,
                candidate_excerpt=cand_props[added],
                deterministic_reason=f"Declared output property '{added}' was added",
            )
        )
    return findings


def detect_schema_changes(
    baseline_tools: dict[str, ToolDescriptor],
    candidate_tools: dict[str, ToolDescriptor],
) -> list[RegressionFinding]:
    """Compare declared tool schemas between baseline and candidate."""
    findings: list[RegressionFinding] = []
    for name in sorted(baseline_tools):
        base_tool = baseline_tools[name]
        cand_tool = candidate_tools.get(name)
        if cand_tool is None:
            findings.append(
                RegressionFinding(
                    tool_name=name,
                    category=FindingCategory.SCHEMA,
                    severity=FindingSeverity.FAIL,
                    json_path="$",
                    baseline_excerpt=name,
                    candidate_excerpt=None,
                    deterministic_reason=f"Tool '{name}' missing from candidate tools/list",
                )
            )
            continue
        findings.extend(_check_input_schema(name, base_tool.input_schema, cand_tool.input_schema))
        findings.extend(
            _check_output_schema(name, base_tool.output_schema, cand_tool.output_schema)
        )
    return findings
