"""Unit tests for Consumer Impact Engine, session correlation, and report exporters."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from regrip.core.consumer_impact import (
    compute_consumer_impact,
    infer_session_correlation_contracts,
    merge_consumer_contracts,
)
from regrip.core.report_exporters import (
    render_json_report,
    render_junit_xml,
    render_markdown_pr_comment,
)
from regrip.types import (
    ActorInfo,
    AgentImpactStatus,
    BaselineCase,
    BehavioralBaseline,
    ConsumerAgentConfig,
    ConsumerToolConfig,
    DiffReport,
    FindingCategory,
    FindingSeverity,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegressionFinding,
    ReplayRun,
    ReplaySummary,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def test_three_source_lineage_and_per_agent_verdicts() -> None:
    t0 = datetime.now(UTC)
    env1 = InteractionEnvelope(
        timestamp_utc=t0,
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="agent-b", session_id="s-1"),
        target=TargetInfo(server_name="catalog-mcp"),
        operation=OperationInfo(tool_name="search_products"),
        request=RequestPayload(arguments={"query": "laptop"}),
        response=ResponsePayload(
            status=ResponseStatus.OK,
            structured_content={"results": [{"id": "sku-99", "description": "fast"}]},
        ),
        telemetry=TelemetryInfo(duration_ms=5.0),
    )
    env2 = InteractionEnvelope(
        timestamp_utc=t0 + timedelta(seconds=1),
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="agent-b", session_id="s-1"),
        target=TargetInfo(server_name="catalog-mcp"),
        operation=OperationInfo(tool_name="get_product"),
        request=RequestPayload(arguments={"product_id": "sku-99"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"id": "sku-99"}),
        telemetry=TelemetryInfo(duration_ms=3.0),
    )

    session_contracts = infer_session_correlation_contracts([env1, env2])
    assert any(
        c.agent_id == "agent-b" and "$.results[*].id" in c.consumed_jsonpaths
        for c in session_contracts
    )

    yaml_consumers = [
        ConsumerAgentConfig(
            agent_id="agent-a",
            tools={
                "search_products": ConsumerToolConfig(
                    consumed_jsonpaths=["$.results[*].price"], order_sensitive=False
                )
            },
        ),
        ConsumerAgentConfig(
            agent_id="agent-c",
            tools={
                "search_products": ConsumerToolConfig(
                    consumed_jsonpaths=["$.results[*].description"], order_sensitive=True
                )
            },
        ),
    ]
    contracts = merge_consumer_contracts(yaml_consumers, [], session_contracts)
    diff = DiffReport(
        baseline_tag="v1",
        target_label="v2",
        verdict=FindingSeverity.FAIL,
        findings=[
            RegressionFinding(
                tool_name="search_products",
                category=FindingCategory.OUTPUT,
                severity=FindingSeverity.FAIL,
                json_path="$.results[*].description",
                deterministic_reason="Field removed",
            )
        ],
        summary_counts={"FAIL": 1, "WARN": 0, "PASS": 0},
    )
    impact = compute_consumer_impact(diff, contracts, call_volumes={"search_products": 100})
    verdicts = {e.agent_id: e.impact_status for e in impact.agent_impacts}
    assert verdicts["agent-a"] == AgentImpactStatus.UNAFFECTED
    assert verdicts["agent-c"] == AgentImpactStatus.CONFIRMED_REGRESSION

    baseline = BehavioralBaseline(
        server_name="catalog-mcp",
        tag="v1",
        cases=[BaselineCase(tool_name="search_products", cluster_key="c1")],
    )
    run = ReplayRun(
        baseline_id=baseline.baseline_id,
        baseline_tag="v1",
        target_uri="stdio://srv",
        summary=ReplaySummary(total=1, failed=1),
    )
    md = render_markdown_pr_comment(baseline, run, diff, impact)
    assert "<!-- regrip-report -->" in md
    assert "CONFIRMED_REGRESSION" in md
    assert '"verdict": "FAIL"' in render_json_report(baseline, run, diff, impact)
    xml_root = ET.fromstring(render_junit_xml(diff))
    assert xml_root.attrib["failures"] == "1"
