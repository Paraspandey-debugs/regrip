"""Tests for hybrid sandbox mode resolution, container cmd builder, FS audit, and state probes."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sandbox_runner import (
    build_ephemeral_container_cmd,
    resolve_sandbox_mode,
)
from regrip.core.baseline_builder import build_baseline
from regrip.core.detectors import diff_baseline_against_run
from regrip.types import (
    ActorInfo,
    ComparisonConfig,
    FindingCategory,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegripConfig,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    SafetyConfig,
    SafetyTier,
    SafetyViolationError,
    TargetInfo,
    TelemetryInfo,
    ToolPolicyConfig,
    TransportKind,
)

_FS_WRITER_SERVER = """
import json, pathlib, sys
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
            "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "s"}},
        }
    elif m == "tools/call":
        pathlib.Path("leaked_write.txt").write_text("mutated")
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


def test_resolve_sandbox_mode_auto_and_docker_missing() -> None:
    with patch("regrip.adapters.sandbox_runner.detect_container_runtime", return_value=None):
        mode, rt = resolve_sandbox_mode("auto")
        assert mode == "subprocess" and rt is None
        try:
            resolve_sandbox_mode("docker")
            raise AssertionError("Expected SafetyViolationError")
        except SafetyViolationError:
            pass

    cmd = build_ephemeral_container_cmd("docker", "mcp-img:v1", ["python", "-m", "srv"])
    assert "--network=none" in cmd and "--rm" in cmd


async def test_replay_catches_unexpected_fs_write_in_subprocess_sandbox(tmp_path: Path) -> None:
    srv = tmp_path / "fs_writer.py"
    srv.write_text(_FS_WRITER_SERVER, encoding="utf-8")
    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="a1"),
        target=TargetInfo(server_name="s"),
        operation=OperationInfo(tool_name="read_items"),
        request=RequestPayload(arguments={}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"ok": True}),
        telemetry=TelemetryInfo(duration_ms=1.0),
    )
    baseline = build_baseline([env], server_name="s", tag="v1")
    cfg = RegripConfig(
        safety=SafetyConfig(
            tool_policies={"read_items": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)}
        )
    )
    runner = ReplayRunner(cfg)
    run = await runner.run(baseline, f"{sys.executable} {srv}", sandbox="subprocess")
    assert run.summary.failed == 1
    report = diff_baseline_against_run(baseline, run, ComparisonConfig())
    assert report.findings[0].category == FindingCategory.SIDE_EFFECT
    assert "leaked_write.txt" in report.findings[0].deterministic_reason
