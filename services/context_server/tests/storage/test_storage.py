from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import services.context_server.storage.vector as vector_module
from services.context_server.storage import (
    RetentionTier,
    SCHEMA_VERSION,
    SQLiteStorage,
    VectorFeatureUnavailable,
    apply_migrations,
    content_addressed_path,
    create_vector_index,
    status_from_connection,
)


def make_storage(tmp_path):
    return SQLiteStorage(
        tmp_path / "context.sqlite3",
        artifact_root=tmp_path / "artifacts",
        load_sqlite_vec_extension=False,
    )


def test_migrations_are_forward_only_and_reentrant(tmp_path):
    database = tmp_path / "schema.sqlite3"
    first = SQLiteStorage(database, artifact_root=tmp_path / "artifacts", load_sqlite_vec_extension=False)
    assert first.schema_version == SCHEMA_VERSION
    first.close()

    connection = sqlite3.connect(database)
    assert apply_migrations(connection) == SCHEMA_VERSION
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    connection.close()


def test_content_addressed_artifacts_are_idempotent_and_recover_staging(tmp_path):
    storage = make_storage(tmp_path)
    try:
        first = storage.artifacts.put_bytes(b"same content", media_type="text/plain")
        second = storage.artifacts.put_bytes(b"same content", media_type="text/plain")

        assert first.digest == second.digest
        assert first.relative_path == f"{first.digest[:2]}/{first.digest[2:4]}/{first.digest}"
        assert content_addressed_path(tmp_path / "artifacts", first.digest).is_file()
        assert storage.connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert storage.artifacts.read(first.digest) == b"same content"

        interrupted = storage.artifacts.staging_root / "interrupted.part"
        interrupted.write_bytes(b"partial write")
        recovery = storage.artifacts.recover()
        assert recovery.removed_staging_files == 1
        assert not interrupted.exists()
        assert storage.artifacts.verify(first.digest)
    finally:
        storage.close()


def test_source_capture_observation_and_job_retries_are_idempotent(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
    try:
        source = storage.create_source("browser", external_id="window-1", name="Browser")
        same_source = storage.create_source("browser", external_id="window-1", name="ignored retry")
        assert same_source.id == source.id

        capture = storage.create_capture(
            source.id,
            captured_at=now,
            idempotency_key="capture:window-1:1",
        )
        same_capture = storage.create_capture(
            source.id,
            captured_at=now + timedelta(seconds=1),
            idempotency_key="capture:window-1:1",
        )
        assert same_capture.id == capture.id

        observation = storage.create_observation(
            capture.id,
            text="a durable note",
            payload={"answer": 42},
            idempotency_key="observation:window-1:1",
            now=now,
        )
        same_observation = storage.create_observation(
            capture.id,
            text="a different retry payload is ignored",
            idempotency_key="observation:window-1:1",
            now=now,
        )
        assert same_observation.id == observation.id
        assert storage.connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert storage.connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert storage.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1

        job = storage.enqueue_job(
            "derive-note",
            {"observation_id": observation.id},
            idempotency_key="derive:observation-1",
            now=now,
        )
        same_job = storage.enqueue_job(
            "derive-note",
            {"observation_id": observation.id},
            idempotency_key="derive:observation-1",
            now=now,
        )
        assert same_job.id == job.id

        claimed = storage.claim_next_job("worker-a", lease_seconds=5, now=now)
        assert claimed is not None
        assert claimed.attempts == 1
        recovered = storage.recover_jobs(now=now + timedelta(seconds=6))
        assert recovered.recovered_jobs == 1
        reclaimed = storage.claim_next_job("worker-b", lease_seconds=5, now=now + timedelta(seconds=6))
        assert reclaimed is not None
        assert reclaimed.id == job.id
        assert reclaimed.attempts == 2
        completed = storage.complete_job(reclaimed.id, {"ok": True}, now=now + timedelta(seconds=7))
        assert completed.status == "succeeded"
        assert storage.complete_job(reclaimed.id, {"ignored": True}).status == "succeeded"
    finally:
        storage.close()


def test_fts5_indexes_search_observations_and_sources(tmp_path):
    storage = make_storage(tmp_path)
    try:
        source = storage.create_source("desktop", name="Workstation", metadata={"room": "studio"})
        capture = storage.create_capture(source.id, metadata={"application": "editor"})
        observation = storage.create_observation(
            capture.id,
            text="the blue notebook is beside the keyboard",
            metadata={"topic": "desk"},
        )
        if storage.fts5_available:
            assert [item.id for item in storage.search_observations("blue notebook")] == [observation.id]
            assert [item.id for item in storage.search_sources("Workstation")] == [source.id]
            storage.rebuild_fts_indexes()
            assert storage.search_observations("keyboard")[0].id == observation.id
        else:
            pytest.skip("SQLite build has no FTS5 support")
    finally:
        storage.close()


def test_fts_search_ties_have_a_stable_identifier_tiebreak(tmp_path):
    storage = make_storage(tmp_path)
    try:
        for identifier in ("observation:zeta", "observation:alpha", "observation:beta"):
            storage.create_observation(
                observation_id=identifier,
                idempotency_key=identifier,
                text="same deterministic marker",
            )
        if not storage.fts5_available:
            pytest.skip("SQLite build has no FTS5 support")

        assert [item.id for item in storage.search_observations("same deterministic marker")] == [
            "observation:alpha",
            "observation:beta",
            "observation:zeta",
        ]
    finally:
        storage.close()


def test_retention_purges_expired_tiers_but_keeps_derived(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
    try:
        raw = storage.artifacts.put_bytes(
            b"raw",
            retention_tier=RetentionTier.RAW,
            created_at=now - timedelta(days=2),
        )
        keyframe = storage.artifacts.put_bytes(
            b"keyframe",
            retention_tier=RetentionTier.KEYFRAME,
            created_at=now - timedelta(days=31),
        )
        derived = storage.artifacts.put_bytes(
            b"derived",
            retention_tier=RetentionTier.DERIVED,
            created_at=now - timedelta(days=365),
        )
        result = storage.purge_expired(as_of=now)
        assert result.deleted_artifacts == 2
        assert not storage.artifacts.exists(raw.digest)
        assert not storage.artifacts.exists(keyframe.digest)
        assert storage.artifacts.exists(derived.digest)
    finally:
        storage.close()


def test_observation_retention_uses_observed_time(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)
    try:
        observation = storage.create_observation(
            text="temporary frame",
            retention_tier=RetentionTier.RAW,
            observed_at=now - timedelta(days=2),
            now=now,
        )
        assert observation.expires_at == now - timedelta(days=1)
        result = storage.purge_expired(as_of=now)
        assert result.deleted_observations == 1
        assert storage.get_observation(observation.id) is None
    finally:
        storage.close()


def test_sqlite_vec_unavailable_state_is_explicit(tmp_path):
    storage = make_storage(tmp_path)
    try:
        status = storage.sqlite_vec_status
        assert status.available is False
        assert status.state == "disabled"
        persisted = storage.feature_status("sqlite_vec")
        assert persisted["state"] == "disabled"
        with pytest.raises(VectorFeatureUnavailable):
            storage.create_vector_index("observation_vectors", 384)
    finally:
        storage.close()


def test_sqlite_vec_missing_package_is_explicit_by_default(tmp_path, monkeypatch):
    def missing_package(name):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(vector_module.importlib, "import_module", missing_package)
    storage = SQLiteStorage(
        tmp_path / "context.sqlite3",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        status = storage.sqlite_vec_status
        assert status.available is False
        assert status.state == "unavailable"
        assert "sqlite_vec package is not installed" in status.reason
        assert storage.feature_status("sqlite_vec")["reason"] == status.reason
    finally:
        storage.close()


def test_sqlite_vec_status_without_storage_features_table_is_explicit():
    connection = sqlite3.connect(":memory:")
    try:
        status = status_from_connection(connection)
        assert status.available is False
        assert status.state == "unavailable"
        assert "status could not be read" in status.reason
        with pytest.raises(VectorFeatureUnavailable, match="status could not be read"):
            create_vector_index(connection, "observation_vectors", 384)
    finally:
        connection.close()
