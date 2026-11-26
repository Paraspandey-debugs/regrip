"""MCP stdio ProtocolDriver implementation for capability discovery and replay."""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

from regrip.adapters.mcp_jsonrpc import (
    build_initialize_request,
    build_initialized_notification,
    build_tools_call_request,
    build_tools_list_request,
    parse_capabilities,
    parse_jsonrpc_tool_response,
)
from regrip.types.baseline import CapabilityManifest
from regrip.types.envelope import InteractionEnvelope, ResponsePayload
from regrip.types.errors import ProtocolError


def _parse_stdio_command(target_uri: str) -> list[str]:
    cleaned = target_uri.removeprefix("stdio://").strip()
    parts = shlex.split(cleaned)
    if not parts:
        raise ProtocolError("Empty stdio target command", context={"target_uri": target_uri})
    return parts


async def _read_json_line(stream: asyncio.StreamReader, timeout: float) -> dict[str, Any]:
    while True:
        raw = await asyncio.wait_for(stream.readline(), timeout=timeout)
        if not raw:
            raise ProtocolError("MCP stdio server closed stdout unexpectedly")
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Malformed JSON-RPC frame from stdio server: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed


async def _send_and_wait(
    stdin: asyncio.StreamWriter,
    stdout: asyncio.StreamReader,
    frame: dict[str, Any],
    req_id: int | str,
    timeout: float,
) -> dict[str, Any]:
    stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
    await stdin.drain()
    while True:
        msg = await _read_json_line(stdout, timeout)
        if msg.get("id") == req_id:
            return msg


class MCPStdioDriver:
    """ProtocolDriver implementation for MCP over stdio subprocesses."""

    driver_name: str = "mcp"

    def __init__(self, timeout_seconds: float = 30.0, cwd: str | None = None) -> None:
        self._timeout = timeout_seconds
        self._cwd = cwd

    async def _start_and_init(
        self, target_uri: str
    ) -> tuple[asyncio.subprocess.Process, dict[str, Any]]:
        cmd = _parse_stdio_command(target_uri)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
        except OSError as exc:
            raise ProtocolError(
                f"Failed to launch stdio MCP command: {exc}",
                context={"command": " ".join(cmd)},
            ) from exc

        assert proc.stdin is not None and proc.stdout is not None
        init_msg = await _send_and_wait(
            proc.stdin, proc.stdout, build_initialize_request(1), 1, self._timeout
        )
        if "error" in init_msg:
            await self._terminate(proc)
            raise ProtocolError(f"MCP initialize failed: {init_msg['error']}")

        notif = json.dumps(build_initialized_notification()) + "\n"
        proc.stdin.write(notif.encode("utf-8"))
        await proc.stdin.drain()
        init_result = init_msg.get("result")
        return proc, init_result if isinstance(init_result, dict) else {}

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        proc, init_result = await self._start_and_init(target_uri)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            tools_msg = await _send_and_wait(
                proc.stdin, proc.stdout, build_tools_list_request(2), 2, self._timeout
            )
            tools_result = tools_msg.get("result")
            return parse_capabilities(
                init_result, tools_result if isinstance(tools_result, dict) else {}
            )
        finally:
            await self._terminate(proc)

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        proc, _ = await self._start_and_init(target_uri)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            req = build_tools_call_request(
                3, envelope.operation.tool_name, envelope.request.arguments
            )
            resp_msg = await _send_and_wait(proc.stdin, proc.stdout, req, 3, self._timeout)
            return parse_jsonrpc_tool_response(resp_msg)
        finally:
            await self._terminate(proc)
