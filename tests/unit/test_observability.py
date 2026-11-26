"""Unit tests for historical analyzer, OTel GenAI span exporter, and advisory LLM explainer."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from regrip.adapters.advisory_llm import AdvisoryExplainer
from regrip.adapters.otel_exporter import envelope_to_otel_span, export_spans_ndjson
from regrip.core.history_analyzer import build_tool_history
from regrip.types import (
    ActorInfo,
    AdvisoryLLMConfig,
    DiffReport,
    FindingCategory,
    FindingSeverity,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegressionFinding,
    ReplayCaseResult,
    ReplayCaseStatus,
    ReplayRun,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def _sample_env() -> InteractionEnvelope:
    return InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="pricing-bot", session_id="s-9"),
        target=TargetInfo(server_name="finance-mcp"),
        operation=OperationInfo(tool_name="get_balance"),
        request=RequestPayload(arguments={"id": "acct-1"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"balance": 100}),
        telemetry=TelemetryInfo(
            duration_ms=12.0,
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
    )


def test_history_analyzer_and_otel_span_export(tmp_path: Path) -> None:
    env = _sample_env()
    span = envelope_to_otel_span(env)
    assert span["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span["attributes"]["gen_ai.operation.name"] == "execute_tool"
    assert span["attributes"]["gen_ai.tool.name"] == "get_balance"
    assert span["attributes"]["mcp.method.name"] == "tools/call"

    ndjson_path = export_spans_ndjson([env], tmp_path / "spans.ndjson")
    assert json.loads(ndjson_path.read_text(encoding="utf-8").strip())["name"] == (
        "execute_tool get_balance"
    )

    run = ReplayRun(
        baseline_id=env.interaction_id,
        baseline_tag="v1.0",
        target_uri="stdio://srv",
        target_git_sha="abc1234",
        results=[
            ReplayCaseResult(
                case_id=env.interaction_id,
                tool_name="get_balance",
                cluster_key="c1",
                status=ReplayCaseStatus.FAIL,
                reason="100x shift",
                baseline_status=ResponseStatus.OK,
            )
        ],
    )
    hist = build_tool_history("get_balance", [env], [run])
    assert hist.total_recorded_calls == 1
    assert hist.dependent_agents == ["pricing-bot"]
    assert hist.timeline[0].failure_rate == 1.0
    assert hist.timeline[0].target_git_sha == "abc1234"


async def test_advisory_explainer_annotates_without_changing_verdict(monkeypatch) -> None:
    monkeypatch.setenv("REGRIP_LLM_API_KEY", "test-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Values scaled 100x (INR to paise)."}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    explainer = AdvisoryExplainer(AdvisoryLLMConfig(enabled=True), http_client=client)
    report = DiffReport(
        baseline_tag="v1",
        target_label="v2",
        verdict=FindingSeverity.WARN,
        findings=[
            RegressionFinding(
                tool_name="get_balance",
                category=FindingCategory.VALUE_UNIT,
                severity=FindingSeverity.WARN,
                json_path="$.balance",
                deterministic_reason="100x shift",
            )
        ],
        summary_counts={"FAIL": 0, "WARN": 1, "PASS": 0},
    )
    annotated = await explainer.annotate_report(report)
    await client.aclose()
    assert annotated.verdict == FindingSeverity.WARN
    assert annotated.findings[0].severity == FindingSeverity.WARN
    assert annotated.findings[0].llm_advisory_note == "Values scaled 100x (INR to paise)."
