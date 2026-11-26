"""Consumer-aware field lineage, per-agent impact matrix, and blast radius (FR-IMP-001..003)."""

from __future__ import annotations

from collections import defaultdict

from regrip.core.payload_normalizer import flatten_json_paths
from regrip.types.config import ConsumerAgentConfig
from regrip.types.diff import DiffReport, FindingCategory, FindingSeverity, RegressionFinding
from regrip.types.envelope import InteractionEnvelope
from regrip.types.impact import (
    AgentImpactEntry,
    AgentImpactStatus,
    ConsumerContract,
    ContractDiscoverySource,
    ImpactReport,
    ToolBlastRadius,
)


def _paths_overlap(consumed: str, changed: str) -> bool:
    if consumed == changed:
        return True
    norm_c = consumed.replace("[*]", "")
    norm_d = changed.replace("[*]", "")
    return norm_c == norm_d or norm_d.startswith(f"{norm_c}.") or norm_c.startswith(f"{norm_d}.")


def infer_session_correlation_contracts(
    envelopes: list[InteractionEnvelope],
) -> list[ConsumerContract]:
    """Infer field lineage when output values of call t1 feed inputs of call t2 in a session."""
    by_session: dict[str, list[InteractionEnvelope]] = defaultdict(list)
    for env in envelopes:
        if env.actor.session_id:
            by_session[env.actor.session_id].append(env)

    discovered: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sess_envs in by_session.values():
        ordered = sorted(sess_envs, key=lambda e: e.timestamp_utc)
        for idx, prev in enumerate(ordered):
            payload = (
                prev.response.structured_content
                if prev.response.structured_content is not None
                else prev.response.parsed_text_json
            )
            if payload is None:
                continue
            out_flat = flatten_json_paths(payload)
            future_arg_vals: set[str] = set()
            for nxt in ordered[idx + 1 :]:
                for v in flatten_json_paths(nxt.request.arguments).values():
                    if not isinstance(v, (dict, list)) and v is not None:
                        future_arg_vals.add(str(v))
            for path, val in out_flat.items():
                if isinstance(val, (dict, list)) or val is None:
                    continue
                sval = str(val)
                if len(sval) >= 2 and sval in future_arg_vals:
                    discovered[(prev.actor.agent_id, prev.operation.tool_name)].add(path)

    return [
        ConsumerContract(
            agent_id=agent_id,
            tool_name=tool_name,
            consumed_jsonpaths=sorted(paths),
            discovery_source=ContractDiscoverySource.SESSION_CORRELATION,
        )
        for (agent_id, tool_name), paths in sorted(discovered.items())
    ]


def merge_consumer_contracts(
    yaml_consumers: list[ConsumerAgentConfig],
    extra_contracts: list[ConsumerContract],
    session_contracts: list[ConsumerContract],
) -> list[ConsumerContract]:
    """Merge contracts in priority order: YAML > SDK > Dynamic > Session Correlation."""
    merged: dict[tuple[str, str], ConsumerContract] = {}

    for c in session_contracts:
        merged[(c.agent_id, c.tool_name)] = c

    for c in extra_contracts:
        key = (c.agent_id, c.tool_name)
        if key not in merged or c.discovery_source != ContractDiscoverySource.SESSION_CORRELATION:
            merged[key] = c

    for agent_cfg in yaml_consumers:
        for tool_name, tool_cfg in agent_cfg.tools.items():
            merged[(agent_cfg.agent_id, tool_name)] = ConsumerContract(
                agent_id=agent_cfg.agent_id,
                tool_name=tool_name,
                consumed_jsonpaths=list(tool_cfg.consumed_jsonpaths),
                order_sensitive=tool_cfg.order_sensitive,
                discovery_source=ContractDiscoverySource.EXPLICIT_YAML,
            )
    return [merged[k] for k in sorted(merged)]


def _evaluate_finding_for_contract(
    finding: RegressionFinding,
    contract: ConsumerContract,
) -> tuple[AgentImpactStatus, list[str]]:
    if finding.severity == FindingSeverity.PASS:
        return AgentImpactStatus.UNAFFECTED, []

    if finding.category in (FindingCategory.ERROR, FindingCategory.SIDE_EFFECT):
        return AgentImpactStatus.CONFIRMED_REGRESSION, [finding.json_path]

    if finding.category == FindingCategory.ORDERING:
        if contract.order_sensitive:
            return AgentImpactStatus.CONFIRMED_REGRESSION, [finding.json_path]
        return AgentImpactStatus.UNAFFECTED, []

    if not contract.consumed_jsonpaths:
        return AgentImpactStatus.POTENTIALLY_BROKEN, []

    matched = [p for p in contract.consumed_jsonpaths if _paths_overlap(p, finding.json_path)]
    if matched:
        if contract.discovery_source == ContractDiscoverySource.SESSION_CORRELATION:
            return AgentImpactStatus.POTENTIALLY_BROKEN, matched
        return AgentImpactStatus.CONFIRMED_REGRESSION, matched
    return AgentImpactStatus.UNAFFECTED, []


def _status_rank(status: AgentImpactStatus) -> int:
    if status == AgentImpactStatus.CONFIRMED_REGRESSION:
        return 2
    if status == AgentImpactStatus.POTENTIALLY_BROKEN:
        return 1
    return 0


def compute_consumer_impact(
    diff_report: DiffReport,
    contracts: list[ConsumerContract],
    *,
    call_volumes: dict[str, int] | None = None,
) -> ImpactReport:
    """Map DiffReport findings onto ConsumerContracts and compute blast radii."""
    findings_by_tool: dict[str, list[RegressionFinding]] = defaultdict(list)
    for f in diff_report.findings:
        findings_by_tool[f.tool_name].append(f)

    entries: list[AgentImpactEntry] = []
    contracts_by_tool: dict[str, list[ConsumerContract]] = defaultdict(list)
    for c in contracts:
        contracts_by_tool[c.tool_name].append(c)

    for contract in contracts:
        worst = AgentImpactStatus.UNAFFECTED
        matched_paths: set[str] = set()
        reasons: list[str] = []
        for finding in findings_by_tool.get(contract.tool_name, []):
            status, paths = _evaluate_finding_for_contract(finding, contract)
            if _status_rank(status) > _status_rank(worst):
                worst = status
            if status != AgentImpactStatus.UNAFFECTED:
                matched_paths.update(paths)
                reasons.append(finding.deterministic_reason)

        entries.append(
            AgentImpactEntry(
                agent_id=contract.agent_id,
                tool_name=contract.tool_name,
                impact_status=worst,
                matched_jsonpaths=sorted(matched_paths),
                reasons=reasons,
            )
        )

    volumes = call_volumes or {}
    blast_radii: list[ToolBlastRadius] = []
    for tool_name in sorted(set(findings_by_tool) | set(contracts_by_tool)):
        tool_entries = [e for e in entries if e.tool_name == tool_name]
        total_agents = len(tool_entries)
        affected = sum(1 for e in tool_entries if e.impact_status != AgentImpactStatus.UNAFFECTED)
        ratio = round(affected / total_agents, 4) if total_agents > 0 else 0.0
        vol = volumes.get(tool_name, 1)
        blast_radii.append(
            ToolBlastRadius(
                tool_name=tool_name,
                affected_agents=affected,
                total_agents=total_agents,
                blast_radius=ratio,
                call_volume=vol,
                risk_score=round(ratio * vol, 2),
            )
        )

    blast_radii.sort(key=lambda b: (-b.risk_score, b.tool_name))
    return ImpactReport(
        baseline_tag=diff_report.baseline_tag,
        target_label=diff_report.target_label,
        agent_impacts=entries,
        blast_radii=blast_radii,
    )
