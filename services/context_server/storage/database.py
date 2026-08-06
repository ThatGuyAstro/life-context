"""SQLite-backed context storage: records, jobs, indexes, and retention."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .artifacts import ArtifactStore
from .migrations import SCHEMA_VERSION, apply_migrations, current_schema_version
from .models import (
    ArtifactRecord,
    CaptureRecord,
    JobRecord,
    JobRecoveryResult,
    ObservationRecord,
    PurgeResult,
    RetentionPolicy,
    RetentionTier,
    SourceRecord,
    SqliteVecStatus,
    ensure_utc,
    parse_timestamp,
    timestamp,
    utc_now,
)
from .vector import (
    VectorFeatureUnavailable,
    create_vector_index as create_sqlite_vec_index,
    load_sqlite_vec,
)
from .paths import normalize_digest, relative_content_addressed_path


class IdempotencyConflict(RuntimeError):
    """Raised when a caller reuses a key for an incompatible operation."""


class JobStateError(RuntimeError):
    """Raised when a job transition is not valid for its current state."""


class ArtifactConflict(RuntimeError):
    """Raised when digest metadata disagrees with an existing artifact row."""


_OWNER_TABLES = {
    "capture": "capture_artifacts",
    "captures": "capture_artifacts",
    "observation": "observation_artifacts",
    "observations": "observation_artifacts",
    "job": "job_artifacts",
    "jobs": "job_artifacts",
}

_SOURCE_FIELD_NAMES = frozenset(
    {
        "source",
        "source_id",
        "source_kind",
        "source_type",
        "origin",
    }
)
_PROJECT_FIELD_NAMES = frozenset({"project", "project_id", "project_name"})


def _selector_values(value: Any) -> tuple[str, ...] | None:
    """Normalize a scalar or collection selector without treating empty input as unscoped."""

    if value is None:
        return None
    if isinstance(value, str):
        return (value.strip(),)
    if isinstance(value, bytes):
        return (value.decode("utf-8", errors="replace").strip(),)
    if isinstance(value, Iterable):
        return tuple(str(getattr(item, "value", item)).strip() for item in value)
    return (str(value).strip(),)


def _scalar_values(value: Any) -> set[str]:
    if isinstance(value, (str, int, float, bool)):
        return {str(value)}
    if isinstance(value, Mapping):
        values: set[str] = set()
        for child in value.values():
            values.update(_scalar_values(child))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        values = set()
        for child in value:
            values.update(_scalar_values(child))
        return values
    return set()


def _json_field_values(value: Any, field_names: frozenset[str]) -> set[str]:
    """Collect values for scope fields from nested JSON metadata/payloads."""

    values: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in field_names:
                values.update(_scalar_values(child))
            values.update(_json_field_values(child, field_names))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            values.update(_json_field_values(child, field_names))
    return values


def _selector_matches(values: set[str], selector: tuple[str, ...] | None) -> bool:
    if selector is None:
        return True
    return bool(values.intersection(selector))


class SQLiteStorage:
    """Thread-safe storage facade around one SQLite connection.

    The class owns the connection and artifact store, making an in-memory DB
    useful in tests while file-backed databases get WAL mode and durable
    forward-only migrations.  All public writes are transaction-scoped.
    """

    def __init__(
        self,
        database: str | os.PathLike[str] = ":memory:",
        *,
        db_path: str | os.PathLike[str] | None = None,
        artifact_root: str | os.PathLike[str] | None = None,
        retention_policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 30.0,
        load_sqlite_vec_extension: bool = True,
        sqlite_vec_extension: str | os.PathLike[str] | None = None,
    ) -> None:
        if db_path is not None:
            if database != ":memory:" and str(database) != str(db_path):
                raise ValueError("database and db_path specify different SQLite files")
            database = db_path
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._clock = clock or utc_now
        self.retention_policy = retention_policy or RetentionPolicy()
        self._configure_connection(timeout)
        apply_migrations(self._connection)
        if artifact_root is None:
            if self.database == ":memory:":
                artifact_root = Path.cwd() / ".context-artifacts"
            else:
                artifact_root = Path(self.database).with_name(Path(self.database).name + ".artifacts")
        self.artifacts = ArtifactStore(self, artifact_root)
        self.artifact_store = self.artifacts
        self.sqlite_vec = load_sqlite_vec(
            self._connection,
            sqlite_vec_extension,
            enabled=load_sqlite_vec_extension,
        )

    def _configure_connection(self, timeout: float) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._connection)

    @property
    def sqlite_vec_status(self) -> SqliteVecStatus:
        return self.sqlite_vec

    @property
    def fts5_available(self) -> bool:
        return self.feature_status("fts5")["state"] == "available"

    def feature_status(self, feature: str) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT feature, state, reason, checked_at FROM storage_features WHERE feature = ?",
            (feature,),
        ).fetchone()
        if row is None:
            return {"feature": feature, "state": "unavailable", "reason": "not checked", "checked_at": ""}
        return {key: str(row[key] or "") for key in ("feature", "state", "reason", "checked_at")}

    def reload_sqlite_vec(
        self,
        extension_path: str | os.PathLike[str] | None = None,
    ) -> SqliteVecStatus:
        with self._lock:
            self.sqlite_vec = load_sqlite_vec(self._connection, extension_path, enabled=True)
            return self.sqlite_vec

    def create_vector_index(self, table_name: str, dimensions: int) -> None:
        with self._lock:
            create_sqlite_vec_index(self._connection, table_name, dimensions)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Start an IMMEDIATE transaction, nesting through savepoints."""

        with self._lock:
            savepoint = f"storage_sp_{self._transaction_depth}"
            if self._transaction_depth == 0:
                self._connection.execute("BEGIN IMMEDIATE")
            else:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield self._connection
            except Exception:
                self._transaction_depth -= 1
                if self._transaction_depth == 0:
                    self._connection.rollback()
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._transaction_depth -= 1
                if self._transaction_depth == 0:
                    self._connection.commit()
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _now(self, value: datetime | str | None = None) -> datetime:
        return ensure_utc(value if value is not None else self._clock())

    @staticmethod
    def _json(value: Any, default: Any = None) -> str:
        if value is None:
            value = default
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(value: str | None, default: Any = None) -> Any:
        if value is None:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Artifact metadata and references
    # ------------------------------------------------------------------
    def register_artifact(
        self,
        *,
        digest: str,
        size_bytes: int,
        media_type: str,
        relative_path: str | os.PathLike[str] | None = None,
        retention_tier: RetentionTier | str = RetentionTier.DERIVED,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | str | None = None,
        expires_at: datetime | str | None = None,
    ) -> ArtifactRecord:
        normalized = normalize_digest(digest)
        tier = RetentionTier.coerce(retention_tier)
        created = self._now(created_at)
        expires = (
            self._now(expires_at)
            if expires_at is not None
            else self.retention_policy.expires_at(tier, created)
        )
        relative = str(relative_path or relative_content_addressed_path(normalized)).replace("\\", "/")
        relative_parts = Path(relative).parts
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in relative_parts
            or relative == "."
            or relative.startswith(".staging/")
        ):
            raise ValueError("artifact relative_path must stay inside the artifact root")
        metadata_json = self._json(dict(metadata or {}), default={})
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE digest = ?", (normalized,)
            ).fetchone()
            if existing is not None:
                if int(existing["size_bytes"]) != int(size_bytes) or str(existing["relative_path"]) != relative:
                    raise ArtifactConflict(f"artifact metadata conflicts for digest {normalized}")
                return self._artifact_from_row(existing)
            connection.execute(
                """
                INSERT INTO artifacts(
                    digest, algorithm, size_bytes, media_type, relative_path,
                    retention_tier, created_at, expires_at, metadata_json
                ) VALUES (?, 'sha256', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    int(size_bytes),
                    media_type,
                    relative,
                    tier.value,
                    timestamp(created),
                    timestamp(expires) if expires else None,
                    metadata_json,
                ),
            )
            return self._artifact_from_row(
                connection.execute("SELECT * FROM artifacts WHERE digest = ?", (normalized,)).fetchone()
            )

    def get_artifact(self, digest: str) -> ArtifactRecord | None:
        normalized = normalize_digest(digest)
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE digest = ?", (normalized,)
        ).fetchone()
        return self._artifact_from_row(row) if row else None

    def list_artifacts(self) -> list[ArtifactRecord]:
        rows = self._connection.execute("SELECT * FROM artifacts ORDER BY created_at, digest").fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def touch_artifact(self, digest: str, accessed_at: datetime | str | None = None) -> None:
        normalized = normalize_digest(digest)
        self._connection.execute(
            "UPDATE artifacts SET last_accessed_at = ? WHERE digest = ?",
            (timestamp(self._now(accessed_at)), normalized),
        )

    def artifact_reference_count(self, digest: str) -> int:
        normalized = normalize_digest(digest)
        return self._artifact_reference_count(self._connection, normalized)

    @staticmethod
    def _artifact_reference_count(connection: sqlite3.Connection, digest: str) -> int:
        total = 0
        for table in ("capture_artifacts", "observation_artifacts", "job_artifacts"):
            row = connection.execute(f"SELECT count(*) FROM {table} WHERE digest = ?", (digest,)).fetchone()
            total += int(row[0]) if row else 0
        # Older/imported rows may retain only the denormalized primary digest.
        # Count those references only when the normalized link row is absent so
        # an ordinary primary observation still reports one reference.
        row = connection.execute(
            """
            SELECT count(*)
            FROM observations AS observations
            WHERE observations.primary_artifact_digest = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM observation_artifacts AS links
                  WHERE links.observation_id = observations.id
                    AND links.digest = observations.primary_artifact_digest
              )
            """,
            (digest,),
        ).fetchone()
        total += int(row[0]) if row else 0
        return total

    def delete_artifact_record(self, digest: str, *, force: bool = False) -> bool:
        normalized = normalize_digest(digest)
        with self.transaction() as connection:
            refs = self._artifact_reference_count(connection, normalized)
            if refs and not force:
                raise RuntimeError(f"artifact {normalized} has {refs} references")
            if force:
                for table in ("capture_artifacts", "observation_artifacts", "job_artifacts"):
                    connection.execute(f"DELETE FROM {table} WHERE digest = ?", (normalized,))
            cursor = connection.execute("DELETE FROM artifacts WHERE digest = ?", (normalized,))
            return cursor.rowcount > 0

    def attach_artifact(
        self,
        owner_type: str,
        owner_id: str,
        digest: str,
        *,
        role: str = "primary",
    ) -> None:
        table = _OWNER_TABLES.get(owner_type.lower())
        if table is None:
            raise ValueError(f"unknown artifact owner type: {owner_type!r}")
        normalized = normalize_digest(digest)
        now = timestamp(self._now())
        id_column = {
            "capture_artifacts": "capture_id",
            "observation_artifacts": "observation_id",
            "job_artifacts": "job_id",
        }[table]
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM artifacts WHERE digest = ?", (normalized,)).fetchone() is None:
                raise KeyError(f"unknown artifact: {normalized}")
            connection.execute(
                f"INSERT OR IGNORE INTO {table}({id_column}, digest, role, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, normalized, role, now),
            )
            if table == "observation_artifacts" and role == "primary":
                connection.execute(
                    "UPDATE observations SET primary_artifact_digest = ? WHERE id = ?",
                    (normalized, owner_id),
                )

    link_artifact = attach_artifact

    # ------------------------------------------------------------------
    # Sources, captures, and observations
    # ------------------------------------------------------------------
    def create_source(
        self,
        kind: str | None = None,
        *,
        source_type: str | None = None,
        name: str | None = None,
        uri: str | None = None,
        external_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_id: str | None = None,
        now: datetime | str | None = None,
    ) -> SourceRecord:
        source_kind = kind or source_type
        if not source_kind:
            raise ValueError("source kind/source_type is required")
        identifier = source_id or str(uuid.uuid4())
        created = self._now(now)
        metadata_json = self._json(dict(metadata or {}), default={})
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM sources WHERE id = ?", (identifier,)).fetchone()
            if existing is None and external_id is not None:
                existing = connection.execute(
                    "SELECT * FROM sources WHERE kind = ? AND external_id = ?",
                    (source_kind, external_id),
                ).fetchone()
            if existing is not None:
                return self._source_from_row(existing)
            connection.execute(
                """
                INSERT INTO sources(id, kind, name, uri, external_id, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    source_kind,
                    name,
                    uri,
                    external_id,
                    metadata_json,
                    timestamp(created),
                    timestamp(created),
                ),
            )
            return self._source_from_row(
                connection.execute("SELECT * FROM sources WHERE id = ?", (identifier,)).fetchone()
            )

    upsert_source = create_source

    def get_source(self, source_id: str) -> SourceRecord | None:
        row = self._connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_from_row(row) if row else None

    def create_capture(
        self,
        source_id: str | None = None,
        *,
        kind: str = "capture",
        external_id: str | None = None,
        captured_at: datetime | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        capture_id: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | str | None = None,
    ) -> CaptureRecord:
        identifier = capture_id or str(uuid.uuid4())
        created = self._now(now)
        captured = self._now(captured_at) if captured_at is not None else created
        metadata_json = self._json(dict(metadata or {}), default={})
        with self.transaction() as connection:
            if source_id is not None and connection.execute(
                "SELECT 1 FROM sources WHERE id = ?", (source_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown source: {source_id}")
            existing = None
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM captures WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            if existing is None and external_id is not None:
                existing = connection.execute(
                    "SELECT * FROM captures WHERE source_id IS ? AND external_id = ?",
                    (source_id, external_id),
                ).fetchone()
            if existing is not None:
                return self._capture_from_row(existing)
            connection.execute(
                """
                INSERT INTO captures(
                    id, source_id, kind, external_id, captured_at, metadata_json,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    source_id,
                    kind,
                    external_id,
                    timestamp(captured),
                    metadata_json,
                    idempotency_key,
                    timestamp(created),
                    timestamp(created),
                ),
            )
            return self._capture_from_row(
                connection.execute("SELECT * FROM captures WHERE id = ?", (identifier,)).fetchone()
            )

    record_capture = create_capture

    def get_capture(self, capture_id: str) -> CaptureRecord | None:
        row = self._connection.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        return self._capture_from_row(row) if row else None

    def create_observation(
        self,
        capture_id: str | None = None,
        *,
        source_id: str | None = None,
        kind: str = "observation",
        observed_at: datetime | str | None = None,
        text_content: str | None = None,
        text: str | None = None,
        payload: Any = None,
        metadata: Mapping[str, Any] | None = None,
        retention_tier: RetentionTier | str = RetentionTier.DERIVED,
        expires_at: datetime | str | None = None,
        observation_id: str | None = None,
        idempotency_key: str | None = None,
        artifact_digest: str | None = None,
        now: datetime | str | None = None,
    ) -> ObservationRecord:
        identifier = observation_id or str(uuid.uuid4())
        created = self._now(now)
        observed = self._now(observed_at) if observed_at is not None else created
        tier = RetentionTier.coerce(retention_tier)
        expiry = (
            self._now(expires_at)
            if expires_at is not None
            else self.retention_policy.expires_at(tier, observed)
        )
        content = text_content if text_content is not None else (text or "")
        metadata_json = self._json(dict(metadata or {}), default={})
        payload_json = self._json(payload, default=None)
        with self.transaction() as connection:
            if capture_id is not None:
                capture_row = connection.execute(
                    "SELECT source_id FROM captures WHERE id = ?", (capture_id,)
                ).fetchone()
                if capture_row is None:
                    raise KeyError(f"unknown capture: {capture_id}")
                if source_id is None:
                    source_id = capture_row["source_id"]
            if source_id is not None and connection.execute(
                "SELECT 1 FROM sources WHERE id = ?", (source_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown source: {source_id}")
            existing = None
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM observations WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            if existing is not None:
                record = self._observation_from_row(existing)
                if artifact_digest is not None:
                    normalized_artifact = normalize_digest(artifact_digest)
                    self._attach_artifact_in_transaction(
                        connection, "observation_artifacts", "observation_id", record.id, normalized_artifact
                    )
                    connection.execute(
                        "UPDATE observations SET primary_artifact_digest = ? WHERE id = ?",
                        (normalized_artifact, record.id),
                    )
                return record
            if artifact_digest is not None:
                normalized_artifact = normalize_digest(artifact_digest)
                if connection.execute(
                    "SELECT 1 FROM artifacts WHERE digest = ?", (normalized_artifact,)
                ).fetchone() is None:
                    raise KeyError(f"unknown artifact: {normalized_artifact}")
            connection.execute(
                """
                INSERT INTO observations(
                    id, capture_id, source_id, kind, observed_at, text_content,
                    payload_json, metadata_json, retention_tier, expires_at,
                    idempotency_key, created_at, updated_at, primary_artifact_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    capture_id,
                    source_id,
                    kind,
                    timestamp(observed),
                    content,
                    payload_json,
                    metadata_json,
                    tier.value,
                    timestamp(expiry) if expiry else None,
                    idempotency_key,
                    timestamp(created),
                    timestamp(created),
                    normalize_digest(artifact_digest) if artifact_digest else None,
                ),
            )
            if artifact_digest is not None:
                self._attach_artifact_in_transaction(
                    connection, "observation_artifacts", "observation_id", identifier, artifact_digest
                )
            return self._observation_from_row(
                connection.execute("SELECT * FROM observations WHERE id = ?", (identifier,)).fetchone()
            )

    record_observation = create_observation

    def get_observation(self, observation_id: str) -> ObservationRecord | None:
        row = self._connection.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        return self._observation_from_row(row) if row else None

    def search_observations(self, query: str, *, limit: int = 50, offset: int = 0) -> list[ObservationRecord]:
        rows = self._search_fts("observations_fts", "observation_id", query, limit, offset)
        if not rows:
            return []
        ids = [str(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        records = self._connection.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {str(row["id"]): self._observation_from_row(row) for row in records}
        return [by_id[item] for item in ids if item in by_id]

    def search_captures(self, query: str, *, limit: int = 50, offset: int = 0) -> list[CaptureRecord]:
        rows = self._search_fts("captures_fts", "capture_id", query, limit, offset)
        if not rows:
            return []
        ids = [str(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        records = self._connection.execute(
            f"SELECT * FROM captures WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {str(row["id"]): self._capture_from_row(row) for row in records}
        return [by_id[item] for item in ids if item in by_id]

    def search_sources(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SourceRecord]:
        rows = self._search_fts("sources_fts", "source_id", query, limit, offset)
        if not rows:
            return []
        ids = [str(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        records = self._connection.execute(
            f"SELECT * FROM sources WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {str(row["id"]): self._source_from_row(row) for row in records}
        return [by_id[item] for item in ids if item in by_id]

    def rebuild_fts_indexes(self) -> None:
        if not self.fts5_available:
            return
        with self.transaction() as connection:
            connection.execute("DELETE FROM observations_fts")
            connection.execute(
                "INSERT INTO observations_fts(rowid, observation_id, text_content, metadata) SELECT rowid, id, text_content, metadata_json || ' ' || payload_json FROM observations"
            )
            connection.execute("DELETE FROM captures_fts")
            connection.execute(
                "INSERT INTO captures_fts(rowid, capture_id, content) SELECT rowid, id, coalesce(kind, '') || ' ' || coalesce(external_id, '') || ' ' || metadata_json FROM captures"
            )
            connection.execute("DELETE FROM sources_fts")
            connection.execute(
                "INSERT INTO sources_fts(rowid, source_id, content) SELECT rowid, id, coalesce(kind, '') || ' ' || coalesce(name, '') || ' ' || coalesce(uri, '') || ' ' || coalesce(external_id, '') || ' ' || metadata_json FROM sources"
            )
            connection.execute("DELETE FROM artifacts_fts")
            connection.execute(
                "INSERT INTO artifacts_fts(rowid, digest, content) SELECT rowid, digest, coalesce(media_type, '') || ' ' || coalesce(metadata_json, '') FROM artifacts"
            )

    def _search_fts(
        self,
        table: str,
        id_column: str,
        query: str,
        limit: int,
        offset: int,
    ) -> list[sqlite3.Row]:
        if not self.fts5_available or not query.strip() or limit <= 0:
            return []
        statement = (
            f"SELECT {id_column} FROM {table} "
            f"WHERE {table} MATCH ? ORDER BY rank, {id_column} COLLATE BINARY "
            "LIMIT ? OFFSET ?"
        )
        try:
            return self._connection.execute(statement, (query, int(limit), int(offset))).fetchall()
        except sqlite3.OperationalError:
            # A free-form user query may contain FTS operators.  Quoting it
            # gives a useful literal fallback while preserving MATCH support.
            literal = '"' + query.replace('"', '""') + '"'
            return self._connection.execute(statement, (literal, int(limit), int(offset))).fetchall()

    # ------------------------------------------------------------------
    # Idempotent jobs and crash/lease recovery
    # ------------------------------------------------------------------
    def enqueue_job(
        self,
        kind: str,
        payload: Any = None,
        *,
        idempotency_key: str | None = None,
        available_at: datetime | str | None = None,
        max_attempts: int = 3,
        job_id: str | None = None,
        now: datetime | str | None = None,
    ) -> JobRecord:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        identifier = job_id or str(uuid.uuid4())
        key = idempotency_key or identifier
        created = self._now(now)
        available = self._now(available_at) if available_at is not None else created
        payload_json = self._json(payload, default={})
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return self._job_from_row(existing)
            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, status, payload_json, idempotency_key, attempts,
                    max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    kind,
                    payload_json,
                    key,
                    int(max_attempts),
                    timestamp(available),
                    timestamp(created),
                    timestamp(created),
                ),
            )
            return self._job_from_row(
                connection.execute("SELECT * FROM jobs WHERE id = ?", (identifier,)).fetchone()
            )

    create_job = enqueue_job

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def recover_jobs(
        self,
        *,
        now: datetime | str | None = None,
    ) -> JobRecoveryResult:
        current = self._now(now)
        current_text = timestamp(current)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?",
                (current_text,),
            ).fetchall()
            ids = tuple(str(row["id"]) for row in rows)
            if ids:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending', lease_until = NULL, locked_by = NULL,
                        available_at = ?, last_error = coalesce(last_error, 'lease expired'),
                        updated_at = ?
                    WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?
                    """,
                    (current_text, current_text, current_text),
                )
            return JobRecoveryResult(recovered_jobs=len(ids), job_ids=ids)

    reap_expired_jobs = recover_jobs

    def claim_next_job(
        self,
        worker_id: str = "worker",
        *,
        lease_seconds: float = 300,
        now: datetime | str | None = None,
    ) -> JobRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        current = self._now(now)
        current_text = timestamp(current)
        lease = timestamp(current + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', lease_until = NULL, locked_by = NULL,
                    available_at = ?, last_error = coalesce(last_error, 'lease expired'),
                    updated_at = ?
                WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (current_text, current_text, current_text),
            )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'pending' AND available_at <= ?
                ORDER BY available_at, created_at, id
                LIMIT 1
                """,
                (current_text,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, locked_by = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (lease, worker_id, current_text, row["id"]),
            )
            return self._job_from_row(
                connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            )

    claim_job = claim_next_job

    def complete_job(
        self,
        job_id: str,
        result: Any = None,
        *,
        now: datetime | str | None = None,
    ) -> JobRecord:
        current = self._now(now)
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            if row["status"] == "succeeded":
                return self._job_from_row(row)
            if row["status"] != "running":
                raise JobStateError(f"job {job_id} is {row['status']}, not running")
            connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', result_json = ?, lease_until = NULL,
                    locked_by = NULL, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (self._json(result, default=None), timestamp(current), timestamp(current), job_id),
            )
            return self._job_from_row(
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            )

    def fail_job(
        self,
        job_id: str,
        error: str,
        *,
        retry_delay: float | timedelta = 0,
        now: datetime | str | None = None,
    ) -> JobRecord:
        current = self._now(now)
        delay = retry_delay.total_seconds() if isinstance(retry_delay, timedelta) else float(retry_delay)
        if delay < 0:
            raise ValueError("retry_delay cannot be negative")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            if row["status"] in ("succeeded", "dead"):
                return self._job_from_row(row)
            if row["status"] != "running":
                raise JobStateError(f"job {job_id} is {row['status']}, not running")
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = "dead" if terminal else "pending"
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, available_at = ?, lease_until = NULL,
                    locked_by = NULL, updated_at = ?, completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (
                    status,
                    str(error),
                    timestamp(current + timedelta(seconds=delay)),
                    timestamp(current),
                    terminal,
                    timestamp(current) if terminal else None,
                    job_id,
                ),
            )
            return self._job_from_row(
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            )

    mark_job_failed = fail_job

    def link_job_artifact(self, job_id: str, digest: str, *, role: str = "input") -> None:
        self.attach_artifact("job", job_id, digest, role=role)

    # ------------------------------------------------------------------
    # Retention purge and recovery facade
    # ------------------------------------------------------------------
    def purge_expired(
        self,
        *,
        as_of: datetime | str | None = None,
        dry_run: bool = False,
        before: datetime | str | None = None,
        date: datetime | str | None = None,
        source: str | Iterable[str] | None = None,
        project: str | Iterable[str] | None = None,
        retention_tiers: Any = None,
        retention_tier: Any = None,
    ) -> PurgeResult:
        """Purge retention- or date-eligible records within an optional scope.

        ``before`` retains the original strict observation-date behavior while
        also making old artifact rows eligible for cleanup.  Without a date,
        the existing expiration timestamps remain authoritative.  Scope
        selectors are matched against normalized source relationships and the
        source/project fields carried in metadata or payload JSON.
        """

        current = self._now(as_of)
        if before is not None and date is not None:
            before_value = self._now(before)
            if before_value != self._now(date):
                raise ValueError("before and date specify different purge boundaries")
        else:
            boundary = before if before is not None else date
            before_value = self._now(boundary) if boundary is not None else None

        source_selector = _selector_values(source)
        project_selector = _selector_values(project)
        tier_values: list[Any] = []
        for raw_tiers in (retention_tiers, retention_tier):
            if raw_tiers is None:
                continue
            normalized = _selector_values(raw_tiers)
            if normalized is not None:
                tier_values.extend(normalized)
        tier_selector = (
            frozenset(RetentionTier.coerce(value) for value in tier_values)
            if retention_tiers is not None or retention_tier is not None
            else None
        )

        files_to_remove: list[Path] = []
        file_errors: list[str] = []
        missing_files = 0
        with self.transaction() as connection:
            source_rows = {
                str(row["id"]): row
                for row in connection.execute("SELECT * FROM sources").fetchall()
            }
            capture_rows = {
                str(row["id"]): row
                for row in connection.execute("SELECT * FROM captures").fetchall()
            }
            observation_rows = {
                str(row["id"]): row
                for row in connection.execute("SELECT * FROM observations").fetchall()
            }
            observations_by_capture: dict[str, list[str]] = {}
            primary_artifact_links: dict[str, list[str]] = {}
            for row in observation_rows.values():
                capture_id = row["capture_id"]
                if capture_id is not None:
                    observations_by_capture.setdefault(str(capture_id), []).append(str(row["id"]))
                digest = row["primary_artifact_digest"]
                if digest is not None:
                    primary_artifact_links.setdefault(str(digest), []).append(str(row["id"]))
            job_rows = {
                str(row["id"]): row
                for row in connection.execute("SELECT * FROM jobs").fetchall()
            }

            def add_json_scope(
                source_values: set[str], project_values: set[str], value: Any
            ) -> None:
                source_values.update(_json_field_values(value, _SOURCE_FIELD_NAMES))
                project_values.update(_json_field_values(value, _PROJECT_FIELD_NAMES))

            def add_source_scope(
                source_values: set[str], project_values: set[str], source_id: Any
            ) -> None:
                if source_id is None:
                    return
                identifier = str(source_id)
                source_values.add(identifier)
                source_row = source_rows.get(identifier)
                if source_row is None:
                    return
                for key in ("id", "kind", "name", "uri", "external_id"):
                    value = source_row[key]
                    if value is not None:
                        source_values.add(str(value))
                add_json_scope(
                    source_values,
                    project_values,
                    self._decode(source_row["metadata_json"], default={}),
                )

            def observation_scope(row: sqlite3.Row) -> tuple[set[str], set[str]]:
                source_values: set[str] = set()
                project_values: set[str] = set()
                add_source_scope(source_values, project_values, row["source_id"])
                capture_id = row["capture_id"]
                capture = capture_rows.get(str(capture_id)) if capture_id is not None else None
                if capture is not None:
                    add_source_scope(source_values, project_values, capture["source_id"])
                    add_json_scope(
                        source_values,
                        project_values,
                        self._decode(capture["metadata_json"], default={}),
                    )
                add_json_scope(
                    source_values,
                    project_values,
                    self._decode(row["metadata_json"], default={}),
                )
                add_json_scope(
                    source_values,
                    project_values,
                    self._decode(row["payload_json"], default=None),
                )
                return source_values, project_values

            def job_scope(row: sqlite3.Row) -> tuple[set[str], set[str]]:
                source_values: set[str] = set()
                project_values: set[str] = set()
                payload = self._decode(row["payload_json"], default={})
                add_json_scope(source_values, project_values, payload)
                if isinstance(payload, Mapping):
                    observation_id = payload.get("observation_id")
                    if observation_id is not None and str(observation_id) in observation_rows:
                        linked_source, linked_project = observation_scope(
                            observation_rows[str(observation_id)]
                        )
                        source_values.update(linked_source)
                        project_values.update(linked_project)
                    capture_id = payload.get("capture_id")
                    capture = capture_rows.get(str(capture_id)) if capture_id is not None else None
                    if capture is not None:
                        add_source_scope(source_values, project_values, capture["source_id"])
                        add_json_scope(
                            source_values,
                            project_values,
                            self._decode(capture["metadata_json"], default={}),
                        )
                return source_values, project_values

            def matches_scope(scope: tuple[set[str], set[str]]) -> bool:
                return _selector_matches(scope[0], source_selector) and _selector_matches(
                    scope[1], project_selector
                )

            def matches_tier(row: sqlite3.Row, *, artifact: bool = False) -> bool:
                tier = RetentionTier.coerce(row["retention_tier"])
                if tier_selector is not None:
                    return tier in tier_selector
                # An explicit date purge has historically selected every old
                # observation, but only short-lived raw artifacts are safe to
                # infer from that date alone.  Keyframes/derived artifacts can
                # be opted into explicitly with retention_tiers.
                return not artifact or before_value is None or tier is RetentionTier.RAW

            def is_eligible(
                row: sqlite3.Row, timestamp_column: str, *, artifact: bool = False
            ) -> bool:
                if not matches_tier(row, artifact=artifact):
                    return False
                if before_value is not None:
                    return ensure_utc(row[timestamp_column]) < before_value
                expires = row["expires_at"]
                return expires is not None and ensure_utc(expires) <= current

            eligible_observation_rows = [
                row
                for row in observation_rows.values()
                if is_eligible(row, "observed_at") and matches_scope(observation_scope(row))
            ]

            capture_links: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT digest, capture_id FROM capture_artifacts"
            ).fetchall():
                capture_links.setdefault(str(row["digest"]), []).append(str(row["capture_id"]))
            observation_links: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT digest, observation_id FROM observation_artifacts"
            ).fetchall():
                observation_links.setdefault(str(row["digest"]), []).append(
                    str(row["observation_id"])
                )
            job_links: dict[str, list[str]] = {}
            for row in connection.execute("SELECT digest, job_id FROM job_artifacts").fetchall():
                job_links.setdefault(str(row["digest"]), []).append(str(row["job_id"]))

            def artifact_scope(row: sqlite3.Row) -> tuple[set[str], set[str]]:
                source_values: set[str] = set()
                project_values: set[str] = set()
                add_json_scope(
                    source_values,
                    project_values,
                    self._decode(row["metadata_json"], default={}),
                )
                digest = str(row["digest"])
                for capture_id in capture_links.get(digest, []):
                    capture = capture_rows.get(capture_id)
                    if capture is None:
                        continue
                    add_source_scope(source_values, project_values, capture["source_id"])
                    add_json_scope(
                        source_values,
                        project_values,
                        self._decode(capture["metadata_json"], default={}),
                    )
                    for observation_id in observations_by_capture.get(capture_id, []):
                        observation = observation_rows.get(observation_id)
                        if observation is None:
                            continue
                        linked_source, linked_project = observation_scope(observation)
                        source_values.update(linked_source)
                        project_values.update(linked_project)
                for observation_id in observation_links.get(digest, []):
                    observation = observation_rows.get(observation_id)
                    if observation is None:
                        continue
                    linked_source, linked_project = observation_scope(observation)
                    source_values.update(linked_source)
                    project_values.update(linked_project)
                for observation_id in primary_artifact_links.get(digest, []):
                    observation = observation_rows.get(observation_id)
                    if observation is None:
                        continue
                    linked_source, linked_project = observation_scope(observation)
                    source_values.update(linked_source)
                    project_values.update(linked_project)
                for job_id in job_links.get(digest, []):
                    job = job_rows.get(job_id)
                    if job is None:
                        continue
                    linked_source, linked_project = job_scope(job)
                    source_values.update(linked_source)
                    project_values.update(linked_project)
                return source_values, project_values

            eligible_artifact_rows = [
                row
                for row in connection.execute("SELECT * FROM artifacts").fetchall()
                if is_eligible(row, "created_at", artifact=True)
                and matches_scope(artifact_scope(row))
            ]
            reference_counts = {
                str(row["digest"]): self._artifact_reference_count(connection, str(row["digest"]))
                for row in eligible_artifact_rows
            }
            skipped = sum(1 for count in reference_counts.values() if count > 0)
            eligible_observations = len(eligible_observation_rows)
            eligible_artifacts = len(eligible_artifact_rows)

            if dry_run:
                return PurgeResult(
                    as_of=current,
                    dry_run=True,
                    eligible_observations=eligible_observations,
                    eligible_artifacts=eligible_artifacts,
                    expired_observations=eligible_observations,
                    expired_artifacts=eligible_artifacts,
                    skipped_referenced_artifacts=skipped,
                )

            deleted_artifacts = 0
            for row in eligible_artifact_rows:
                digest = str(row["digest"])
                # Reference counts are deliberately taken before deleting any
                # selected record: evidence referenced by a record in this
                # purge remains recoverable rather than being deleted as a
                # side effect of the same request.
                if reference_counts[digest] > 0:
                    continue
                connection.execute("DELETE FROM artifacts WHERE digest = ?", (digest,))
                deleted_artifacts += 1
                files_to_remove.append(self.artifacts.root / str(row["relative_path"]))

            deleted_observations = 0
            for row in eligible_observation_rows:
                cursor = connection.execute("DELETE FROM observations WHERE id = ?", (row["id"],))
                deleted_observations += max(0, int(cursor.rowcount))

        for path in files_to_remove:
            try:
                if path.exists():
                    path.unlink()
                else:
                    missing_files += 1
            except OSError as exc:
                file_errors.append(f"{path}: {exc}")

        return PurgeResult(
            as_of=current,
            dry_run=False,
            eligible_observations=eligible_observations,
            eligible_artifacts=eligible_artifacts,
            expired_observations=eligible_observations,
            deleted_observations=deleted_observations,
            expired_artifacts=eligible_artifacts,
            deleted_artifacts=deleted_artifacts,
            skipped_referenced_artifacts=skipped,
            missing_artifact_files=missing_files,
            file_errors=tuple(file_errors),
        )

    purge = purge_expired
    run_retention = purge_expired

    def recover(self, *, now: datetime | str | None = None, clean_orphans: bool = False) -> dict[str, Any]:
        return {
            "jobs": self.recover_jobs(now=now),
            "artifacts": self.artifacts.recover(remove_orphans=clean_orphans),
        }

    # ------------------------------------------------------------------
    # Row conversion helpers
    # ------------------------------------------------------------------
    @classmethod
    def _artifact_from_row(cls, row: sqlite3.Row | None) -> ArtifactRecord:
        if row is None:
            raise RuntimeError("expected artifact row")
        return ArtifactRecord(
            digest=str(row["digest"]),
            size_bytes=int(row["size_bytes"]),
            media_type=str(row["media_type"]),
            relative_path=str(row["relative_path"]),
            retention_tier=RetentionTier.coerce(row["retention_tier"]),
            created_at=ensure_utc(row["created_at"]),
            expires_at=parse_timestamp(row["expires_at"]),
            metadata=cls._decode(row["metadata_json"], default={}),
            last_accessed_at=parse_timestamp(row["last_accessed_at"]),
        )

    @classmethod
    def _source_from_row(cls, row: sqlite3.Row | None) -> SourceRecord:
        if row is None:
            raise RuntimeError("expected source row")
        return SourceRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            name=row["name"],
            uri=row["uri"],
            external_id=row["external_id"],
            metadata=cls._decode(row["metadata_json"], default={}),
            created_at=ensure_utc(row["created_at"]),
            updated_at=ensure_utc(row["updated_at"]),
        )

    @classmethod
    def _capture_from_row(cls, row: sqlite3.Row | None) -> CaptureRecord:
        if row is None:
            raise RuntimeError("expected capture row")
        return CaptureRecord(
            id=str(row["id"]),
            source_id=row["source_id"],
            kind=str(row["kind"]),
            external_id=row["external_id"],
            captured_at=ensure_utc(row["captured_at"]),
            metadata=cls._decode(row["metadata_json"], default={}),
            idempotency_key=row["idempotency_key"],
            created_at=ensure_utc(row["created_at"]),
            updated_at=ensure_utc(row["updated_at"]),
        )

    @classmethod
    def _observation_from_row(cls, row: sqlite3.Row | None) -> ObservationRecord:
        if row is None:
            raise RuntimeError("expected observation row")
        return ObservationRecord(
            id=str(row["id"]),
            capture_id=row["capture_id"],
            source_id=row["source_id"],
            kind=str(row["kind"]),
            observed_at=ensure_utc(row["observed_at"]),
            text_content=str(row["text_content"] or ""),
            payload=cls._decode(row["payload_json"], default=None),
            metadata=cls._decode(row["metadata_json"], default={}),
            retention_tier=RetentionTier.coerce(row["retention_tier"]),
            expires_at=parse_timestamp(row["expires_at"]),
            idempotency_key=row["idempotency_key"],
            created_at=ensure_utc(row["created_at"]),
            updated_at=ensure_utc(row["updated_at"]),
        )

    @classmethod
    def _job_from_row(cls, row: sqlite3.Row | None) -> JobRecord:
        if row is None:
            raise RuntimeError("expected job row")
        return JobRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            payload=cls._decode(row["payload_json"], default={}),
            idempotency_key=str(row["idempotency_key"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            available_at=ensure_utc(row["available_at"]),
            lease_until=parse_timestamp(row["lease_until"]),
            locked_by=row["locked_by"],
            last_error=row["last_error"],
            result=cls._decode(row["result_json"], default=None),
            created_at=ensure_utc(row["created_at"]),
            updated_at=ensure_utc(row["updated_at"]),
            completed_at=parse_timestamp(row["completed_at"]),
        )

    @staticmethod
    def _attach_artifact_in_transaction(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        owner_id: str,
        digest: str,
        role: str = "primary",
    ) -> None:
        normalized = normalize_digest(digest)
        if connection.execute("SELECT 1 FROM artifacts WHERE digest = ?", (normalized,)).fetchone() is None:
            raise KeyError(f"unknown artifact: {normalized}")
        connection.execute(
            f"INSERT OR IGNORE INTO {table}({id_column}, digest, role, created_at) VALUES (?, ?, ?, ?)",
            (owner_id, normalized, role, timestamp(utc_now())),
        )


# Friendly aliases for applications that call this component Storage/ContextStore.
Storage = SQLiteStorage
ContextStore = SQLiteStorage


__all__ = [
    "ArtifactConflict",
    "ContextStore",
    "IdempotencyConflict",
    "JobStateError",
    "SCHEMA_VERSION",
    "SQLiteStorage",
    "Storage",
    "VectorFeatureUnavailable",
]
