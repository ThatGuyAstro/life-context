from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.context_server.storage import RetentionTier, SQLiteStorage


def make_storage(tmp_path):
    return SQLiteStorage(
        tmp_path / "context.sqlite3",
        artifact_root=tmp_path / "artifacts",
        load_sqlite_vec_extension=False,
    )


def test_scoped_date_purge_filters_records_and_artifacts_without_erasing_evidence(tmp_path):
    storage = make_storage(tmp_path)
    before = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    old = before - timedelta(days=2)
    try:
        clipboard = storage.create_source(
            "clipboard",
            source_id="source:clipboard",
            external_id="clipboard",
            now=old,
        )
        window = storage.create_source(
            "window",
            source_id="source:window",
            external_id="window",
            now=old,
        )

        matching_raw = storage.create_observation(
            source_id=clipboard.id,
            observation_id="observation:matching-raw",
            text="clipboard raw record",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.RAW,
            observed_at=old,
            now=old,
        )
        matching_derived = storage.create_observation(
            source_id=clipboard.id,
            observation_id="observation:matching-derived",
            text="clipboard derived record",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.DERIVED,
            observed_at=old,
            now=old,
        )
        other_scope = storage.create_observation(
            source_id=window.id,
            observation_id="observation:other-scope",
            text="window record must remain",
            payload={"project": "other-project"},
            retention_tier=RetentionTier.RAW,
            observed_at=old,
            now=old,
        )
        newer = storage.create_observation(
            source_id=clipboard.id,
            observation_id="observation:newer",
            text="new clipboard record must remain",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.RAW,
            observed_at=before,
            now=before,
        )

        unreferenced_artifact = storage.artifacts.put_bytes(
            b"scoped raw bytes",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=old,
        )
        referenced_artifact = storage.artifacts.put_bytes(
            b"referenced scoped bytes",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=old,
        )
        storage.create_observation(
            source_id=window.id,
            observation_id="observation:evidence-owner",
            text="unrelated record owns evidence",
            payload={"project": "other-project"},
            retention_tier=RetentionTier.DERIVED,
            artifact_digest=referenced_artifact.digest,
            observed_at=before,
            now=before,
        )
        other_artifact = storage.artifacts.put_bytes(
            b"other scope bytes",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "window", "project": "other-project"},
            created_at=old,
        )
        keyframe_artifact = storage.artifacts.put_bytes(
            b"keyframe bytes",
            retention_tier=RetentionTier.KEYFRAME,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=old,
        )

        dry_run = storage.purge_expired(
            as_of=before,
            before=before,
            source="clipboard",
            project="life-context",
            retention_tiers=["raw", "derived"],
            dry_run=True,
        )

        assert dry_run.eligible_observations == 2
        assert dry_run.expired_observations == 2
        assert dry_run.deleted_observations == 0
        assert dry_run.eligible_artifacts == 2
        assert dry_run.expired_artifacts == 2
        assert dry_run.deleted_artifacts == 0
        assert dry_run.skipped_referenced_artifacts == 1

        result = storage.purge_expired(
            as_of=before,
            before=before,
            source="clipboard",
            project="life-context",
            retention_tiers=["raw", "derived"],
        )

        assert result.eligible_observations == 2
        assert result.deleted_observations == 2
        assert result.eligible_artifacts == 2
        assert result.deleted_artifacts == 1
        assert result.skipped_referenced_artifacts == 1
        assert storage.get_observation(matching_raw.id) is None
        assert storage.get_observation(matching_derived.id) is None
        assert storage.get_observation(other_scope.id) is not None
        assert storage.get_observation(newer.id) is not None
        assert not storage.artifacts.exists(unreferenced_artifact.digest)
        assert storage.artifacts.exists(referenced_artifact.digest)
        assert storage.artifacts.exists(other_artifact.digest)
        assert storage.artifacts.exists(keyframe_artifact.digest)
    finally:
        storage.close()


def test_retention_purge_selectors_keep_derived_and_unmatched_rows(tmp_path):
    storage = make_storage(tmp_path)
    now = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    old = now - timedelta(days=2)
    try:
        matching_source = storage.create_source("clipboard", source_id="source:retention-clipboard")
        other_source = storage.create_source("window", source_id="source:retention-window")
        matching_raw = storage.create_observation(
            source_id=matching_source.id,
            observation_id="observation:retention-matching",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.RAW,
            observed_at=old,
            now=old,
        )
        matching_derived = storage.create_observation(
            source_id=matching_source.id,
            observation_id="observation:retention-derived",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.DERIVED,
            observed_at=old,
            now=old,
        )
        other_raw = storage.create_observation(
            source_id=other_source.id,
            observation_id="observation:retention-other",
            payload={"project": "other-project"},
            retention_tier=RetentionTier.RAW,
            observed_at=old,
            now=old,
        )
        matching_artifact = storage.artifacts.put_bytes(
            b"matching expired raw",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=old,
        )
        other_artifact = storage.artifacts.put_bytes(
            b"other expired raw",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "window", "project": "other-project"},
            created_at=old,
        )

        result = storage.purge_expired(
            as_of=now,
            source="clipboard",
            project="life-context",
            retention_tier=RetentionTier.RAW,
        )

        assert result.eligible_observations == 1
        assert result.deleted_observations == 1
        assert result.eligible_artifacts == 1
        assert result.deleted_artifacts == 1
        assert storage.get_observation(matching_raw.id) is None
        assert storage.get_observation(matching_derived.id) is not None
        assert storage.get_observation(other_raw.id) is not None
        assert not storage.artifacts.exists(matching_artifact.digest)
        assert storage.artifacts.exists(other_artifact.digest)
    finally:
        storage.close()
