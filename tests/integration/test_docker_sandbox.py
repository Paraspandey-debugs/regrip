"""Integration tests for `--sandbox docker` container execution and CLI invocation."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from regrip.adapters.replay_runner import ReplayRunner
from regrip.adapters.sandbox_runner import detect_container_runtime
from regrip.core.baseline_builder import build_baseline
from regrip.types import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegripConfig,
    ReplayCaseStatus,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    SafetyConfig,
    SafetyTier,
    TargetInfo,
    TelemetryInfo,
    ToolPolicyConfig,
    TransportKind,
)

_CONTAINER_ECHO_MCP = """
import json, sys
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
            "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "c"}},
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


def _find_live_runtime_and_image() -> tuple[str, str, str] | None:
    runtime = detect_container_runtime()
    if runtime is None:
        return None
    preferred = os.environ.get("REGRIP_SANDBOX_IMAGE")
    candidates = (
        [preferred]
        if preferred
        else [
            "python:3.12-slim",
            "python:3.12-alpine",
            "python:alpine",
            "localhost/notebook-builder:fedora",
        ]
    )
    for img in candidates:
        if not img:
            continue
        inspect = subprocess.run([runtime, "image", "inspect", img], capture_output=True, timeout=5)
        if inspect.returncode != 0:
            continue
        for py_bin in ("python3", "python"):
            probe = subprocess.run(
                [runtime, "run", "--rm", "--network=none", img, py_bin, "--version"],
                capture_output=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return runtime, img, py_bin
    return None


async def test_replay_runner_docker_sandbox_invocation_via_runtime_cli(tmp_path: Path) -> None:
    srv = tmp_path / "srv.py"
    srv.write_text(_CONTAINER_ECHO_MCP, encoding="utf-8")
    args_log = tmp_path / "docker_argv.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_shim = fake_bin / "docker"
    docker_shim.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys\n"
        f"pathlib.Path({str(args_log)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "idx = sys.argv.index('regrip-mcp:test') + 1\n"
        "raise SystemExit(subprocess.call(sys.argv[idx:]))\n",
        encoding="utf-8",
    )
    docker_shim.chmod(docker_shim.stat().st_mode | stat.S_IXUSR)

    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="a1"),
        target=TargetInfo(server_name="c"),
        operation=OperationInfo(tool_name="ping"),
        request=RequestPayload(arguments={}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"ok": True}),
        telemetry=TelemetryInfo(duration_ms=1.0),
    )
    baseline = build_baseline([env], server_name="c", tag="v1")
    cfg = RegripConfig(
        safety=SafetyConfig(tool_policies={"ping": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)})
    )

    with (
        patch("regrip.adapters.sandbox_runner.detect_container_runtime", return_value="docker"),
        patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}),
    ):
        run = await ReplayRunner(cfg).run(
            baseline,
            f"{sys.executable} {srv}",
            sandbox="docker",
            sandbox_image="regrip-mcp:test",
            egress_audit=False,
        )

    assert run.execution_mode == "docker"
    assert run.summary.passed == 1
    assert run.results[0].status == ReplayCaseStatus.PASS
    recorded_argv = args_log.read_text(encoding="utf-8")
    assert (
        "run --rm -i --network=none --tmpfs /tmp:rw,noexec,nosuid regrip-mcp:test" in recorded_argv
    )


async def test_live_container_runtime_sandbox_if_available() -> None:
    found = _find_live_runtime_and_image()
    if found is None:
        pytest.skip("No live Docker/Podman daemon and cached python image available")
    runtime, image, py_bin = found
    assert shutil.which(runtime) is not None

    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="a1"),
        target=TargetInfo(server_name="c"),
        operation=OperationInfo(tool_name="ping"),
        request=RequestPayload(arguments={}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"ok": True}),
        telemetry=TelemetryInfo(duration_ms=1.0),
    )
    baseline = build_baseline([env], server_name="c", tag="v1")
    cfg = RegripConfig(
        safety=SafetyConfig(tool_policies={"ping": ToolPolicyConfig(tier=SafetyTier.READ_SAFE)})
    )
    inline_cmd = f"{py_bin} -c {shlex.quote(_CONTAINER_ECHO_MCP)}"
    run = await ReplayRunner(cfg).run(
        baseline,
        inline_cmd,
        sandbox="docker",
        sandbox_image=image,
        egress_audit=False,
    )
    assert run.execution_mode == "docker"
    assert run.summary.passed == 1
