"""OpenTelemetry GenAI semantic convention span exporter (NFR-OBS-001)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from regrip.types.envelope import InteractionEnvelope


def _parse_traceparent(traceparent: str | None) -> tuple[str, str]:
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 3 and len(parts[1]) == 32 and len(parts[2]) == 16:
            return parts[1], parts[2]
    return "0" * 32, "0" * 16


def envelope_to_otel_span(envelope: InteractionEnvelope) -> dict[str, Any]:
    """Convert an InteractionEnvelope into an OTel GenAI semantic convention span dict."""
    trace_id, parent_span_id = _parse_traceparent(envelope.telemetry.traceparent)
    start_ns = int(envelope.timestamp_utc.timestamp() * 1_000_000_000)
    end_ts = envelope.timestamp_utc + timedelta(milliseconds=envelope.telemetry.duration_ms)
    end_ns = int(end_ts.timestamp() * 1_000_000_000)
    span_id = envelope.interaction_id.hex[:16]

    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": envelope.operation.tool_name,
        "mcp.method.name": envelope.operation.method,
        "mcp.protocol.version": envelope.protocol.spec_version,
        "mcp.transport": envelope.protocol.transport.value,
        "regrip.server.name": envelope.target.server_name,
        "regrip.agent.id": envelope.actor.agent_id,
        "regrip.is_error": envelope.response.is_error,
    }
    if envelope.actor.session_id:
        attributes["regrip.session.id"] = envelope.actor.session_id

    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": f"execute_tool {envelope.operation.tool_name}",
        "kind": "CLIENT",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": "ERROR" if envelope.response.is_error else "OK"},
        "attributes": attributes,
    }


def export_spans_ndjson(envelopes: list[InteractionEnvelope], output_path: Path) -> Path:
    """Write OTel GenAI spans as newline-delimited JSON for collector ingestion."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(envelope_to_otel_span(env), sort_keys=True) for env in envelopes]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path
