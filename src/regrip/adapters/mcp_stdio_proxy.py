"""Transparent MCP stdio recording proxy (FR-CAP-001, NFR-REL-001)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from typing import Any

from regrip.adapters.mcp_jsonrpc import MCP_SPEC_VERSION, parse_jsonrpc_tool_response
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.trace_context import extract_trace_and_actor
from regrip.types.envelope import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    TargetInfo,
    TelemetryInfo,
    ToolAnnotations,
    TransportKind,
)


async def _stdin_stream_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


def _try_parse_json_dict(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        frame: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


class MCPStdioProxy:
    """Transparent stdio JSON-RPC proxy recording tools/call interactions (fail-open)."""

    def __init__(
        self,
        command: list[str],
        *,
        store: SQLiteStore,
        server_name: str,
        agent_id: str = "default-agent",
        session_id: str | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._command = command
        self._store = store
        self._server_name = server_name
        self._agent_id = agent_id
        self._session_id = session_id
        self._on_error = on_error
        self._pending_calls: dict[
            str, tuple[float, str, dict[str, Any], dict[str, Any] | None]
        ] = {}
        self._tool_annotations: dict[str, ToolAnnotations] = {}

    def _cache_tool_annotations(self, frame: dict[str, Any]) -> None:
        result = frame.get("result")
        if not isinstance(result, dict):
            return
        tools = result.get("tools")
        if not isinstance(tools, list):
            return
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            ann = tool.get("annotations")
            if isinstance(ann, dict):
                self._tool_annotations[tool["name"]] = ToolAnnotations.model_validate(ann)

    def _record_request(self, frame: dict[str, Any]) -> None:
        if frame.get("method") != "tools/call" or "id" not in frame:
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        meta = params.get("_meta")
        if isinstance(tool_name, str) and isinstance(arguments, dict):
            self._pending_calls[str(frame["id"])] = (
                time.monotonic(),
                tool_name,
                arguments,
                meta if isinstance(meta, dict) else None,
            )

    def _record_response(self, frame: dict[str, Any]) -> None:
        if "id" not in frame:
            return
        pending = self._pending_calls.pop(str(frame["id"]), None)
        if pending is None:
            self._cache_tool_annotations(frame)
            return

        started, tool_name, arguments, meta = pending
        duration_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        agent_id, session_id, traceparent, tracestate = extract_trace_and_actor(
            meta,
            None,
            default_agent=self._agent_id,
            default_session=self._session_id,
        )
        envelope = InteractionEnvelope(
            protocol=ProtocolInfo(
                driver=ProtocolDriverKind.MCP,
                spec_version=MCP_SPEC_VERSION,
                transport=TransportKind.STDIO,
            ),
            actor=ActorInfo(agent_id=agent_id, session_id=session_id),
            target=TargetInfo(server_name=self._server_name),
            operation=OperationInfo(
                method="tools/call",
                tool_name=tool_name,
                annotations=self._tool_annotations.get(tool_name),
            ),
            request=RequestPayload(arguments=arguments),
            response=parse_jsonrpc_tool_response(frame),
            telemetry=TelemetryInfo(
                duration_ms=round(duration_ms, 3),
                traceparent=traceparent,
                tracestate=tracestate,
            ),
        )
        try:
            self._store.save_interaction(envelope)
        except Exception as exc:  # fail-open capture (NFR-REL-001)
            if self._on_error:
                self._on_error(exc)

    async def run(
        self,
        in_stream: asyncio.StreamReader | None = None,
        out_writer: Any = None,
    ) -> int:
        """Spawn child MCP server and proxy stdin/stdout/stderr until EOF."""
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        reader = in_stream or await _stdin_stream_reader()
        writer = out_writer or sys.stdout.buffer

        async def _pump_in() -> None:
            while line := await reader.readline():
                if frame := _try_parse_json_dict(line):
                    self._record_request(frame)
                proc.stdin.write(line)  # type: ignore[union-attr]
                await proc.stdin.drain()  # type: ignore[union-attr]
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()

        async def _pump_out() -> None:
            while line := await proc.stdout.readline():  # type: ignore[union-attr]
                if frame := _try_parse_json_dict(line):
                    self._record_response(frame)
                writer.write(line)
                writer.flush()

        async def _pump_err() -> None:
            while chunk := await proc.stderr.read(4096):  # type: ignore[union-attr]
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()

        await asyncio.gather(_pump_in(), _pump_out(), _pump_err())
        return await proc.wait()
