"""Transparent MCP stdio recording proxy (FR-CAP-001, NFR-REL-001)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from regrip.adapters.mcp_session_tracker import MCPSessionTracker
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types.envelope import TransportKind


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
        self._on_error = on_error
        self._tracker = MCPSessionTracker(
            server_name=server_name,
            transport=TransportKind.STDIO,
            default_agent=agent_id,
            default_session=session_id,
        )

    def _record_request(self, frame: dict[str, Any]) -> None:
        self._tracker.observe_request(frame)

    def _record_response(self, frame: dict[str, Any]) -> None:
        envelope = self._tracker.observe_server_frame(frame)
        if envelope is None:
            return
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
                try:
                    proc.stdin.write(line)  # type: ignore[union-attr]
                    await proc.stdin.drain()  # type: ignore[union-attr]
                except BrokenPipeError, ConnectionResetError:
                    break
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
