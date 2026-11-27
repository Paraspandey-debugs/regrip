"""Unit tests for core payload normalization, redaction, baseline builder, and safety policy."""

from __future__ import annotations

from regrip.core.baseline_builder import build_baseline
from regrip.core.payload_normalizer import build_cluster_key, normalize_response
from regrip.core.redactor import redact_envelope
from regrip.core.replay_comparator import compare_case_response, summarize_results
from regrip.core.safety_policy import prepare_replay_arguments, validate_target_allowed
from regrip.types import (
    ActorInfo,
    CaptureConfig,
    ComparisonConfig,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RedactionAction,
    RedactionConfig,
    RedactionRule,
    ReplayCaseStatus,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    SafetyConfig,
    SafetyTier,
    SafetyViolationError,
    TargetInfo,
    TelemetryInfo,
    ToolAnnotations,
    ToolPolicyConfig,
    TransportKind,
)


def _make_envelope(
    *,
    tool_name: str = "search_products",
    arguments: dict[str, object] | None = None,
    structured: dict[str, object] | None = None,
    content_blocks: list[dict[str, object]] | None = None,
    is_error: bool = False,
) -> InteractionEnvelope:
    resp = normalize_response(
        ResponsePayload(
            status=ResponseStatus.TOOL_ERROR if is_error else ResponseStatus.OK,
            is_error=is_error,
            structured_content=structured,  # type: ignore[arg-type]
            content_blocks=content_blocks or [],  # type: ignore[arg-type]
        )
    )
    return InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="agent-a", session_id="sess-1"),
        target=TargetInfo(server_name="catalog-mcp", server_version="1.0.0"),
        operation=OperationInfo(
            tool_name=tool_name,
            annotations=ToolAnnotations(readOnlyHint=True),
        ),
        request=RequestPayload(arguments=arguments or {"query": "laptop"}),  # type: ignore[arg-type]
        response=resp,
        telemetry=TelemetryInfo(duration_ms=12.5),
    )


def test_normalize_parses_json_text_content() -> None:
    env = _make_envelope(
        content_blocks=[{"type": "text", "text": '{"balance": 100, "currency": "INR"}'}]
    )
    assert env.response.parsed_text_json == {"balance": 100, "currency": "INR"}
    assert env.response.inferred_types == {"$.balance": "int", "$.currency": "str"}


def test_redact_envelope_applies_rules_and_builtin_detectors() -> None:
    env = _make_envelope(
        arguments={
            "api_key": "secret-123",
            "email": "alice@example.com",
            "anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
            "openai": "sk-proj-abcdefghijklmnopqrstuvwxyz1234",
        },
        structured={"customer": {"email": "alice@example.com", "ssn": "123-45-6789"}},
    )
    cfg = CaptureConfig(
        redaction=RedactionConfig(
            rules=[
                RedactionRule(path="$.arguments.api_key", action=RedactionAction.DROP),
                RedactionRule(
                    path="$.structured_content.customer.email",
                    action=RedactionAction.HMAC_TOKEN,
                ),
            ]
        )
    )
    redacted = redact_envelope(env, cfg)
    assert "api_key" not in redacted.request.arguments
    assert redacted.request.arguments["email"] == "[REDACTED:EMAIL]"
    assert redacted.request.arguments["anthropic"] == "[REDACTED:ANTHROPIC_KEY]"
    assert redacted.request.arguments["openai"] == "[REDACTED:OPENAI_KEY]"
    assert isinstance(redacted.response.structured_content, dict)
    customer = redacted.response.structured_content["customer"]
    assert isinstance(customer, dict)
    assert str(customer["email"]).startswith("tok_")
    assert customer["ssn"] == "[REDACTED:SSN]"
    assert redacted.telemetry.redaction_applied is True


def test_build_baseline_clusters_and_masks_volatile_fields() -> None:
    samples = [
        _make_envelope(
            arguments={"query": "laptop", "limit": 10},
            structured={
                "request_id": f"550e8400-e29b-41d4-a716-44665544000{i}",
                "timestamp": f"2026-11-26T18:00:0{i}Z",
                "results": [{"id": 1, "price": 999}],
            },
        )
        for i in range(3)
    ]
    baseline = build_baseline(samples, server_name="catalog-mcp", tag="v1.0.0")
    assert len(baseline.cases) == 1
    assert baseline.cases[0].sample_count == 3
    assert "$.request_id" in baseline.noise_masks
    assert "$.timestamp" in baseline.noise_masks
    assert baseline.content_hash != ""


def test_cluster_key_groups_same_shape() -> None:
    k1 = build_cluster_key("search", {"query": "a", "limit": 10})
    k2 = build_cluster_key("search", {"limit": 20, "query": "b"})
    k3 = build_cluster_key("search", {"query": "b"})
    assert k1 == k2
    assert k1 != k3


def test_safety_policy_ignores_untrusted_annotations_and_blocks_prod() -> None:
    safety = SafetyConfig(
        default_tier=SafetyTier.WRITE_BLOCKED,
        tool_policies={"get_balance": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)},
    )
    # Tool has readOnlyHint=True in envelope, but default_tier is WRITE_BLOCKED
    tier, prepared = prepare_replay_arguments("unknown_tool", {"x": 1}, safety)
    assert tier == SafetyTier.WRITE_BLOCKED
    assert prepared is None

    tier_ok, prepared_ok = prepare_replay_arguments("get_balance", {"id": "1"}, safety)
    assert tier_ok == SafetyTier.READ_SAFE
    assert prepared_ok == {"id": "1"}

    try:
        validate_target_allowed("https://mcp.prod.internal/mcp", safety)
        raise AssertionError("Expected SafetyViolationError")
    except SafetyViolationError:
        pass


def test_replay_comparator_detects_value_change_and_ignores_noise() -> None:
    samples = [
        _make_envelope(
            structured={
                "request_id": f"550e8400-e29b-41d4-a716-44665544000{i}",
                "balance": 100.0,
            }
        )
        for i in range(3)
    ]
    baseline = build_baseline(samples, server_name="catalog-mcp", tag="v1")
    case = baseline.cases[0]

    cand_ok = normalize_response(
        ResponsePayload(
            status=ResponseStatus.OK,
            structured_content={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "balance": 102.0,
            },
        )
    )
    res_ok = compare_case_response(
        case,
        cand_ok,
        noise_masks=baseline.noise_masks,
        comparison=ComparisonConfig(numeric_relative_tolerance=0.05),
        duration_ms=5.0,
    )
    assert res_ok.status == ReplayCaseStatus.PASS

    cand_fail = normalize_response(
        ResponsePayload(
            status=ResponseStatus.OK,
            structured_content={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "balance": 10000.0,
            },
        )
    )
    res_fail = compare_case_response(
        case,
        cand_fail,
        noise_masks=baseline.noise_masks,
        comparison=ComparisonConfig(numeric_relative_tolerance=0.05),
        duration_ms=5.0,
    )
    assert res_fail.status == ReplayCaseStatus.FAIL
    assert res_fail.diff_paths == ["$.balance"]
    summary = summarize_results([res_ok, res_fail])
    assert summary.total == 2 and summary.passed == 1 and summary.failed == 1
