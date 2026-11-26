"""SQLite schema DDL and DSN helpers for Regrip local storage."""

from __future__ import annotations

from pathlib import Path

SCHEMA_SQL = """
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


def sqlite_path_from_dsn(dsn: str) -> Path:
    """Resolve filesystem Path from a sqlite:/// DSN."""
    prefix = "sqlite:///"
    if dsn.startswith(prefix):
        return Path(dsn[len(prefix) :])
    return Path(dsn)
