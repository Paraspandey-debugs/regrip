"""Tests for RemoteStore (PostgreSQL + S3 stubs), project isolation, and Bearer RBAC."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from regrip.adapters.api_server import create_api_app
from regrip.adapters.remote_store import RemoteStore, open_store
from regrip.adapters.sqlite_store import SQLiteStore
from regrip.types import (
    ActorInfo,
    BaselineCase,
    BehavioralBaseline,
    ConsumerContract,
    InteractionEnvelope,
    OperationInfo,
    ProtocolDriverKind,
    ProtocolInfo,
    RegripConfig,
    ReplayRun,
    RequestPayload,
    ResponsePayload,
    ResponseStatus,
    StorageConfig,
    StorageMode,
    TargetInfo,
    TelemetryInfo,
    TransportKind,
)


class _FakePgConn:
    """In-memory SQLite-backed adapter emulating psycopg %s placeholders for RemoteStore tests."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        adapted = sql.replace("TIMESTAMPTZ", "TEXT").replace("JSONB", "TEXT")
        adapted = adapted.replace("DOUBLE PRECISION", "REAL").replace("BOOLEAN", "INTEGER")
        adapted = adapted.replace("%s", "?")
        if "ON CONFLICT" in adapted and "EXCLUDED." in adapted:
            adapted = adapted.replace("EXCLUDED.", "excluded.")
        return self._conn.execute(adapted, params)

    def commit(self) -> None:
        self._conn.commit()


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(self, bucket: str, key: str, body: bytes) -> None:
        self.objects[(bucket, key)] = body

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


def test_remote_store_redacts_compresses_and_isolates_projects(tmp_path: Path) -> None:
    pg = _FakePgConn()
    s3 = _FakeS3Client()
    storage = StorageConfig(
        mode=StorageMode.REMOTE,
        dsn="postgresql://user:pass@db.local/regrip",
        s3_bucket="regrip-prod-blobs",
    )
    store_a = RemoteStore(storage, project_slug="alpha", pg_conn=pg, s3_client=s3)
    store_b = RemoteStore(storage, project_slug="beta", pg_conn=pg, s3_client=s3)

    env = InteractionEnvelope(
        protocol=ProtocolInfo(driver=ProtocolDriverKind.MCP, transport=TransportKind.STDIO),
        actor=ActorInfo(agent_id="a1"),
        target=TargetInfo(server_name="svc"),
        operation=OperationInfo(tool_name="login"),
        request=RequestPayload(arguments={"token": "Bearer verysecrettoken12345"}),
        response=ResponsePayload(status=ResponseStatus.OK, structured_content={"ok": True}),
        telemetry=TelemetryInfo(duration_ms=1.0),
    )
    saved = store_a.save_interaction(env)
    assert "verysecrettoken12345" not in str(saved.request.arguments)
    assert len(s3.objects) == 1
    assert len(store_a.list_interactions(server_name="svc")) == 1
    assert store_b.list_interactions(server_name="svc") == []

    b = BehavioralBaseline(
        server_name="svc",
        tag="v1",
        cases=[BaselineCase(tool_name="login", cluster_key="login:1")],
    )
    store_a.save_baseline(b)
    assert store_a.get_baseline_by_tag("svc", "v1").baseline_id == b.baseline_id
    assert store_b.list_baselines() == []

    run = ReplayRun(baseline_id=b.baseline_id, baseline_tag="v1", target_uri="stdio://cand")
    store_a.save_replay_run(run)
    assert store_a.get_replay_run(run.run_id).run_id == run.run_id
    assert store_b.list_replay_runs() == []

    store_a.save_consumer_contract(ConsumerContract(agent_id="a1", tool_name="login"))
    assert len(store_a.list_consumer_contracts()) == 1
    assert store_b.list_consumer_contracts() == []

    local_cfg = RegripConfig(
        storage=StorageConfig(
            dsn=f"sqlite:///{tmp_path / 'local.db'}",
            blob_dir=str(tmp_path / "blobs"),
        )
    )
    assert isinstance(open_store(local_cfg), SQLiteStore)


async def test_api_server_enforces_bearer_rbac_in_remote_mode(tmp_path: Path) -> None:
    pg = _FakePgConn()
    s3 = _FakeS3Client()
    storage = StorageConfig(
        mode=StorageMode.REMOTE,
        dsn="postgresql://db.local/regrip",
        s3_bucket="regrip-blobs",
        api_token_env="REGRIP_GLOBAL_TOK",
        project_token_envs={"team-a": "REGRIP_TEAM_A_TOK"},
    )
    cfg = RegripConfig(project="team-a", storage=storage)
    store = RemoteStore(storage, project_slug="team-a", pg_conn=pg, s3_client=s3)
    app = create_api_app(cfg, store)

    with patch.dict(
        os.environ,
        {"REGRIP_GLOBAL_TOK": "global-secret", "REGRIP_TEAM_A_TOK": "team-a-secret"},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://regrip.local"
        ) as client:
            assert (await client.get("/api/v1/health")).status_code == 200
            assert (await client.get("/api/v1/projects/team-a/baselines")).status_code == 401
            assert (
                await client.get(
                    "/api/v1/projects/team-a/baselines",
                    headers={"Authorization": "Bearer global-secret"},
                )
            ).status_code == 403
            assert (
                await client.get(
                    "/api/v1/projects/team-a/baselines",
                    headers={"Authorization": "Bearer team-a-secret"},
                )
            ).status_code == 200
