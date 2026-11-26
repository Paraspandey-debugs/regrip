"""Sample v2 MCP stdio server introducing intentional behavioral regressions for demo."""

from __future__ import annotations

import json
import sys
import uuid


def _handle(msg: dict[str, object]) -> dict[str, object] | None:
    req_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "commerce-mcp", "version": "2.0.0"},
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "get_balance",
                        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
                        "annotations": {"readOnlyHint": True},
                    },
                    {
                        "name": "search_products",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                        "annotations": {"readOnlyHint": True},
                    },
                ]
            },
        }
    if method == "tools/call":
        params = msg.get("params")
        tool_name = params.get("name") if isinstance(params, dict) else None
        if tool_name == "get_balance":
            # Regression 1: INR -> paise (100x scale shift)
            payload = {
                "request_id": str(uuid.uuid4()),
                "currency": "INR",
                "balance": 125000.0,
                "email": "alice@example.com",
            }
        elif tool_name == "search_products":
            # Regression 2: removed $.results[*].description, reversed ranking, truncated list
            payload = {
                "request_id": str(uuid.uuid4()),
                "results": [
                    {"id": "sku-3", "price": 499},
                    {"id": "sku-1", "price": 999},
                ],
            }
        else:
            payload = {"ok": True}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
            },
        }
    return None


def main() -> None:
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        resp = _handle(json.loads(stripped))
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
