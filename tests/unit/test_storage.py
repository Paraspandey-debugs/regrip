"""Unit tests for config_store and SQLiteStore."""

from __future__ import annotations

from pathlib import Path

from regrip.adapters.config_store import load_config, write_default_config
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.core.baseline_builder import build_baseline
from regrip.types import (
    ActorInfo,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    ReplayRun,
    ReplaySummary,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    StorageConfig,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


def test_write_and_load_default_config(tmp_path: Path) -> None:
    cfg_path = write_default_config(tmp_path, project_name="demo-proj")
    assert cfg_path.exists()
    assert (tmp_path / ".regrip" / "blobs").is_dir()
    loaded = load_config(cfg_path)
    assert loaded.project == "demo-proj"
    assert loaded.safety.trust_mcp_annotations is False


def test_sqlite_store_redacts_and_persists_roundtrip(tmp_path: Path) -> None:
    storage = StorageConfig(
        dsn=f"sqlite:///{tmp_path / 'regrip.db'}",
        blob_dir=str(tmp_path / "blobs"),
    )
    store = SQLiteStore(storage)
    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="agent-1"),
        target=TargetInfo(server_name="orders-mcp"),
        operation=OperationInfo(tool_name="get_user"),
        request=RequestPayload(arguments={"email": "bob@example.com"}),
        response=ResponsePayload(
            status=ResponseStatus.OK,
            structured_content={"id": "u1", "token": "Bearer supersecrettoken12345"},
        ),
        telemetry=TelemetryInfo(duration_ms=4.2),
    )

    saved = store.save_interaction(env)
    assert saved.request.arguments["email"] == "[REDACTED:EMAIL]"
    assert "[REDACTED:BEARER]" in str(saved.response.structured_content)

    loaded = store.list_interactions(server_name="orders-mcp")
    assert len(loaded) == 1
    assert loaded[0].interaction_id == env.interaction_id
    assert loaded[0].request.arguments["email"] == "[REDACTED:EMAIL]"

    baseline = build_baseline(loaded, server_name="orders-mcp", tag="v1")
    store.save_baseline(baseline)
    fetched = store.get_baseline_by_tag("orders-mcp", "v1")
    assert fetched.content_hash == baseline.content_hash

    run = ReplayRun(
        baseline_id=baseline.baseline_id,
        baseline_tag="v1",
        target_uri="stdio://mock",
        summary=ReplaySummary(total=1, passed=1),
    )
    store.save_replay_run(run)
    fetched_run = store.get_replay_run(run.run_id)
    assert fetched_run.summary.passed == 1
