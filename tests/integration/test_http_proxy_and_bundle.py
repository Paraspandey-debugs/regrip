"""Integration tests for Streamable HTTP MCP proxy, trace attribution, and tar.zst bundles."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from regrip.adapters.baseline_bundle import export_baseline_bundle, import_baseline_bundle
from regrip.adapters.mcp_http_proxy import create_http_proxy_app
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.types import StorageConfig


def _mock_upstream_app() -> Starlette:
    async def handle_mcp(request: Request) -> JSONResponse | StreamingResponse:
        body = await request.json()
        method = body.get("method")
        req_id = body.get("id")
        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name")
            if tool_name == "stream_search":

                async def sse_gen():
                    msg = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": False,
                            "structuredContent": {"items": [{"id": "p1", "price": 500}]},
                        },
                    }
                    yield f"data: {json.dumps(msg)}\n\n".encode()

                return StreamingResponse(
                    sse_gen(),
                    media_type="text/event-stream",
                    headers={"Mcp-Session-Id": "sess-http-1"},
                )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": False,
                        "structuredContent": {"balance": 1000, "currency": "INR"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-http-1"},
            )
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    return Starlette(routes=[Route("/mcp", handle_mcp, methods=["POST"])])


async def test_http_proxy_json_and_sse_capture_plus_bundle_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs"))
    )
    upstream_transport = httpx.ASGITransport(app=_mock_upstream_app())
    upstream_client = httpx.AsyncClient(transport=upstream_transport, base_url="http://upstream")

    proxy_app = create_http_proxy_app(
        "http://upstream/mcp",
        store=store,
        server_name="catalog-mcp",
        http_client=upstream_client,
    )
    proxy_transport = httpx.ASGITransport(app=proxy_app)

    async with httpx.AsyncClient(transport=proxy_transport, base_url="http://proxy") as client:
        r1 = await client.post(
            "/mcp",
            headers={
                "X-Regrip-Agent": "pricing-bot",
                "Authorization": "Bearer topsecret-jwt-token",
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_balance", "arguments": {"id": "a1"}},
            },
        )
        assert r1.status_code == 200
        assert r1.headers.get("mcp-session-id") == "sess-http-1"

        r2 = await client.post(
            "/mcp",
            headers={"X-Regrip-Agent": "search-bot", "Mcp-Session-Id": "sess-http-1"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "stream_search", "arguments": {"query": "laptop"}},
            },
        )
        assert r2.status_code == 200
        assert "stream_search" not in r2.text
        assert "p1" in r2.text

    await upstream_client.aclose()

    interactions = store.list_interactions(server_name="catalog-mcp")
    assert len(interactions) == 2
    assert interactions[0].actor.agent_id == "pricing-bot"
    assert interactions[0].actor.session_id == "sess-http-1"
    assert (
        interactions[0].telemetry.traceparent
        == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    )
    assert interactions[1].actor.agent_id == "search-bot"
    assert interactions[1].response.structured_content == {"items": [{"id": "p1", "price": 500}]}

    baseline = build_baseline(interactions, server_name="catalog-mcp", tag="v1.4.2")
    bundle_path = tmp_path / "catalog-v1.4.2.regrip-baseline.tar.zst"
    export_baseline_bundle(baseline, bundle_path)
    assert bundle_path.exists()

    restored = import_baseline_bundle(bundle_path)
    assert restored.content_hash == baseline.content_hash
    assert len(restored.cases) == 2
