"""Hosted remote PostgreSQL + S3 store backend (Phase 7)."""

from __future__ import annotations

import importlib
import json
from compression import zstd
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from regrip.adapters.sqlite_store import SQLiteStore
from regrip.adapters.store_protocol import PG_SCHEMA_SQL, BlobClient, BotoS3Adapter, StoreBackend
from regrip.core.payload_normalizer import canonical_hash
from regrip.core.redactor import redact_envelope
from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.config import CaptureConfig, RegripConfig, StorageConfig, StorageMode
from regrip.types.envelope import InteractionEnvelope
from regrip.types.errors import BaselineError, StorageError
from regrip.types.impact import ConsumerContract


class RemoteStore:
    """Hosted PostgreSQL + S3 zstd blob store scoped by project_slug."""

    def __init__(
        self,
        storage: StorageConfig,
        capture: CaptureConfig | None = None,
        *,
        project_slug: str = "default",
        pg_conn: Any = None,
        s3_client: BlobClient | None = None,
    ) -> None:
        self._storage = storage
        self._capture = capture or CaptureConfig()
        self._project = project_slug
        self._conn = pg_conn or self._connect_pg(storage.dsn)
        self._s3 = s3_client or self._connect_s3()
        self._bucket = storage.s3_bucket or "regrip-blobs"
        self._prefix = storage.s3_prefix.strip("/")
        for stmt in PG_SCHEMA_SQL.strip().split(";"):
            cleaned = stmt.strip()
            if cleaned:
                self._conn.execute(cleaned)
        self._conn.commit()

    def _connect_pg(self, dsn: str) -> Any:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError as exc:
            raise StorageError(
                "Remote PostgreSQL mode requires `psycopg` installed",
                context={"dsn": dsn},
            ) from exc
        return psycopg.connect(dsn)

    def _connect_s3(self) -> BlobClient:
        try:
            boto3 = importlib.import_module("boto3")
        except ImportError as exc:
            raise StorageError(
                "Remote S3 blob mode requires `boto3` installed",
                context={"bucket": self._storage.s3_bucket or ""},
            ) from exc
        return BotoS3Adapter(boto3.client("s3"))

    def _blob_key(self, env: InteractionEnvelope) -> str:
        digest = canonical_hash(env.model_dump(mode="json"))[:16]
        name = f"{env.target.server_name}_{env.interaction_id}_{digest}.jsonl.zst"
        return f"{self._prefix}/{self._project}/{name}"

    def save_interaction(self, envelope: InteractionEnvelope) -> InteractionEnvelope:
        redacted = redact_envelope(envelope, self._capture)
        key = self._blob_key(redacted)
        raw = (redacted.model_dump_json() + "\n").encode("utf-8")
        self._s3.put_bytes(self._bucket, key, zstd.compress(raw))
        self._conn.execute(
            """
            INSERT INTO interactions (
                project_slug, interaction_id, timestamp_utc, server_name, agent_id,
                session_id, tool_name, method, status, is_error, duration_ms, blob_ref
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (interaction_id) DO UPDATE SET blob_ref = EXCLUDED.blob_ref
            """,
            (
                self._project,
                str(redacted.interaction_id),
                redacted.timestamp_utc.isoformat(),
                redacted.target.server_name,
                redacted.actor.agent_id,
                redacted.actor.session_id,
                redacted.operation.tool_name,
                redacted.operation.method,
                redacted.response.status.value,
                redacted.response.is_error,
                redacted.telemetry.duration_ms,
                key,
            ),
        )
        self._conn.commit()
        return redacted

    def list_interactions(
        self,
        *,
        server_name: str | None = None,
        tool_name: str | None = None,
        limit: int = 500,
    ) -> list[InteractionEnvelope]:
        clauses = ["project_slug = %s"]
        params: list[Any] = [self._project]
        if server_name:
            clauses.append("server_name = %s")
            params.append(server_name)
        if tool_name:
            clauses.append("tool_name = %s")
            params.append(tool_name)
        params.append(limit)
        where = " AND ".join(clauses)
        sql = f"SELECT blob_ref FROM interactions WHERE {where} ORDER BY timestamp_utc ASC LIMIT %s"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        out: list[InteractionEnvelope] = []
        for (key,) in rows:
            raw_bytes = self._s3.get_bytes(self._bucket, key)
            raw_json = zstd.decompress(raw_bytes).decode("utf-8").strip()
            out.append(InteractionEnvelope.model_validate_json(raw_json))
        return out

    def save_baseline(self, baseline: BehavioralBaseline) -> None:
        self._conn.execute(
            """
            INSERT INTO baselines (
                project_slug, baseline_id, server_name, tag, git_sha,
                content_hash, created_at, payload_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (baseline_id) DO UPDATE SET payload_json = EXCLUDED.payload_json
            """,
            (
                self._project,
                str(baseline.baseline_id),
                baseline.server_name,
                baseline.tag,
                baseline.git_sha,
                baseline.content_hash,
                baseline.created_at.isoformat(),
                baseline.model_dump_json(),
            ),
        )
        self._conn.commit()

    def get_baseline_by_tag(self, server_name: str, tag: str) -> BehavioralBaseline:
        sql = (
            "SELECT payload_json FROM baselines "
            "WHERE project_slug = %s AND server_name = %s AND tag = %s"
        )
        row = self._conn.execute(sql, (self._project, server_name, tag)).fetchone()
        if row is None:
            raise BaselineError(
                "Baseline not found",
                context={"server_name": server_name, "tag": tag},
            )
        return _parse_json_model(BehavioralBaseline, row[0])

    def list_baselines(self, *, limit: int = 100) -> list[BehavioralBaseline]:
        sql = (
            "SELECT payload_json FROM baselines "
            "WHERE project_slug = %s ORDER BY created_at DESC LIMIT %s"
        )
        rows = self._conn.execute(sql, (self._project, limit)).fetchall()
        return [_parse_json_model(BehavioralBaseline, row[0]) for row in rows]

    def save_replay_run(self, run: ReplayRun) -> None:
        self._conn.execute(
            """
            INSERT INTO replay_runs (
                project_slug, run_id, baseline_id, baseline_tag, target_uri,
                started_at, completed_at, payload_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET payload_json = EXCLUDED.payload_json
            """,
            (
                self._project,
                str(run.run_id),
                str(run.baseline_id),
                run.baseline_tag,
                run.target_uri,
                run.started_at.isoformat(),
                run.completed_at.isoformat() if run.completed_at else None,
                run.model_dump_json(),
            ),
        )
        self._conn.commit()

    def get_replay_run(self, run_id: UUID) -> ReplayRun:
        sql = "SELECT payload_json FROM replay_runs WHERE project_slug = %s AND run_id = %s"
        row = self._conn.execute(sql, (self._project, str(run_id))).fetchone()
        if row is None:
            raise StorageError("Replay run not found", context={"run_id": str(run_id)})
        return _parse_json_model(ReplayRun, row[0])

    def list_replay_runs(self, *, limit: int = 200) -> list[ReplayRun]:
        sql = (
            "SELECT payload_json FROM replay_runs "
            "WHERE project_slug = %s ORDER BY started_at ASC LIMIT %s"
        )
        rows = self._conn.execute(sql, (self._project, limit)).fetchall()
        return [_parse_json_model(ReplayRun, row[0]) for row in rows]

    def save_consumer_contract(self, contract: ConsumerContract) -> None:
        self._conn.execute(
            """
            INSERT INTO consumer_contracts (project_slug, agent_id, tool_name, payload_json)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (project_slug, agent_id, tool_name)
            DO UPDATE SET payload_json = EXCLUDED.payload_json
            """,
            (self._project, contract.agent_id, contract.tool_name, contract.model_dump_json()),
        )
        self._conn.commit()

    def list_consumer_contracts(self) -> list[ConsumerContract]:
        sql = (
            "SELECT payload_json FROM consumer_contracts "
            "WHERE project_slug = %s ORDER BY agent_id, tool_name"
        )
        rows = self._conn.execute(sql, (self._project,)).fetchall()
        return [_parse_json_model(ConsumerContract, row[0]) for row in rows]


def _parse_json_model[T: BaseModel](model_cls: type[T], payload: Any) -> T:
    if isinstance(payload, (str, bytes)):
        return model_cls.model_validate_json(payload)
    return model_cls.model_validate(json.loads(json.dumps(payload)))


def open_store(config: RegripConfig) -> StoreBackend:
    """Instantiate Local SQLiteStore or Hosted RemoteStore based on config.storage.mode."""
    if config.storage.mode == StorageMode.REMOTE:
        return RemoteStore(config.storage, config.capture, project_slug=config.project)
    return SQLiteStore(config.storage, config.capture)
