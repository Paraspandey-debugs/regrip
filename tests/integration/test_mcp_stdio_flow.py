"""Integration tests for MCP stdio proxy capture, baseline build, and safe replay."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

from regrip.adapters.mcp_stdio import MCPStdioDriver
from regrip.adapters.mcp_stdio_proxy import MCPStdioProxy
from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.types import (
    RegripConfig,
    ReplayCaseStatus,
    SafetyConfig,
    SafetyTier,
    StorageConfig,
    ToolPolicyConfig,
)

_MOCK_SERVER_CODE = """
import json, os, sys

SCALE = int(os.environ.get("MOCK_BALANCE_SCALE", "1"))

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    req_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "finance-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }
    elif method == "tools/list":
        resp = {
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
                        "name": "transfer_funds",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    },
                ]
            },
        }
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        if name == "get_balance":
            payload = {"currency": "INR", "balance": 1000 * SCALE}
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                },
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps({"ok": True})}],
                    "structuredContent": {"ok": True},
                },
            }
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


async def test_mcp_stdio_proxy_record_baseline_and_replay(tmp_path: Path) -> None:
    server_script = tmp_path / "mock_server.py"
    server_script.write_text(_MOCK_SERVER_CODE, encoding="utf-8")
    cmd = [sys.executable, str(server_script)]

    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(tmp_path / "blobs"))
    )
    proxy = MCPStdioProxy(cmd, store=store, server_name="finance-mcp", agent_id="billing-agent")

    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_balance", "arguments": {"id": "acct-1"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "transfer_funds", "arguments": {"amount": 50}},
        },
    ]
    in_reader = asyncio.StreamReader()
    in_reader.feed_data(("".join(json.dumps(f) + "\n" for f in frames)).encode("utf-8"))
    in_reader.feed_eof()
    out_buf = io.BytesIO()

    rc = await proxy.run(in_stream=in_reader, out_writer=out_buf)
    assert rc == 0

    recorded = store.list_interactions(server_name="finance-mcp")
    assert len(recorded) == 2

    driver = MCPStdioDriver()
    manifest = await driver.discover_capabilities(f"{sys.executable} {server_script}")
    assert manifest.server_name == "finance-mcp"
    assert [t.name for t in manifest.tools] == ["get_balance", "transfer_funds"]

    baseline = build_baseline(recorded, server_name="finance-mcp", tag="v1")
    store.save_baseline(baseline)

    config = RegripConfig(
        safety=SafetyConfig(
            default_tier=SafetyTier.WRITE_BLOCKED,
            tool_policies={"get_balance": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)},
        )
    )
    runner = ReplayRunner(config, store=store)
    run_v1 = await runner.run(baseline, f"{sys.executable} {server_script}")
    assert run_v1.summary.passed == 1
    assert run_v1.summary.skipped_unsafe == 1
    assert run_v1.summary.failed == 0

    # Regression check: v2 server scales balance by 100x
    v2_script = tmp_path / "mock_server_v2.py"
    v2_script.write_text(
        _MOCK_SERVER_CODE.replace('os.environ.get("MOCK_BALANCE_SCALE", "1")', '"100"'),
        encoding="utf-8",
    )
    run_v2 = await runner.run(baseline, f"{sys.executable} {v2_script}")
    assert run_v2.summary.failed == 1
    failed_case = next(r for r in run_v2.results if r.status == ReplayCaseStatus.FAIL)
    assert failed_case.diff_paths == ["$.balance"]
