"""Tests for FR-CAP-008 dynamic tool list refresh and MCP async Task correlation."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from regrip.adapters.mcp_http_proxy import create_http_proxy_app
from regrip.adapters.mcp_session_tracker import MCPSessionTracker
from regrip.adapters.mcp_stdio_proxy import MCPStdioProxy
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types import ResponseStatus, StorageConfig, TransportKind

_DYNAMIC_ASYNC_SERVER = """
import json, sys, time

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "dyn"}},
        }
    elif m == "tools/list":
        ver = 2 if msg.get("params", {}).get("after_change") else 1
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {
                        "name": "run_report",
                        "inputSchema": {"type": "object", "properties": {"v": {"const": ver}}},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            },
        }
    elif m == "tools/call":
        if msg["params"]["name"] == "trigger_refresh":
            notif = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            sys.stdout.write(json.dumps(notif) + "\\n")
            sys.stdout.flush()
            resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"isError": False, "structuredContent": {"refreshed": True}},
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"task": {"taskId": "task-42", "status": "working"}},
            }
    elif m == "tasks/get":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"taskId": "task-42", "status": "completed"},
        }
    elif m == "tasks/result":
        time.sleep(0.02)
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"isError": False, "structuredContent": {"rows": 12}},
        }
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


def test_session_tracker_list_changed_and_failed_async_task() -> None:
    tracker = MCPSessionTracker(server_name="svc", transport=TransportKind.STDIO)
    tracker.observe_server_frame(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "job", "inputSchema": {"type": "object"}}]},
        }
    )
    assert "job" in tracker.tool_schemas

    tracker.observe_server_frame({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    assert tracker.stale_schemas is True
    assert tracker.list_changed_events == 1
    assert tracker.tool_schemas == {}

    tracker.observe_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "job", "arguments": {"id": "1"}},
        }
    )
    assert (
        tracker.observe_server_frame(
            {"jsonrpc": "2.0", "id": 2, "result": {"task": {"taskId": "t-9", "status": "working"}}}
        )
        is None
    )

    tracker.observe_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tasks/get", "params": {"taskId": "t-9"}}
    )
    env = tracker.observe_server_frame(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"taskId": "t-9", "status": "failed", "statusMessage": "worker crash"},
        }
    )
    assert env is not None
    assert env.operation.tool_name == "job"
    assert env.response.status == ResponseStatus.TOOL_ERROR
    assert env.response.error_message == "worker crash"


async def test_stdio_proxy_refreshes_schemas_and_correlates_async_task(tmp_path: Path) -> None:
    srv = tmp_path / "dyn_srv.py"
    srv.write_text(_DYNAMIC_ASYNC_SERVER, encoding="utf-8")
    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs"))
    )
    proxy = MCPStdioProxy(
        [sys.executable, str(srv)],
        store=store,
        server_name="dyn",
        agent_id="batch-agent",
    )

    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "trigger_refresh", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"after_change": True}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "run_report", "arguments": {"fmt": "csv"}},
        },
        {"jsonrpc": "2.0", "id": 6, "method": "tasks/get", "params": {"taskId": "task-42"}},
        {"jsonrpc": "2.0", "id": 7, "method": "tasks/result", "params": {"taskId": "task-42"}},
    ]
    in_reader = asyncio.StreamReader()
    in_reader.feed_data(("".join(json.dumps(f) + "\n" for f in frames)).encode("utf-8"))
    in_reader.feed_eof()

    rc = await proxy.run(in_stream=in_reader, out_writer=io.BytesIO())
    assert rc == 0

    envs = store.list_interactions(server_name="dyn")
    assert len(envs) == 2
    assert envs[0].operation.tool_name == "trigger_refresh"
    assert envs[1].operation.tool_name == "run_report"
    assert envs[1].request.arguments == {"fmt": "csv"}
    assert envs[1].response.structured_content == {"rows": 12}
    assert envs[1].operation.input_schema_hash is not None
    assert envs[1].telemetry.duration_ms >= 15.0


async def test_http_proxy_correlates_async_task_across_requests(tmp_path: Path) -> None:
    async def handle_mcp(request: Request) -> JSONResponse:
        body = await request.json()
        method, rid = body.get("method"), body.get("id")
        if method == "tools/call":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"task": {"taskId": "http-task-1", "status": "working"}},
                }
            )
        if method == "tasks/get":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"taskId": "http-task-1", "status": "working"},
                }
            )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"isError": False, "structuredContent": {"done": True}},
            }
        )

    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs"))
    )
    upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(routes=[Route("/mcp", handle_mcp, methods=["POST"])])
        ),
        base_url="http://upstream",
    )
    proxy_app = create_http_proxy_app(
        "http://upstream/mcp", store=store, server_name="http-tasks", http_client=upstream
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app), base_url="http://proxy"
    ) as c:
        await c.post(
            "/mcp",
            headers={"X-Regrip-Agent": "async-agent"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "build_export", "arguments": {"year": 2026}},
            },
        )
        await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": {"taskId": "http-task-1"},
            },
        )
        await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tasks/result",
                "params": {"taskId": "http-task-1"},
            },
        )
    await upstream.aclose()

    envs = store.list_interactions(server_name="http-tasks")
    assert len(envs) == 1
    assert envs[0].operation.tool_name == "build_export"
    assert envs[0].actor.agent_id == "async-agent"
    assert envs[0].response.structured_content == {"done": True}
