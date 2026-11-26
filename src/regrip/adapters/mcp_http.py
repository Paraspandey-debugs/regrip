"""MCP Streamable HTTP client driver (FR-CAP-002)."""

from __future__ import annotations

import json
from typing import Any

import httpx

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


def _parse_http_jsonrpc_body(resp: httpx.Response) -> dict[str, Any]:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    parsed: Any = json.loads(payload)
                    if isinstance(parsed, dict):
                        return parsed
        raise ProtocolError("No JSON-RPC payload found in SSE response")

    try:
        data: Any = resp.json()
    except ValueError as exc:
        raise ProtocolError(f"Invalid JSON response from MCP HTTP server: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("MCP HTTP JSON-RPC response must be a JSON object")
    return data


class MCPHttpDriver:
    """ProtocolDriver implementation for MCP Streamable HTTP servers."""

    driver_name: str = "mcp"

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    async def _post_rpc(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProtocolError(f"MCP HTTP request failed: {exc}", context={"url": url}) from exc

        next_session = resp.headers.get("mcp-session-id") or session_id
        if resp.status_code == 202 or not resp.content:
            return None, next_session
        return _parse_http_jsonrpc_body(resp), next_session

    async def _initialize_session(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[dict[str, Any], str | None]:
        init_msg, session_id = await self._post_rpc(client, url, build_initialize_request(1))
        if init_msg is None or "error" in init_msg:
            raise ProtocolError("MCP HTTP initialize failed", context={"url": url})
        await self._post_rpc(client, url, build_initialized_notification(), session_id)
        init_result = init_msg.get("result")
        return init_result if isinstance(init_result, dict) else {}, session_id

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            init_result, session_id = await self._initialize_session(client, target_uri)
            tools_msg, _ = await self._post_rpc(
                client, target_uri, build_tools_list_request(2), session_id
            )
            tools_result = tools_msg.get("result") if tools_msg else {}
            return parse_capabilities(
                init_result, tools_result if isinstance(tools_result, dict) else {}
            )

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            _, session_id = await self._initialize_session(client, target_uri)
            req = build_tools_call_request(
                3, envelope.operation.tool_name, envelope.request.arguments
            )
            resp_msg, _ = await self._post_rpc(client, target_uri, req, session_id)
            if resp_msg is None:
                raise ProtocolError(
                    "Empty HTTP response for tools/call", context={"url": target_uri}
                )
            return parse_jsonrpc_tool_response(resp_msg)
