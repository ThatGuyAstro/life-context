from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from services.context_server.storage import (
    ArtifactConflict,
    ArtifactCorruption,
    ArtifactReferenced,
    MIGRATIONS,
    RetentionTier,
    SQLiteStorage,
)


UTC = timezone.utc


def make_storage(tmp_path):
    return SQLiteStorage(
        tmp_path / "context.sqlite3",
        artifact_root=tmp_path / "artifacts",
        load_sqlite_vec_extension=False,
    )


def _run_crash_writer(database, artifacts, body):
    """Run a tiny writer that terminates without closing its SQLite handle."""

    root = str(__file__)
    script = textwrap.dedent(
        f"""
        import os
        from datetime import datetime, timezone
        from pathlib import Path
        import sys

        sys.path.insert(0, {str(Path(root).parents[4])!r})
        from services.context_server.storage import RetentionTier, SQLiteStorage

        storage = SQLiteStorage(
            {str(database)!r},
            artifact_root={str(artifacts)!r},
            load_sqlite_vec_extension=False,
        )
        storage.connection.execute("PRAGMA wal_autocheckpoint = 1000000")
        {textwrap.indent(textwrap.dedent(body), "        ")}
        os._exit(0)
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(root).parents[4]),
        check=False,
        capture_output=True,
        text=True,
    )


def test_abrupt_restart_recovers_committed_events_artifacts_and_queue_rows(tmp_path):
    database = tmp_path / "crash.sqlite3"
    artifacts = tmp_path / "artifacts"
    body = b"committed bytes survive an abrupt process exit"
    digest = sha256(body).hexdigest()

    result = _run_crash_writer(
        database,
        artifacts,
        f"""
        source = storage.create_source("clipboard", source_id="source:crash")
        capture = storage.create_capture(
            source.id,
            capture_id="capture:crash",
            idempotency_key="capture:crash",
            now=datetime.now(timezone.utc),
        )
        artifact = storage.artifacts.put_bytes(
            {body!r}, retention_tier=RetentionTier.RAW
        )
        storage.attach_artifact("capture", capture.id, artifact.digest, role="raw")
        storage.create_observation(
            capture.id,
            observation_id="observation:crash",
            kind="source_event:clipboard",
            text="committed event survives abrupt exit",
            payload={{"marker": "crash-committed"}},
            idempotency_key="event:crash",
        )
        storage.enqueue_job(
            "inference",
            {{"capture_id": capture.id, "artifact_digest": artifact.digest}},
            job_id="job:crash",
            idempotency_key="job:crash",
        )
        """,
    )
    assert result.returncode == 0, result.stderr
    wal_path = database.with_name(database.name + "-wal")
    assert wal_path.exists(), "the abrupt writer should leave a WAL for recovery"

    recovered = SQLiteStorage(
        database, artifact_root=artifacts, load_sqlite_vec_extension=False
    )
    try:
        assert recovered.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert recovered.get_capture("capture:crash") is not None
        event = recovered.get_observation("observation:crash")
        assert event is not None
        assert event.text_content == "committed event survives abrupt exit"
        assert recovered.get_job("job:crash").payload["artifact_digest"] == digest
        assert recovered.artifacts.read(digest) == body
        assert recovered.artifact_reference_count(digest) == 1
    finally:
        recovered.close()


def test_abrupt_restart_rolls_back_open_transaction(tmp_path):
    database = tmp_path / "rollback.sqlite3"
    artifacts = tmp_path / "artifacts"
    result = _run_crash_writer(
        database,
        artifacts,
        """
        with storage.transaction():
            storage.create_source("window", source_id="source:uncommitted")
            storage.create_capture("source:uncommitted", capture_id="capture:uncommitted")
            os._exit(0)
        """,
    )
    assert result.returncode == 0, result.stderr

    recovered = SQLiteStorage(
        database, artifact_root=artifacts, load_sqlite_vec_extension=False
    )
    try:
        assert recovered.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert recovered.get_source("source:uncommitted") is None
        assert recovered.get_capture("capture:uncommitted") is None
    finally:
        recovered.close()


def test_abrupt_restart_requeues_expired_running_job(tmp_path):
    database = tmp_path / "queue-crash.sqlite3"
    artifacts = tmp_path / "artifacts"
    claimed_at = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    result = _run_crash_writer(
        database,
        artifacts,
        f"""
        job = storage.enqueue_job(
            "inference",
            {{"marker": "queue-crash"}},
            job_id="job:queue-crash",
            idempotency_key="job:queue-crash",
            now=datetime.fromisoformat({claimed_at.isoformat()!r}),
        )
        claimed = storage.claim_next_job(
            "crashed-worker",
            lease_seconds=30,
            now=datetime.fromisoformat({claimed_at.isoformat()!r}),
        )
        assert claimed is not None and claimed.id == job.id
        """,
    )
    assert result.returncode == 0, result.stderr

    recovered = SQLiteStorage(
        database, artifact_root=artifacts, load_sqlite_vec_extension=False
    )
    try:
        before = recovered.get_job("job:queue-crash")
        assert before is not None and before.status == "running"
        report = recovered.recover_jobs(now=claimed_at + timedelta(seconds=31))
        assert report.recovered_jobs == 1
        pending = recovered.get_job("job:queue-crash")
        assert pending is not None
        assert pending.status == "pending"
        assert pending.locked_by is None
        assert recovered.claim_next_job(
            "restarted-worker", now=claimed_at + timedelta(seconds=31)
        ) is not None
    finally:
        recovered.close()


def test_restart_recovers_committed_capture_and_event_observations(tmp_path):
    database = tmp_path / "context.sqlite3"
    artifacts = tmp_path / "artifacts"
    committed_at = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)

    first = SQLiteStorage(database, artifact_root=artifacts, load_sqlite_vec_extension=False)
    try:
        source = first.create_source(
            "clipboard",
            source_id="source:restart",
            name="Restart source",
            external_id="clipboard-restart",
            now=committed_at,
        )
        capture = first.create_capture(
            source.id,
            capture_id="capture:restart",
            kind="capture_envelope",
            external_id="envelope-restart",
            metadata={"restart_marker": "committed-capture"},
            idempotency_key="capture:restart",
            captured_at=committed_at,
            now=committed_at,
        )
        events = [
            first.create_observation(
                capture.id,
                observation_id="observation:event:restart:1",
                kind="source_event:clipboard",
                text="committed clipboard event survives restart",
                payload={"value": "restart payload"},
                metadata={"event_id": "event:restart:1", "event_type": "clipboard.changed"},
                idempotency_key="event:restart:1",
                retention_tier=RetentionTier.RAW,
                observed_at=committed_at,
                now=committed_at,
            ),
            first.create_observation(
                capture.id,
                observation_id="observation:event:restart:2",
                kind="source_event:window",
                text="second committed event survives restart",
                payload={"window": "editor"},
                metadata={"event_id": "event:restart:2", "event_type": "window.changed"},
                idempotency_key="event:restart:2",
                retention_tier=RetentionTier.RAW,
                observed_at=committed_at + timedelta(seconds=1),
                now=committed_at,
            ),
        ]
    finally:
        first.close()

    second = SQLiteStorage(database, artifact_root=artifacts, load_sqlite_vec_extension=False)
    try:
        assert second.get_source(source.id) == source
        assert second.get_capture(capture.id) == capture
        assert [second.get_observation(event.id) for event in events] == events
        assert second.connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert second.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 2

        # A replay after restart returns the committed logical records instead of
        # creating a second capture or event observation.
        replayed_capture = second.create_capture(
            source.id,
            capture_id="capture:replayed-different-id",
            idempotency_key="capture:restart",
            now=committed_at + timedelta(minutes=1),
        )
        replayed_event = second.create_observation(
            capture.id,
            observation_id="observation:event:replayed-different-id",
            text="retry payload is ignored",
            idempotency_key="event:restart:1",
            now=committed_at + timedelta(minutes=1),
        )
        assert replayed_capture.id == capture.id
        assert replayed_event.id == events[0].id
        assert [item.id for item in second.search_observations("clipboard event")] == [events[0].id]
    finally:
        second.close()


def test_fts_migration_and_rebuild_backfill_rows_from_legacy_schema(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    applied_at = "2026-08-05T16:00:00+00:00"

    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    MIGRATIONS[0].apply(connection)
    connection.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, 'core_tables', ?)",
        (applied_at,),
    )
    connection.execute(
        """
        INSERT INTO sources(id, kind, name, external_id, metadata_json, created_at, updated_at)
        VALUES ('source:legacy', 'desktop', 'Legacy source', 'legacy-source', '{}', ?, ?)
        """,
        (applied_at, applied_at),
    )
    connection.execute(
        """
        INSERT INTO captures(
            id, source_id, kind, external_id, captured_at, metadata_json,
            idempotency_key, created_at, updated_at
        ) VALUES ('capture:legacy', 'source:legacy', 'legacy_capture', 'legacy-capture', ?, ?, ?, ?, ?)
        """,
        (applied_at, '{"migration_marker":"legacy-capture"}', "capture:legacy", applied_at, applied_at),
    )
    connection.execute(
        """
        INSERT INTO observations(
            id, capture_id, source_id, kind, observed_at, text_content,
            payload_json, metadata_json, retention_tier, idempotency_key,
            created_at, updated_at
        ) VALUES ('observation:legacy', 'capture:legacy', 'source:legacy', 'legacy_event', ?, ?, ?, ?, 'derived', ?, ?, ?)
        """,
        (
            applied_at,
            "legacy event must be reindexed after migration",
            '{"payload_marker":"legacy-payload"}',
            '{"migration_marker":"legacy-observation"}',
            "observation:legacy",
            applied_at,
            applied_at,
        ),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(database, artifact_root=tmp_path / "artifacts", load_sqlite_vec_extension=False)
    try:
        if not storage.fts5_available:
            pytest.skip("SQLite build has no FTS5 support")

        assert storage.schema_version == 3
        assert storage.search_captures("legacy-capture") == []
        assert storage.search_observations("legacy event") == []

        storage.rebuild_fts_indexes()

        assert [item.id for item in storage.search_captures("legacy-capture")] == ["capture:legacy"]
        assert [item.id for item in storage.search_observations("legacy event")] == [
            "observation:legacy"
        ]
        assert [item.id for item in storage.search_sources("Legacy source")] == ["source:legacy"]
    finally:
        storage.close()


def test_fts_rebuild_removes_stale_rows_and_restores_deleted_rows(tmp_path):
    storage = make_storage(tmp_path)
    try:
        observation = storage.create_observation(
            observation_id="observation:fts-rebuild",
            text="rebuild marker is present in the committed observation",
            idempotency_key="observation:fts-rebuild",
        )
        if not storage.fts5_available:
            pytest.skip("SQLite build has no FTS5 support")

        storage.connection.execute(
            "DELETE FROM observations_fts WHERE observation_id = ?", (observation.id,)
        )
        storage.connection.execute(
            "INSERT INTO observations_fts(observation_id, text_content, metadata) VALUES (?, ?, '')",
            ("observation:stale", "stale ghost marker"),
        )
        assert storage.search_observations("rebuild marker") == []

        storage.rebuild_fts_indexes()

        assert [item.id for item in storage.search_observations("rebuild marker")] == [observation.id]
        assert (
            storage.connection.execute(
                "SELECT count(*) FROM observations_fts WHERE observations_fts MATCH 'ghost'"
            ).fetchone()[0]
            == 0
        )
    finally:
        storage.close()


def test_content_addressed_artifacts_deduplicate_and_report_conflicts(tmp_path):
    storage = make_storage(tmp_path)
    try:
        first = storage.artifacts.put_bytes(
            b"immutable artifact bytes",
            media_type="text/plain",
            metadata={"producer": "first-write"},
        )
        duplicate = storage.artifacts.put_bytes(
            b"immutable artifact bytes",
            media_type="application/octet-stream",
            metadata={"producer": "retry"},
        )

        assert duplicate == first
        assert storage.connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert storage.artifacts.path_for(first.digest).is_file()
        assert list(storage.artifacts.staging_root.glob("*.part")) == []

        with pytest.raises(ArtifactConflict, match=first.digest):
            storage.register_artifact(
                digest=first.digest,
                size_bytes=first.size_bytes,
                media_type=first.media_type,
                relative_path=f"conflict/{first.digest}",
            )

        with pytest.raises(ArtifactCorruption, match="does not match content"):
            storage.artifacts.put_bytes(b"different bytes", expected_digest=first.digest)
        assert storage.artifacts.read(first.digest) == b"immutable artifact bytes"
    finally:
        storage.close()


def test_retention_purge_keeps_referenced_expired_artifacts_safe(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    try:
        referenced = storage.artifacts.put_bytes(
            b"referenced raw artifact",
            retention_tier=RetentionTier.RAW,
            created_at=now - timedelta(days=2),
        )
        unreferenced = storage.artifacts.put_bytes(
            b"unreferenced raw artifact",
            retention_tier=RetentionTier.RAW,
            created_at=now - timedelta(days=2),
        )
        fresh = storage.artifacts.put_bytes(
            b"fresh raw artifact",
            retention_tier=RetentionTier.RAW,
            created_at=now,
        )
        capture = storage.create_capture(capture_id="capture:retention-reference", now=now)
        storage.attach_artifact("capture", capture.id, referenced.digest, role="raw")

        dry_run = storage.purge_expired(as_of=now, dry_run=True)
        assert dry_run.dry_run is True
        assert dry_run.expired_artifacts == 2
        assert dry_run.deleted_artifacts == 0
        assert dry_run.skipped_referenced_artifacts == 1
        assert storage.artifacts.exists(referenced.digest)
        assert storage.artifacts.exists(unreferenced.digest)

        result = storage.purge_expired(as_of=now)
        assert result.expired_artifacts == 2
        assert result.deleted_artifacts == 1
        assert result.skipped_referenced_artifacts == 1
        assert storage.artifacts.exists(referenced.digest)
        assert storage.artifacts.read(referenced.digest) == b"referenced raw artifact"
        assert not storage.artifacts.exists(unreferenced.digest)
        assert storage.artifacts.exists(fresh.digest)
        assert storage.artifact_reference_count(referenced.digest) == 1

        with pytest.raises(ArtifactReferenced, match=referenced.digest):
            storage.artifacts.delete(referenced.digest)
    finally:
        storage.close()
