"""Shared MCP JSON-RPC 2.0 frame builders and response normalizers."""

from __future__ import annotations

from typing import Any

from regrip.core.payload_normalizer import canonical_hash, normalize_response
from regrip.types.baseline import CapabilityManifest, ToolDescriptor
from regrip.types.envelope import (
    JsonObject,
    ResponsePayload,
    ResponseStatus,
    ToolAnnotations,
)

MCP_SPEC_VERSION = "2025-11-25"


def build_initialize_request(req_id: int | str = 1) -> JsonObject:
    """Build an MCP JSON-RPC 2.0 initialize request."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_SPEC_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "regrip", "version": "0.1.0"},
        },
    }


def build_initialized_notification() -> JsonObject:
    """Build an MCP notifications/initialized frame."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def build_tools_list_request(req_id: int | str = 2) -> JsonObject:
    """Build an MCP tools/list request."""
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}}


def build_tools_call_request(
    req_id: int | str,
    tool_name: str,
    arguments: JsonObject,
    *,
    meta: JsonObject | None = None,
) -> JsonObject:
    """Build an MCP tools/call request."""
    params: JsonObject = {"name": tool_name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": params}


def parse_capabilities(
    init_result: dict[str, Any],
    tools_result: dict[str, Any],
    *,
    fallback_name: str = "mcp-server",
) -> CapabilityManifest:
    """Parse initialize + tools/list JSON-RPC results into a CapabilityManifest."""
    server_info = init_result.get("serverInfo")
    server_name = fallback_name
    server_version: str | None = None
    if isinstance(server_info, dict):
        if isinstance(server_info.get("name"), str):
            server_name = server_info["name"]
        if isinstance(server_info.get("version"), str):
            server_version = server_info["version"]

    spec_version = str(init_result.get("protocolVersion") or MCP_SPEC_VERSION)
    raw_tools = tools_result.get("tools", [])
    descriptors: list[ToolDescriptor] = []
    if isinstance(raw_tools, list):
        for raw in raw_tools:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            input_schema = raw.get("inputSchema")
            output_schema = raw.get("outputSchema")
            ann_raw = raw.get("annotations")
            in_dict = input_schema if isinstance(input_schema, dict) else {}
            out_dict = output_schema if isinstance(output_schema, dict) else None
            descriptors.append(
                ToolDescriptor(
                    name=raw["name"],
                    description=raw.get("description")
                    if isinstance(raw.get("description"), str)
                    else None,
                    input_schema=in_dict,
                    output_schema=out_dict,
                    input_schema_hash=canonical_hash(in_dict) if in_dict else None,
                    output_schema_hash=canonical_hash(out_dict) if out_dict else None,
                    annotations=ToolAnnotations.model_validate(ann_raw)
                    if isinstance(ann_raw, dict)
                    else None,
                )
            )
    return CapabilityManifest(
        server_name=server_name,
        server_version=server_version,
        spec_version=spec_version,
        tools=descriptors,
    )


def parse_jsonrpc_tool_response(message: dict[str, Any]) -> ResponsePayload:
    """Convert an MCP JSON-RPC response frame into a normalized ResponsePayload."""
    if "error" in message and isinstance(message["error"], dict):
        err = message["error"]
        return normalize_response(
            ResponsePayload(
                status=ResponseStatus.PROTOCOL_ERROR,
                is_error=True,
                error_code=err.get("code"),
                error_message=str(err.get("message", "JSON-RPC error")),
            )
        )

    result = message.get("result")
    if not isinstance(result, dict):
        return normalize_response(
            ResponsePayload(
                status=ResponseStatus.PROTOCOL_ERROR,
                is_error=True,
                error_message="Missing result object in MCP response",
            )
        )

    is_error = bool(result.get("isError", False))
    content_raw = result.get("content", [])
    content_blocks = (
        [b for b in content_raw if isinstance(b, dict)] if isinstance(content_raw, list) else []
    )
    structured = result.get("structuredContent")
    structured_val = structured if isinstance(structured, (dict, list)) else None
    error_message: str | None = None
    if is_error and content_blocks:
        first_text = content_blocks[0].get("text")
        if isinstance(first_text, str):
            error_message = first_text

    return normalize_response(
        ResponsePayload(
            status=ResponseStatus.TOOL_ERROR if is_error else ResponseStatus.OK,
            is_error=is_error,
            error_message=error_message,
            structured_content=structured_val,
            content_blocks=content_blocks,
        )
    )
