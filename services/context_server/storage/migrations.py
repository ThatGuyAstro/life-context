"""SQLite schema and forward-only migrations for context storage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


SCHEMA_VERSION = 3


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _set_feature(
    connection: sqlite3.Connection,
    feature: str,
    state: str,
    reason: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO storage_features(feature, state, reason, checked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(feature) DO UPDATE SET
            state = excluded.state,
            reason = excluded.reason,
            checked_at = excluded.checked_at
        """,
        (
            feature,
            state,
            reason,
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        ),
    )


def _migration_1_core(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS storage_features (
            feature TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('available', 'unavailable', 'disabled')),
            reason TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            digest TEXT PRIMARY KEY,
            algorithm TEXT NOT NULL DEFAULT 'sha256',
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            relative_path TEXT NOT NULL UNIQUE,
            retention_tier TEXT NOT NULL CHECK (retention_tier IN ('raw', 'keyframe', 'derived')),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            last_accessed_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT,
            uri TEXT,
            external_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(kind, external_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS captures (
            id TEXT PRIMARY KEY,
            source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'capture',
            external_id TEXT,
            captured_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_id, external_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            capture_id TEXT REFERENCES captures(id) ON DELETE SET NULL,
            source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'observation',
            observed_at TEXT NOT NULL,
            text_content TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT 'null',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            retention_tier TEXT NOT NULL CHECK (retention_tier IN ('raw', 'keyframe', 'derived')),
            expires_at TEXT,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            primary_artifact_digest TEXT REFERENCES artifacts(digest) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead')),
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
            available_at TEXT NOT NULL,
            lease_until TEXT,
            locked_by TEXT,
            last_error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS capture_artifacts (
            capture_id TEXT NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            digest TEXT NOT NULL REFERENCES artifacts(digest) ON DELETE RESTRICT,
            role TEXT NOT NULL DEFAULT 'primary',
            created_at TEXT NOT NULL,
            PRIMARY KEY (capture_id, digest, role)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS observation_artifacts (
            observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
            digest TEXT NOT NULL REFERENCES artifacts(digest) ON DELETE RESTRICT,
            role TEXT NOT NULL DEFAULT 'primary',
            created_at TEXT NOT NULL,
            PRIMARY KEY (observation_id, digest, role)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS job_artifacts (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            digest TEXT NOT NULL REFERENCES artifacts(digest) ON DELETE RESTRICT,
            role TEXT NOT NULL DEFAULT 'input',
            created_at TEXT NOT NULL,
            PRIMARY KEY (job_id, digest, role)
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_2_fts(connection: sqlite3.Connection) -> None:
    """Install FTS5 tables/triggers, recording a feature-level fallback."""

    statements = (
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
            observation_id UNINDEXED,
            text_content,
            metadata,
            tokenize = 'unicode61'
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
            capture_id UNINDEXED,
            content,
            tokenize = 'unicode61'
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
            source_id UNINDEXED,
            content,
            tokenize = 'unicode61'
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
            digest UNINDEXED,
            content,
            tokenize = 'unicode61'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS observations_fts_ai AFTER INSERT ON observations BEGIN
            INSERT INTO observations_fts(rowid, observation_id, text_content, metadata)
            VALUES (new.rowid, new.id, new.text_content, new.metadata_json || ' ' || new.payload_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS observations_fts_ad AFTER DELETE ON observations BEGIN
            DELETE FROM observations_fts WHERE rowid = old.rowid;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS observations_fts_au AFTER UPDATE ON observations BEGIN
            DELETE FROM observations_fts WHERE rowid = old.rowid;
            INSERT INTO observations_fts(rowid, observation_id, text_content, metadata)
            VALUES (new.rowid, new.id, new.text_content, new.metadata_json || ' ' || new.payload_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS captures_fts_ai AFTER INSERT ON captures BEGIN
            INSERT INTO captures_fts(rowid, capture_id, content)
            VALUES (new.rowid, new.id, coalesce(new.kind, '') || ' ' || coalesce(new.external_id, '') || ' ' || new.metadata_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS captures_fts_ad AFTER DELETE ON captures BEGIN
            DELETE FROM captures_fts WHERE rowid = old.rowid;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS captures_fts_au AFTER UPDATE ON captures BEGIN
            DELETE FROM captures_fts WHERE rowid = old.rowid;
            INSERT INTO captures_fts(rowid, capture_id, content)
            VALUES (new.rowid, new.id, coalesce(new.kind, '') || ' ' || coalesce(new.external_id, '') || ' ' || new.metadata_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sources_fts_ai AFTER INSERT ON sources BEGIN
            INSERT INTO sources_fts(rowid, source_id, content)
            VALUES (new.rowid, new.id, coalesce(new.kind, '') || ' ' || coalesce(new.name, '') || ' ' || coalesce(new.uri, '') || ' ' || coalesce(new.external_id, '') || ' ' || new.metadata_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sources_fts_ad AFTER DELETE ON sources BEGIN
            DELETE FROM sources_fts WHERE rowid = old.rowid;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS sources_fts_au AFTER UPDATE ON sources BEGIN
            DELETE FROM sources_fts WHERE rowid = old.rowid;
            INSERT INTO sources_fts(rowid, source_id, content)
            VALUES (new.rowid, new.id, coalesce(new.kind, '') || ' ' || coalesce(new.name, '') || ' ' || coalesce(new.uri, '') || ' ' || coalesce(new.external_id, '') || ' ' || new.metadata_json);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS artifacts_fts_ai AFTER INSERT ON artifacts BEGIN
            INSERT INTO artifacts_fts(rowid, digest, content)
            VALUES (new.rowid, new.digest, coalesce(new.media_type, '') || ' ' || coalesce(new.metadata_json, ''));
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS artifacts_fts_ad AFTER DELETE ON artifacts BEGIN
            DELETE FROM artifacts_fts WHERE rowid = old.rowid;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS artifacts_fts_au AFTER UPDATE ON artifacts BEGIN
            DELETE FROM artifacts_fts WHERE rowid = old.rowid;
            INSERT INTO artifacts_fts(rowid, digest, content)
            VALUES (new.rowid, new.digest, coalesce(new.media_type, '') || ' ' || coalesce(new.metadata_json, ''));
        END
        """,
    )
    try:
        for statement in statements:
            connection.execute(statement)
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).lower() and "no such module" not in str(exc).lower():
            raise
        _set_feature(connection, "fts5", "unavailable", str(exc))
        return
    _set_feature(connection, "fts5", "available", "")


def _migration_3_indexes(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_artifacts_expiration ON artifacts(expires_at) WHERE expires_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_observations_expiration ON observations(expires_at) WHERE expires_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_observations_capture_time ON observations(capture_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS idx_captures_source_time ON captures(source_id, captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, available_at, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_until) WHERE status = 'running'",
        "CREATE INDEX IF NOT EXISTS idx_capture_artifacts_digest ON capture_artifacts(digest)",
        "CREATE INDEX IF NOT EXISTS idx_observation_artifacts_digest ON observation_artifacts(digest)",
        "CREATE INDEX IF NOT EXISTS idx_job_artifacts_digest ON job_artifacts(digest)",
    )
    for statement in statements:
        connection.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core_tables", _migration_1_core),
    Migration(2, "fts5_indexes", _migration_2_fts),
    Migration(3, "retention_and_job_indexes", _migration_3_indexes),
)


def current_schema_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Apply every missing migration atomically and return the schema version."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                ),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
            applied.add(migration.version)
    return current_schema_version(connection)


__all__ = [
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "Migration",
    "apply_migrations",
    "current_schema_version",
]
