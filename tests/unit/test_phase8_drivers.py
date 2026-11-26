"""Tests for Phase 8 protocol drivers (HTTPDriver for REST/OpenAPI and A2ADriver)."""

from __future__ import annotations

import json

import httpx

from regrip.adapters.a2a_driver import A2ADriver
from regrip.adapters.http_driver import HTTPDriver
from regrip.adapters.replay_runner import ReplayRunner, select_driver
from regrip.core.baseline_builder import build_baseline
from regrip.core.detectors import diff_baseline_against_run
from regrip.types import (
    ActorInfo,
    ComparisonConfig,
    FindingCategory,
    FindingSeverity,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegripConfig,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    SafetyConfig,
    SafetyTier,
    TargetInfo,
    TelemetryInfo,
    ToolPolicyConfig,
    TransportKind,
)


def _openapi_spec() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "billing-api", "version": "2.0.0"},
        "paths": {
            "/accounts/{account_id}/balance": {
                "get": {
                    "operationId": "get_balance",
                    "parameters": [{"name": "account_id", "in": "path", "required": True}],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"balance": {"type": "number"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }


async def test_http_driver_openapi_discovery_and_replay_diff() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=_openapi_spec())
        if request.url.path == "/accounts/acc-1/balance":
            return httpx.Response(200, json={"balance": 10000})
        return httpx.Response(404, json={"error": "not found"})

    driver = HTTPDriver(transport=httpx.MockTransport(_handler))
    manifest = await driver.discover_capabilities("openapi+http://billing.local")
    assert manifest.server_name == "billing-api"
    assert manifest.tools[0].name == "get_balance"

    env = InteractionEnvelope(
        protocol=ProtocolInfo(
            driver=ProtocolDriverKind.HTTP, transport=TransportKind.STREAMABLE_HTTP
        ),
        actor=ActorInfo(agent_id="pricing-bot"),
        target=TargetInfo(server_name="billing-api"),
        operation=OperationInfo(
            method="GET /accounts/{account_id}/balance", tool_name="get_balance"
        ),
        request=RequestPayload(arguments={"account_id": "acc-1"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"balance": 100}),
        telemetry=TelemetryInfo(duration_ms=3.0),
    )
    baseline = build_baseline([env], server_name="billing-api", tag="v1")
    cfg = RegripConfig(
        safety=SafetyConfig(
            tool_policies={"get_balance": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)}
        )
    )
    run = await ReplayRunner(cfg, driver=driver).run(
        baseline, "openapi+http://billing.local", sandbox="subprocess"
    )
    report = diff_baseline_against_run(baseline, run, ComparisonConfig())
    assert report.verdict == FindingSeverity.FAIL
    assert any(f.category == FindingCategory.VALUE_UNIT for f in report.findings)
    assert isinstance(select_driver("openapi+https://api.example"), HTTPDriver)


async def test_a2a_driver_agent_card_discovery_and_invoke() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(
                200,
                json={
                    "name": "research-agent",
                    "version": "1.2.0",
                    "skills": [{"id": "summarize", "description": "Summarize topic"}],
                },
            )
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "message/send"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "status": {"state": "completed"},
                    "artifacts": [
                        {"parts": [{"kind": "data", "data": {"summary": "done", "score": 95}}]}
                    ],
                },
            },
        )

    driver = A2ADriver(transport=httpx.MockTransport(_handler))
    manifest = await driver.discover_capabilities("a2a+http://agent.local")
    assert manifest.server_name == "research-agent"
    assert manifest.tools[0].name == "summarize"

    env = InteractionEnvelope(
        protocol=ProtocolInfo(
            driver=ProtocolDriverKind.A2A, transport=TransportKind.STREAMABLE_HTTP
        ),
        actor=ActorInfo(agent_id="orchestrator"),
        target=TargetInfo(server_name="research-agent"),
        operation=OperationInfo(method="message/send", tool_name="summarize"),
        request=RequestPayload(arguments={"topic": "mcp"}),
        response=ResponsePayload(status=ResponseStatus.OK),
        telemetry=TelemetryInfo(duration_ms=2.0),
    )
    resp = await driver.invoke(env, "a2a+http://agent.local")
    assert resp.status == ResponseStatus.OK
    assert resp.structured_content == {"summary": "done", "score": 95}
    assert isinstance(select_driver("a2a+https://agent.example"), A2ADriver)
