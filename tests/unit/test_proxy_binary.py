"""Tests for Rust regrip-proxy binary discovery, command building, and blob sync."""

from __future__ import annotations

from compression import zstd
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from regrip.adapters.proxy_binary import build_rust_proxy_cmd, find_rust_proxy_binary
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
    StorageConfig,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)

runner = CliRunner()


def test_find_and_build_rust_proxy_command(tmp_path: Path) -> None:
    pkg_root = tmp_path / "regrip"
    bin_dir = pkg_root / "bin"
    bin_dir.mkdir(parents=True)
    fake_bin = bin_dir / "regrip-proxy"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    assert find_rust_proxy_binary(pkg_root) == fake_bin

    stdio_cmd = build_rust_proxy_cmd(
        fake_bin,
        server_name="svc",
        agent_id="bot",
        session_id="s1",
        blob_dir=".regrip/blobs",
        config_path=tmp_path / "regrip.yaml",
        stdio_command=["python3", "srv.py"],
    )
    assert "--" in stdio_cmd
    assert stdio_cmd[-2:] == ["python3", "srv.py"]

    http_cmd = build_rust_proxy_cmd(
        fake_bin,
        server_name="svc",
        agent_id="bot",
        listen="http://127.0.0.1:9100",
        upstream="http://127.0.0.1:8000/mcp",
    )
    assert "--listen" in http_cmd
    assert "127.0.0.1:9100" in http_cmd
    assert "--upstream" in http_cmd


def test_record_cli_engine_rust_errors_when_binary_missing(tmp_path: Path) -> None:
    with patch("regrip.cli.app.find_rust_proxy_binary", return_value=None):
        res = runner.invoke(
            app,
            [
                "record",
                "--server",
                "demo",
                "--engine",
                "rust",
                "--stdio",
                "--",
                "python3",
                "srv.py",
            ],
        )
    assert res.exit_code == 3
    assert "not found" in res.output


def test_sqlite_store_syncs_external_rust_zstd_blobs(tmp_path: Path) -> None:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    store = SQLiteStore(
        StorageConfig(dsn=f"sqlite:///{tmp_path / 'regrip.db'}", blob_dir=str(blob_dir))
    )

    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="rust-agent"),
        target=TargetInfo(server_name="rust-srv"),
        operation=OperationInfo(tool_name="get_balance"),
        request=RequestPayload(arguments={"id": "a1"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"balance": 42}),
        telemetry=TelemetryInfo(duration_ms=0.8),
    )
    raw = (env.model_dump_json() + "\n").encode("utf-8")
    (blob_dir / f"rust-srv_{env.interaction_id}.jsonl.zst").write_bytes(zstd.compress(raw))

    loaded = store.list_interactions(server_name="rust-srv")
    assert len(loaded) == 1
    assert loaded[0].interaction_id == env.interaction_id
    assert loaded[0].actor.agent_id == "rust-agent"
