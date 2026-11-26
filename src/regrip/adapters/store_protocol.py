"""Storage backend protocols, PostgreSQL schema DDL, and S3 adapter."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.envelope import InteractionEnvelope
from regrip.types.impact import ConsumerContract

PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interactions (
    project_slug TEXT NOT NULL,
    interaction_id TEXT PRIMARY KEY,
    timestamp_utc TIMESTAMPTZ NOT NULL,
    server_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    is_error BOOLEAN NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    blob_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    project_slug TEXT NOT NULL,
    baseline_id TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    tag TEXT NOT NULL,
    git_sha TEXT,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload_json JSONB NOT NULL,
    UNIQUE(project_slug, server_name, tag)
);
CREATE TABLE IF NOT EXISTS replay_runs (
    project_slug TEXT NOT NULL,
    run_id TEXT PRIMARY KEY,
    baseline_id TEXT NOT NULL,
    baseline_tag TEXT NOT NULL,
    target_uri TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    payload_json JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_contracts (
    project_slug TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    PRIMARY KEY (project_slug, agent_id, tool_name)
);
"""


class StoreBackend(Protocol):
    """Protocol implemented by both SQLiteStore and RemoteStore."""

    def save_interaction(self, envelope: InteractionEnvelope) -> InteractionEnvelope: ...

    def list_interactions(
        self,
        *,
        server_name: str | None = None,
        tool_name: str | None = None,
        limit: int = 500,
    ) -> list[InteractionEnvelope]: ...

    def save_baseline(self, baseline: BehavioralBaseline) -> None: ...

    def get_baseline_by_tag(self, server_name: str, tag: str) -> BehavioralBaseline: ...

    def list_baselines(self, *, limit: int = 100) -> list[BehavioralBaseline]: ...

    def save_replay_run(self, run: ReplayRun) -> None: ...

    def get_replay_run(self, run_id: UUID) -> ReplayRun: ...

    def list_replay_runs(self, *, limit: int = 200) -> list[ReplayRun]: ...

    def save_consumer_contract(self, contract: ConsumerContract) -> None: ...

    def list_consumer_contracts(self) -> list[ConsumerContract]: ...


class BlobClient(Protocol):
    """Minimal S3-compatible object storage interface."""

    def put_bytes(self, bucket: str, key: str, body: bytes) -> None: ...

    def get_bytes(self, bucket: str, key: str) -> bytes: ...


class BotoS3Adapter:
    """Thin adapter bridging boto3 S3 client to BlobClient."""

    def __init__(self, boto_client: Any) -> None:
        self._client = boto_client

    def put_bytes(self, bucket: str, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=body)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        obj = self._client.get_object(Bucket=bucket, Key=key)
        data: bytes = obj["Body"].read()
        return data
