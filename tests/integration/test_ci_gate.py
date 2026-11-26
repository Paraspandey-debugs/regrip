"""Integration tests for `regrip ci` gate and idempotency side-effect probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from regrip.adapters.config_store import load_config
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.cli.app import app
from regrip.core.baseline_builder import build_baseline
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

_SIDE_EFFECT_SERVER = """
import json, pathlib, sys
STATE_FILE = pathlib.Path(sys.argv[1])
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
            "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "store"}},
        }
    elif m == "tools/call":
        count = int(STATE_FILE.read_text()) if STATE_FILE.exists() else 0
        count += 1
        STATE_FILE.write_text(str(count))
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"isError": False, "structuredContent": {"balance": 100, "seq": count}},
        }
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


def test_ci_gate_catches_side_effect_and_emits_reports(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--project", "store", "--dir", str(tmp_path)])
    cfg_path = tmp_path / "regrip.yaml"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace(
        "sqlite:///.regrip/regrip.db", f"sqlite:///{tmp_path / '.regrip/regrip.db'}"
    ).replace(".regrip/blobs", str(tmp_path / ".regrip/blobs"))
    cfg_text = cfg_text.replace(
        "tool_policies: {}",
        "tool_policies:\n    get_balance:\n      tier: READ_SAFE\n      probe_idempotency: true",
    )
    cfg_path.write_text(cfg_text, encoding="utf-8")

    cfg = load_config(cfg_path)
    SQLiteStore(cfg.storage, cfg.capture)
    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="billing-bot"),
        target=TargetInfo(server_name="store"),
        operation=OperationInfo(tool_name="get_balance"),
        request=RequestPayload(arguments={}),
        response=ResponsePayload(
            status=ResponseStatus.OK, structured_content={"balance": 100, "seq": 1}
        ),
        telemetry=TelemetryInfo(duration_ms=2.0),
    )
    baseline = build_baseline([env], server_name="store", tag="v1")
    b_path = tmp_path / "baseline.json"
    b_path.write_text(baseline.model_dump_json(), encoding="utf-8")

    srv = tmp_path / "side_effect_srv.py"
    srv.write_text(_SIDE_EFFECT_SERVER, encoding="utf-8")
    state_file = tmp_path / "counter.txt"
    out_dir = tmp_path / "reports"

    res = runner.invoke(
        app,
        [
            "ci",
            "--config",
            str(cfg_path),
            "--baseline",
            str(b_path),
            "--target",
            f"{sys.executable} {srv} {state_file}",
            "--format",
            "markdown,json,junit",
            "--out-dir",
            str(out_dir),
        ],
    )
    assert res.exit_code == 1, res.output
    assert (out_dir / "regrip-report.md").exists()
    assert (out_dir / "regrip-report.json").exists()
    assert (out_dir / "regrip-junit.xml").exists()
    report_data = json.loads((out_dir / "regrip-report.json").read_text(encoding="utf-8"))
    assert report_data["verdict"] == "FAIL"
    assert any(f["category"] == "Side-Effect" for f in report_data["findings"])
