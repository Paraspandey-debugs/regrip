"""SQLite WAL metadata store and Zstandard (.jsonl.zst) payload blob store."""

from __future__ import annotations

import json
import sqlite3
from compression import zstd
from pathlib import Path
from uuid import UUID

from regrip.core.payload_normalizer import canonical_hash
from regrip.core.redactor import redact_envelope
from regrip.types.baseline import BehavioralBaseline, ReplayRun
from regrip.types.config import CaptureConfig, StorageConfig
from regrip.types.envelope import InteractionEnvelope
from regrip.types.errors import BaselineError, StorageError
from regrip.types.impact import ConsumerContract

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interactions (
    interaction_id TEXT PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    server_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    is_error INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    blob_ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interactions_server_tool
    ON interactions (server_name, tool_name);

CREATE TABLE IF NOT EXISTS baselines (
    baseline_id TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    tag TEXT NOT NULL,
    git_sha TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(server_name, tag)
);

CREATE TABLE IF NOT EXISTS replay_runs (
    run_id TEXT PRIMARY KEY,
    baseline_id TEXT NOT NULL,
    baseline_tag TEXT NOT NULL,
    target_uri TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumer_contracts (
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (agent_id, tool_name)
);
"""


def _sqlite_path_from_dsn(dsn: str) -> Path:
    prefix = "sqlite:///"
    if dsn.startswith(prefix):
        return Path(dsn[len(prefix) :])
    return Path(dsn)


class SQLiteStore:
    """Local-first interaction, baseline, and replay store using SQLite WAL + zstd blobs."""

    def __init__(self, storage: StorageConfig, capture: CaptureConfig | None = None) -> None:
        self._db_path = _sqlite_path_from_dsn(storage.dsn)
        self._blob_dir = Path(storage.blob_dir)
        self._capture = capture or CaptureConfig()
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._blob_dir.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA_SQL)
        except OSError as exc:
            raise StorageError(f"Failed to initialize SQLite store: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _write_blob(self, envelope: InteractionEnvelope) -> str:
        raw_line = (envelope.model_dump_json() + "\n").encode("utf-8")
        digest = canonical_hash(envelope.model_dump(mode="json"))[:16]
        blob_name = f"{envelope.target.server_name}_{envelope.interaction_id}_{digest}.jsonl.zst"
        blob_path = self._blob_dir / blob_name
        blob_path.write_bytes(zstd.compress(raw_line))
        return blob_name

    def _read_blob(self, blob_ref: str) -> InteractionEnvelope:
        blob_path = self._blob_dir / blob_ref
        if not blob_path.exists():
            raise StorageError("Interaction blob missing", context={"blob_ref": blob_ref})
        raw = zstd.decompress(blob_path.read_bytes()).decode("utf-8").strip()
        return InteractionEnvelope.model_validate_json(raw)

    def save_interaction(self, envelope: InteractionEnvelope) -> InteractionEnvelope:
        """Redact and persist an InteractionEnvelope to blob store and SQLite index."""
        redacted = redact_envelope(envelope, self._capture)
        try:
            blob_ref = self._write_blob(redacted)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO interactions (
                        interaction_id, timestamp_utc, server_name, agent_id, session_id,
                        tool_name, method, status, is_error, duration_ms, blob_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(redacted.interaction_id),
                        redacted.timestamp_utc.isoformat(),
                        redacted.target.server_name,
                        redacted.actor.agent_id,
                        redacted.actor.session_id,
                        redacted.operation.tool_name,
                        redacted.operation.method,
                        redacted.response.status.value,
                        1 if redacted.response.is_error else 0,
                        redacted.telemetry.duration_ms,
                        blob_ref,
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to persist interaction: {exc}") from exc
        return redacted

    def list_interactions(
        self,
        *,
        server_name: str | None = None,
        tool_name: str | None = None,
        limit: int = 500,
    ) -> list[InteractionEnvelope]:
        """Load persisted InteractionEnvelopes matching optional server/tool filters."""
        clauses: list[str] = []
        params: list[str | int] = []
        if server_name:
            clauses.append("server_name = ?")
            params.append(server_name)
        if tool_name:
            clauses.append("tool_name = ?")
            params.append(tool_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT blob_ref FROM interactions {where} ORDER BY timestamp_utc ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to query interactions: {exc}") from exc
        return [self._read_blob(row[0]) for row in rows]

    def save_baseline(self, baseline: BehavioralBaseline) -> None:
        """Persist a versioned BehavioralBaseline indexed by (server_name, tag)."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO baselines (
                        baseline_id, server_name, tag, git_sha, content_hash, created_at,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(baseline.baseline_id),
                        baseline.server_name,
                        baseline.tag,
                        baseline.git_sha,
                        baseline.content_hash,
                        baseline.created_at.isoformat(),
                        baseline.model_dump_json(),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to save baseline: {exc}") from exc

    def get_baseline_by_tag(self, server_name: str, tag: str) -> BehavioralBaseline:
        """Load a BehavioralBaseline by (server_name, tag)."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM baselines WHERE server_name = ? AND tag = ?",
                    (server_name, tag),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to load baseline: {exc}") from exc
        if row is None:
            raise BaselineError(
                "Baseline not found", context={"server_name": server_name, "tag": tag}
            )
        return BehavioralBaseline.model_validate_json(row[0])

    def list_baselines(self, *, limit: int = 100) -> list[BehavioralBaseline]:
        """List stored BehavioralBaselines ordered by creation time."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM baselines ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list baselines: {exc}") from exc
        return [BehavioralBaseline.model_validate_json(row[0]) for row in rows]

    def save_replay_run(self, run: ReplayRun) -> None:
        """Persist a completed ReplayRun."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO replay_runs (
                        run_id, baseline_id, baseline_tag, target_uri, started_at,
                        completed_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(run.run_id),
                        str(run.baseline_id),
                        run.baseline_tag,
                        run.target_uri,
                        run.started_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        run.model_dump_json(),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to save replay run: {exc}") from exc

    def get_replay_run(self, run_id: UUID) -> ReplayRun:
        """Fetch a persisted ReplayRun by UUID."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM replay_runs WHERE run_id = ?", (str(run_id),)
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to load replay run: {exc}") from exc
        if row is None:
            raise StorageError("Replay run not found", context={"run_id": str(run_id)})
        return ReplayRun.model_validate(json.loads(row[0]))

    def list_replay_runs(self, *, limit: int = 200) -> list[ReplayRun]:
        """List persisted ReplayRuns ordered chronologically."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM replay_runs ORDER BY started_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list replay runs: {exc}") from exc
        return [ReplayRun.model_validate(json.loads(row[0])) for row in rows]

    def save_consumer_contract(self, contract: ConsumerContract) -> None:
        """Upsert a ConsumerContract indexed by (agent_id, tool_name)."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO consumer_contracts (agent_id, tool_name, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (contract.agent_id, contract.tool_name, contract.model_dump_json()),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to save consumer contract: {exc}") from exc

    def list_consumer_contracts(self) -> list[ConsumerContract]:
        """List all persisted ConsumerContracts."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM consumer_contracts ORDER BY agent_id, tool_name"
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list consumer contracts: {exc}") from exc
        return [ConsumerContract.model_validate_json(row[0]) for row in rows]
