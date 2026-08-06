from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.context_server.storage import RetentionTier
from services.context_server.tests.integration.test_runtime_app import runtime_client


def test_v1_purge_applies_source_project_date_and_tier_selectors_with_counts():
    before = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    old = before - timedelta(days=2)

    with runtime_client() as (client, app):
        runtime = app.state.context_runtime
        matching_source = runtime.storage.create_source(
            "clipboard",
            source_id="source:api-clipboard",
            external_id="api-clipboard",
            now=old,
        )
        other_source = runtime.storage.create_source(
            "window",
            source_id="source:api-window",
            external_id="api-window",
            now=old,
        )
        matching = runtime.storage.create_observation(
            source_id=matching_source.id,
            observation_id="observation:api-matching",
            text="matching API purge record",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.DERIVED,
            observed_at=old,
            now=old,
        )
        other = runtime.storage.create_observation(
            source_id=other_source.id,
            observation_id="observation:api-other",
            text="other API purge record",
            payload={"project": "other-project"},
            retention_tier=RetentionTier.DERIVED,
            observed_at=old,
            now=old,
        )
        matching_artifact = runtime.storage.artifacts.put_bytes(
            b"matching API raw artifact",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "clipboard", "project": "life-context"},
            created_at=old,
        )
        other_artifact = runtime.storage.artifacts.put_bytes(
            b"other API raw artifact",
            retention_tier=RetentionTier.RAW,
            metadata={"source": "window", "project": "other-project"},
            created_at=old,
        )

        response = client.post(
            "/v1/purge",
            json={
                "before": before.isoformat(),
                "source": "clipboard",
                "project": "life-context",
                "retention_tiers": ["raw", "derived"],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["before"] == before.isoformat()
        assert payload["source"] == "clipboard"
        assert payload["project"] == "life-context"
        assert payload["retention_tiers"] == ["raw", "derived"]
        assert payload["eligible"] == 1
        assert payload["deleted"] == 1
        assert payload["eligible_observations"] == 1
        assert payload["deleted_observations"] == 1
        assert payload["eligible_artifacts"] == 1
        assert payload["deleted_artifacts"] == 1
        assert payload["skipped_referenced_artifacts"] == 0
        assert runtime.storage.get_observation(matching.id) is None
        assert runtime.storage.get_observation(other.id) is not None
        assert not runtime.storage.artifacts.exists(matching_artifact.digest)
        assert runtime.storage.artifacts.exists(other_artifact.digest)


def test_v1_purge_dry_run_reports_selector_counts_without_mutation():
    before = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)
    old = before - timedelta(days=2)

    with runtime_client() as (client, app):
        runtime = app.state.context_runtime
        source = runtime.storage.create_source("clipboard", source_id="source:api-dry-run", now=old)
        observation = runtime.storage.create_observation(
            source_id=source.id,
            observation_id="observation:api-dry-run",
            payload={"project": "life-context"},
            retention_tier=RetentionTier.DERIVED,
            observed_at=old,
            now=old,
        )

        response = client.post(
            "/v1/purge",
            json={
                "before": before.isoformat(),
                "source": "clipboard",
                "project": "life-context",
                "retention_tier": "derived",
                "dry_run": True,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["dry_run"] is True
        assert payload["eligible"] == 1
        assert payload["deleted"] == 0
        assert payload["eligible_observations"] == 1
        assert payload["deleted_observations"] == 0
        assert runtime.storage.get_observation(observation.id) is not None
