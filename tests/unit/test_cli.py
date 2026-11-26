"""Unit tests for the Regrip Typer CLI."""

from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from regrip.adapters.config_store import load_config
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.app import app
from regrip.types import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)

runner = CliRunner()

_CLI_MOCK_SERVER = """
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "demo"}},
        }
    elif m == "tools/call":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"isError": False, "structuredContent": {"ok": True}},
        }
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


def test_cli_init_baseline_and_replay(tmp_path: Path) -> None:
    res_init = runner.invoke(app, ["init", "--project", "demo", "--dir", str(tmp_path)])
    assert res_init.exit_code == 0, res_init.output
    cfg_path = tmp_path / "regrip.yaml"
    assert cfg_path.exists()

    # Point config storage to tmp_path and mark ping READ_SAFE
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace(
        "sqlite:///.regrip/regrip.db", f"sqlite:///{tmp_path / '.regrip/regrip.db'}"
    ).replace(".regrip/blobs", str(tmp_path / ".regrip/blobs"))
    cfg_text = cfg_text.replace(
        "tool_policies: {}",
        "tool_policies:\n    ping:\n      tier: READ_SAFE",
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")

    cfg = load_config(cfg_path)
    store = SQLiteStore(cfg.storage, cfg.capture)
    store.save_interaction(
        InteractionEnvelope(
            protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
            actor=ActorInfo(agent_id="cli-agent"),
            target=TargetInfo(server_name="demo"),
            operation=OperationInfo(tool_name="ping"),
            request=RequestPayload(arguments={}),
            response=ResponsePayload(status=ResponseStatus.OK, structured_content={"ok": True}),
            telemetry=TelemetryInfo(duration_ms=2.0),
        )
    )

    out_json = tmp_path / "baseline.json"
    res_build = runner.invoke(
        app,
        [
            "baseline",
            "build",
            "--server",
            "demo",
            "--tag",
            "v1",
            "--output",
            str(out_json),
            "--config",
            str(cfg_path),
        ],
    )
    assert res_build.exit_code == 0, res_build.output
    assert "Baseline: demo@v1" in res_build.output

    res_inspect = runner.invoke(app, ["baseline", "inspect", "--file", str(out_json)])
    assert res_inspect.exit_code == 0, res_inspect.output
    assert "ping" in res_inspect.output

    server_py = tmp_path / "srv.py"
    server_py.write_text(_CLI_MOCK_SERVER, encoding="utf-8")
    res_replay = runner.invoke(
        app,
        [
            "replay",
            "--baseline",
            str(out_json),
            "--target",
            f"{sys.executable} {server_py}",
            "--config",
            str(cfg_path),
        ],
    )
    assert res_replay.exit_code == 0, res_replay.output
    assert "passed=1" in res_replay.output

    # Blocked production URL must exit with code 3
    res_blocked = runner.invoke(
        app,
        [
            "replay",
            "--baseline",
            str(out_json),
            "--target",
            "https://mcp.prod.internal/mcp",
            "--config",
            str(cfg_path),
        ],
    )
    assert res_blocked.exit_code == 3

    runs = store.list_replay_runs()
    assert len(runs) == 1
    out_md = tmp_path / "exported.md"
    res_export = runner.invoke(
        app,
        [
            "export",
            "--baseline",
            str(out_json),
            "--run-id",
            str(runs[0].run_id),
            "--format",
            "markdown",
            "--output",
            str(out_md),
            "--config",
            str(cfg_path),
        ],
    )
    assert res_export.exit_code == 0, res_export.output
    assert "<!-- regrip-report -->" in out_md.read_text(encoding="utf-8")
