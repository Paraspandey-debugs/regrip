"""Phase 8 Agent-to-Agent (A2A) ProtocolDriver adapter (SRS Section 2.3 & 6.2)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from regrip.types.baseline import CapabilityManifest, ToolDescriptor
from regrip.types.envelope import InteractionEnvelope, ResponsePayload, ResponseStatus
from regrip.types.errors import ProtocolError


def _strip_scheme(target_uri: str) -> str:
    if target_uri.lower().startswith("a2a+"):
        return target_uri[len("a2a+") :]
    return target_uri


def _extract_parts_payload(result: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    parts: list[dict[str, Any]] = []
    for artifact in result.get("artifacts", []):
        if isinstance(artifact, dict) and isinstance(artifact.get("parts"), list):
            parts.extend(p for p in artifact["parts"] if isinstance(p, dict))
    msg = (result.get("status") or {}).get("message")
    if isinstance(msg, dict) and isinstance(msg.get("parts"), list):
        parts.extend(p for p in msg["parts"] if isinstance(p, dict))

    structured: Any = None
    blocks: list[dict[str, Any]] = []
    parsed_text: Any = None
    for part in parts:
        kind = part.get("kind") or part.get("type")
        if kind == "data" and "data" in part and isinstance(part["data"], (dict, list)):
            structured = part["data"]
        elif kind == "text" and isinstance(part.get("text"), str):
            text = part["text"]
            blocks.append({"type": "text", "text": text})
            if parsed_text is None:
                try:
                    candidate = json.loads(text)
                    if isinstance(candidate, (dict, list)):
                        parsed_text = candidate
                except ValueError:
                    pass
    return structured if structured is not None else parsed_text, blocks


class A2ADriver:
    """ProtocolDriver implementation for Agent-to-Agent (A2A) JSON-RPC servers."""

    driver_name: str = "a2a"

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport

    async def discover_capabilities(self, target_uri: str) -> CapabilityManifest:
        base_url = _strip_scheme(target_uri).rstrip("/")
        card_url = f"{base_url}/.well-known/agent.json"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(card_url)
                resp.raise_for_status()
                card: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProtocolError(
                f"Failed to fetch A2A AgentCard: {exc}", context={"url": card_url}
            ) from exc

        if not isinstance(card, dict):
            raise ProtocolError("A2A AgentCard must be a JSON object", context={"url": card_url})

        tools: list[ToolDescriptor] = []
        for skill in card.get("skills", []):
            if not isinstance(skill, dict):
                continue
            skill_id = str(skill.get("id") or skill.get("name") or "default_skill")
            tools.append(
                ToolDescriptor(
                    name=skill_id,
                    description=skill.get("description"),
                    input_schema=skill.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        return CapabilityManifest(
            server_name=str(card.get("name") or base_url),
            server_version=str(card["version"]) if "version" in card else None,
            spec_version=str(card.get("protocolVersion") or "0.2"),
            tools=tools,
        )

    async def invoke(self, envelope: InteractionEnvelope, target_uri: str) -> ResponsePayload:
        url = _strip_scheme(target_uri)
        rpc_method = (
            envelope.operation.method
            if envelope.operation.method in {"message/send", "tasks/send"}
            else "message/send"
        )
        rpc_body = {
            "jsonrpc": "2.0",
            "id": str(envelope.interaction_id),
            "method": rpc_method,
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "data", "data": envelope.request.arguments}],
                },
                "metadata": {"skill": envelope.operation.tool_name},
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(url, json=rpc_body)
                resp.raise_for_status()
                msg: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProtocolError(f"A2A invocation failed: {exc}", context={"url": url}) from exc

        if not isinstance(msg, dict):
            raise ProtocolError("A2A JSON-RPC response must be an object", context={"url": url})

        if "error" in msg and isinstance(msg["error"], dict):
            err = msg["error"]
            return ResponsePayload(
                status=ResponseStatus.PROTOCOL_ERROR,
                is_error=True,
                error_code=err.get("code"),
                error_message=str(err.get("message", "A2A JSON-RPC error")),
            )

        result = msg.get("result")
        if not isinstance(result, dict):
            return ResponsePayload(status=ResponseStatus.OK, is_error=False)

        state = str((result.get("status") or {}).get("state", "completed")).lower()
        structured, blocks = _extract_parts_payload(result)
        is_failed = state in {"failed", "rejected"}
        return ResponsePayload(
            status=ResponseStatus.TOOL_ERROR if is_failed else ResponseStatus.OK,
            is_error=is_failed,
            structured_content=structured if isinstance(structured, (dict, list)) else None,
            content_blocks=blocks,
        )
