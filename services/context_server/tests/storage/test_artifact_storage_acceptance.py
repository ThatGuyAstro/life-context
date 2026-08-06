from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from services.context_server.storage import (
    MIGRATIONS,
    ArtifactReferenced,
    RetentionTier,
    SQLiteStorage,
    content_addressed_path,
    digest_bytes,
)


def make_storage(tmp_path):
    return SQLiteStorage(
        tmp_path / "context.sqlite3",
        artifact_root=tmp_path / "artifacts",
        load_sqlite_vec_extension=False,
    )


def test_duplicate_bytes_with_different_metadata_keep_one_immutable_record(tmp_path):
    storage = make_storage(tmp_path)
    created_at = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    try:
        first = storage.artifacts.put_bytes(
            b"same evidence bytes",
            media_type="text/plain",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context", "attempt": 1},
            created_at=created_at,
        )
        retry = storage.artifacts.put_bytes(
            b"same evidence bytes",
            media_type="application/octet-stream",
            retention_tier=RetentionTier.KEYFRAME,
            metadata={"source": "screenshots", "project": "other-project", "attempt": 2},
            created_at=created_at + timedelta(minutes=1),
        )

        assert retry == first
        assert storage.list_artifacts() == [first]
        assert storage.artifacts.read(first.digest) == b"same evidence bytes"
        assert storage.get_artifact(first.digest).metadata == {
            "source": "clipboard",
            "project": "life-context",
            "attempt": 1,
        }
        assert list(storage.artifacts.staging_root.glob("*.part")) == []
    finally:
        storage.close()


def test_staged_artifact_recovery_removes_interrupted_parts_and_orphans(tmp_path):
    storage = make_storage(tmp_path)
    try:
        committed = storage.artifacts.put_bytes(b"committed bytes")
        interrupted = storage.artifacts.staging_root / "interrupted.part"
        interrupted.write_bytes(b"partial artifact")
        orphan = storage.artifacts.root / "orphan.bin"
        orphan.write_bytes(b"unregistered artifact")

        report = storage.artifacts.recover()
        assert report.removed_staging_files == 1
        assert report.missing_records == ()
        assert report.corrupt_records == ()
        assert report.orphan_files == ("orphan.bin",)
        assert storage.artifacts.verify(committed.digest)

        cleanup = storage.artifacts.recover(remove_orphans=True)
        assert cleanup.removed_orphan_files == 1
        assert not orphan.exists()
        assert storage.artifacts.verify(committed.digest)
    finally:
        storage.close()


def test_raw_and_keyframe_purge_respect_exact_expiration_boundaries(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    try:
        raw_at_boundary = storage.artifacts.put_bytes(
            b"raw at boundary",
            retention_tier=RetentionTier.RAW,
            created_at=now - timedelta(hours=24),
        )
        raw_inside_window = storage.artifacts.put_bytes(
            b"raw inside window",
            retention_tier=RetentionTier.RAW,
            created_at=now - timedelta(hours=24) + timedelta(microseconds=1),
        )
        keyframe_at_boundary = storage.artifacts.put_bytes(
            b"keyframe at boundary",
            retention_tier=RetentionTier.KEYFRAME,
            created_at=now - timedelta(days=30),
        )
        keyframe_inside_window = storage.artifacts.put_bytes(
            b"keyframe inside window",
            retention_tier=RetentionTier.KEYFRAME,
            created_at=now - timedelta(days=30) + timedelta(microseconds=1),
        )
        derived = storage.artifacts.put_bytes(
            b"derived remains durable",
            retention_tier=RetentionTier.DERIVED,
            created_at=now - timedelta(days=365),
        )

        result = storage.purge_expired(as_of=now)

        assert result.expired_artifacts == 2
        assert result.deleted_artifacts == 2
        assert not storage.artifacts.exists(raw_at_boundary.digest)
        assert not storage.artifacts.exists(keyframe_at_boundary.digest)
        assert storage.artifacts.exists(raw_inside_window.digest)
        assert storage.artifacts.exists(keyframe_inside_window.digest)
        assert storage.artifacts.exists(derived.digest)
    finally:
        storage.close()


def test_referenced_capture_observation_and_job_evidence_survive_purge(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    expired_at = now - timedelta(days=2)
    try:
        capture_evidence = storage.artifacts.put_bytes(
            b"capture evidence", retention_tier=RetentionTier.RAW, created_at=expired_at
        )
        observation_evidence = storage.artifacts.put_bytes(
            b"observation evidence", retention_tier=RetentionTier.RAW, created_at=expired_at
        )
        job_evidence = storage.artifacts.put_bytes(
            b"job evidence", retention_tier=RetentionTier.RAW, created_at=expired_at
        )
        unreferenced = storage.artifacts.put_bytes(
            b"unreferenced evidence", retention_tier=RetentionTier.RAW, created_at=expired_at
        )

        capture = storage.create_capture(capture_id="capture:evidence", now=now)
        storage.attach_artifact("capture", capture.id, capture_evidence.digest, role="raw")
        observation = storage.create_observation(
            observation_id="observation:evidence",
            text="evidence observation",
            artifact_digest=observation_evidence.digest,
            now=now,
        )
        job = storage.enqueue_job(
            "derive-evidence",
            {"observation_id": observation.id},
            job_id="job:evidence",
            idempotency_key="job:evidence",
            now=now,
        )
        storage.link_job_artifact(job.id, job_evidence.digest, role="output")

        result = storage.purge_expired(as_of=now)

        assert result.expired_artifacts == 4
        assert result.deleted_artifacts == 1
        assert result.skipped_referenced_artifacts == 3
        for record in (capture_evidence, observation_evidence, job_evidence):
            assert storage.artifacts.exists(record.digest)
            with pytest.raises(ArtifactReferenced, match=record.digest):
                storage.artifacts.delete(record.digest)
        assert not storage.artifacts.exists(unreferenced.digest)
        assert storage.artifact_reference_count(capture_evidence.digest) == 1
        assert storage.artifact_reference_count(observation_evidence.digest) == 1
        assert storage.artifact_reference_count(job_evidence.digest) == 1
    finally:
        storage.close()


def test_explicit_date_purge_preserves_newer_and_other_source_project_artifacts(tmp_path):
    storage = make_storage(tmp_path)
    before = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    try:
        old_matching_scope = storage.artifacts.put_bytes(
            b"old clipboard project artifact",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=before - timedelta(days=2),
        )
        newer_matching_scope = storage.artifacts.put_bytes(
            b"new clipboard project artifact",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=before,
        )
        older_other_scope = storage.artifacts.put_bytes(
            b"old screenshot other project artifact",
            retention_tier=RetentionTier.DERIVED,
            metadata={"source": "screenshots", "project": "other-project"},
            created_at=before - timedelta(days=365),
        )

        result = storage.purge_expired(as_of=before)

        assert result.deleted_artifacts == 1
        assert not storage.artifacts.exists(old_matching_scope.digest)
        assert storage.artifacts.exists(newer_matching_scope.digest)
        assert storage.artifacts.exists(older_other_scope.digest)
        assert storage.get_artifact(newer_matching_scope.digest).metadata == {
            "source": "clipboard",
            "project": "life-context",
        }
        assert storage.get_artifact(older_other_scope.digest).metadata == {
            "source": "screenshots",
            "project": "other-project",
        }
    finally:
        storage.close()


def test_restart_and_forward_migration_recover_legacy_artifact_content(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    artifact_root = tmp_path / "artifacts"
    payload = b"artifact committed before restart and migration"
    digest = digest_bytes(payload)
    relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}"
    content_path = content_addressed_path(artifact_root, digest)
    content_path.parent.mkdir(parents=True, exist_ok=True)
    content_path.write_bytes(payload)

    applied_at = "2026-08-05T16:00:00+00:00"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    MIGRATIONS[0].apply(connection)
    connection.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, 'core_tables', ?)",
        (applied_at,),
    )
    connection.execute(
        """
        INSERT INTO artifacts(
            digest, algorithm, size_bytes, media_type, relative_path,
            retention_tier, created_at, expires_at, metadata_json
        ) VALUES (?, 'sha256', ?, 'application/octet-stream', ?, 'keyframe', ?, NULL, ?)
        """,
        (
            digest,
            len(payload),
            relative_path,
            applied_at,
            '{"source":"legacy","project":"life-context"}',
        ),
    )
    connection.commit()
    connection.close()

    first = SQLiteStorage(database, artifact_root=artifact_root, load_sqlite_vec_extension=False)
    try:
        assert first.schema_version == 3
        assert first.artifacts.verify(digest)
        assert first.artifacts.read(digest) == payload
    finally:
        first.close()

    second = SQLiteStorage(database, artifact_root=artifact_root, load_sqlite_vec_extension=False)
    try:
        recovery = second.recover()
        assert recovery["artifacts"].missing_records == ()
        assert recovery["artifacts"].corrupt_records == ()
        assert recovery["artifacts"].orphan_files == ()
        record = second.get_artifact(digest)
        assert record is not None
        assert record.metadata == {"source": "legacy", "project": "life-context"}
        assert second.artifacts.read(digest) == payload
    finally:
        second.close()
