"""Side-effect detection probes for safe replay (FR-SAF-003)."""

from __future__ import annotations

from regrip.adapters.egress_proxy import EgressAttempt
from regrip.adapters.protocol_driver import ProtocolDriver
from regrip.core.payload_normalizer import canonical_hash, flatten_json_paths
from regrip.types.baseline import BaselineCase, BehavioralBaseline
from regrip.types.config import ToolPolicyConfig
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    JsonObject,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def payload_digest(resp: ResponsePayload, noise_masks: set[str]) -> str:
    """Compute canonical hash of response payload with noise masks stripped."""
    payload = (
        resp.structured_content if resp.structured_content is not None else resp.parsed_text_json
    )
    flat = {k: v for k, v in flatten_json_paths(payload).items() if k not in noise_masks}
    return canonical_hash(flat)


def make_replay_envelope(
    server_name: str, method: str, tool_name: str, arguments: JsonObject
) -> InteractionEnvelope:
    """Construct a synthetic InteractionEnvelope for replay invocation."""
    return InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="regrip-replay"),
        target=TargetInfo(server_name=server_name),
        operation=OperationInfo(method=method, tool_name=tool_name),
        request=RequestPayload(arguments=arguments),
        response=ResponsePayload(status=ResponseStatus.OK),
        telemetry=TelemetryInfo(duration_ms=0.0),
    )


async def run_side_effect_probes(
    driver: ProtocolDriver,
    baseline: BehavioralBaseline,
    case: BaselineCase,
    target_uri: str,
    envelope: InteractionEnvelope,
    candidate_resp: ResponsePayload,
    policy: ToolPolicyConfig | None,
    pre_state_digest: str | None,
    fs_changes: list[str],
    blocked_egress: list[EgressAttempt],
) -> str | None:
    """Check FS audit, HTTP egress audit, idempotency double-call, and state probes."""
    if fs_changes:
        return (
            f"Sandbox FS write detected during READ_SAFE tool '{case.tool_name}': "
            f"{', '.join(fs_changes[:5])}"
        )
    if blocked_egress:
        first = blocked_egress[0]
        return (
            f"Sandbox outbound HTTP mutation blocked during READ_SAFE tool '{case.tool_name}': "
            f"{first.method} {first.target}"
        )
    noise = set(baseline.noise_masks)
    if policy and policy.probe_idempotency:
        probe_resp = await driver.invoke(envelope, target_uri)
        if payload_digest(candidate_resp, noise) != payload_digest(probe_resp, noise):
            return f"Idempotency double-call probe diverged for READ_SAFE tool '{case.tool_name}'"
    if policy and policy.state_probe_tool and pre_state_digest is not None:
        post_env = make_replay_envelope(
            baseline.server_name,
            "tools/call",
            policy.state_probe_tool,
            policy.state_probe_args,
        )
        post_resp = await driver.invoke(post_env, target_uri)
        if payload_digest(post_resp, noise) != pre_state_digest:
            return (
                f"Pre/post state probe '{policy.state_probe_tool}' changed after '{case.tool_name}'"
            )
    return None
