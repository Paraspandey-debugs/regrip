"""Shared MCP session state tracker for dynamic tool lists and async Tasks (FR-CAP-008)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from regrip.adapters.mcp_jsonrpc import (
    MCP_SPEC_VERSION,
    parse_capabilities,
    parse_jsonrpc_tool_response,
)
from regrip.core.trace_context import extract_trace_and_actor
from regrip.types.baseline import ToolDescriptor
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
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


@dataclass
class PendingCall:
    """In-flight MCP JSON-RPC request awaiting its initial or task-result response."""

    started_monotonic: float
    method: str
    tool_name: str
    arguments: dict[str, Any]
    meta: dict[str, Any] | None
    task_id: str | None = None
    agent_id: str = "default-agent"
    session_id: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None


class MCPSessionTracker:
    """Tracks mid-session tool schema refreshes and MCP async Task lifecycles."""

    def __init__(
        self,
        *,
        server_name: str,
        transport: TransportKind,
        default_agent: str = "default-agent",
        default_session: str | None = None,
    ) -> None:
        self._server_name = server_name
        self._transport = transport
        self._default_agent = default_agent
        self._default_session = default_session
        self.tool_schemas: dict[str, ToolDescriptor] = {}
        self.list_changed_events: int = 0
        self.stale_schemas: bool = False
        self._pending_requests: dict[str, PendingCall] = {}
        self._pending_tasks: dict[str, PendingCall] = {}

    def observe_request(
        self,
        frame: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Record outgoing tools/call, tasks/get, or tasks/result JSON-RPC request."""
        method = frame.get("method")
        req_id = frame.get("id")
        if req_id is None or not isinstance(method, str):
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        agent_id, session_id, traceparent, tracestate = extract_trace_and_actor(
            meta,
            headers,
            default_agent=self._default_agent,
            default_session=self._default_session,
        )

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if isinstance(tool_name, str) and isinstance(arguments, dict):
                self._pending_requests[str(req_id)] = PendingCall(
                    started_monotonic=time.monotonic(),
                    method="tools/call",
                    tool_name=tool_name,
                    arguments=arguments,
                    meta=meta,
                    agent_id=agent_id,
                    session_id=session_id,
                    traceparent=traceparent,
                    tracestate=tracestate,
                )
        elif method in {"tasks/get", "tasks/result"}:
            task_id = params.get("taskId")
            if isinstance(task_id, str):
                self._pending_requests[str(req_id)] = PendingCall(
                    started_monotonic=time.monotonic(),
                    method=method,
                    tool_name="",
                    arguments={},
                    meta=meta,
                    task_id=task_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    traceparent=traceparent,
                    tracestate=tracestate,
                )

    def _update_schemas(self, frame: dict[str, Any]) -> None:
        result = frame.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return
        manifest = parse_capabilities({}, result)
        self.tool_schemas = {tool.name: tool for tool in manifest.tools}
        self.stale_schemas = False

    def _build_envelope(
        self,
        call: PendingCall,
        response: ResponsePayload,
        *,
        session_override: str | None = None,
    ) -> InteractionEnvelope:
        duration_ms = max(0.0, (time.monotonic() - call.started_monotonic) * 1000.0)
        descriptor = self.tool_schemas.get(call.tool_name)
        return InteractionEnvelope(
            protocol=ProtocolInfo(
                driver=ProtocolDriverKind.MCP,
                spec_version=MCP_SPEC_VERSION,
                transport=self._transport,
            ),
            actor=ActorInfo(
                agent_id=call.agent_id,
                session_id=session_override or call.session_id,
            ),
            target=TargetInfo(server_name=self._server_name),
            operation=OperationInfo(
                method="tools/call",
                tool_name=call.tool_name,
                input_schema_hash=descriptor.input_schema_hash if descriptor else None,
                output_schema_hash=descriptor.output_schema_hash if descriptor else None,
                annotations=descriptor.annotations if descriptor else None,
            ),
            request=RequestPayload(arguments=call.arguments),
            response=response,
            telemetry=TelemetryInfo(
                duration_ms=round(duration_ms, 3),
                traceparent=call.traceparent,
                tracestate=call.tracestate,
            ),
        )

    def observe_server_frame(
        self,
        frame: dict[str, Any],
        *,
        session_override: str | None = None,
    ) -> InteractionEnvelope | None:
        """Process server notification or response; emit envelope when interaction completes."""
        if frame.get("method") == "notifications/tools/list_changed":
            self.list_changed_events += 1
            self.stale_schemas = True
            self.tool_schemas.clear()
            return None

        if "id" not in frame:
            return None
        pending = self._pending_requests.pop(str(frame["id"]), None)
        if pending is None:
            self._update_schemas(frame)
            return None

        result = frame.get("result")
        if pending.method == "tools/call":
            if (
                isinstance(result, dict)
                and isinstance(result.get("task"), dict)
                and isinstance(result["task"].get("taskId"), str)
            ):
                task_id = result["task"]["taskId"]
                self._pending_tasks[task_id] = pending
                return None
            return self._build_envelope(
                pending, parse_jsonrpc_tool_response(frame), session_override=session_override
            )

        task_call = self._pending_tasks.get(pending.task_id or "")
        if task_call is None:
            return None

        if pending.method == "tasks/get":
            if not isinstance(result, dict):
                return None
            status = str(result.get("status", "")).lower()
            if status in {"failed", "cancelled"}:
                self._pending_tasks.pop(pending.task_id or "", None)
                err_msg = str(result.get("statusMessage") or f"MCP async task {status}")
                return self._build_envelope(
                    task_call,
                    ResponsePayload(
                        status=ResponseStatus.TOOL_ERROR,
                        is_error=True,
                        error_message=err_msg,
                    ),
                    session_override=session_override,
                )
            return None

        # pending.method == "tasks/result"
        self._pending_tasks.pop(pending.task_id or "", None)
        return self._build_envelope(
            task_call, parse_jsonrpc_tool_response(frame), session_override=session_override
        )
