from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from services.context_server.config import Settings
from services.context_server.query import QueryOrchestrator
from services.context_server.query.models import QueryRequest, SearchHit
from services.context_server.runtime import ContextRuntime

ARTIFACT_BODY = b"query integration screenshot evidence"
ALPHA_ID = "observation:event-fts-alpha"
BETA_ID = "observation:event-semantic-beta"
EXPECTED_HYBRID_WARNING = ("semantic retrieval unavailable: no semantic backend is configured",)


def _source_event(
    event_id: str,
    *,
    source_id: str,
    event_type: str,
    occurred_at: str,
    text: str,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_id": source_id,
        "source_kind": "application",
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload": {"text": text, "workspace": "life-context"},
        "artifacts": artifacts or [],
        "metadata": {"fixture": "query-ingestion-integration"},
    }


def _capture_envelope(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "envelope_id": "capture:query-ingestion-integration",
        "schema_version": "1",
        "captured_at": "2026-01-01T12:01:00Z",
        "source_kind": "application",
        "producer": "query-integration-test",
        "events": events,
        "metadata": {"fixture": "query-ingestion-integration"},
    }


@pytest.fixture
def ingested_runtime(tmp_path: Path) -> Iterator[dict[str, Any]]:
    runtime = ContextRuntime(
        Settings(
            data_dir=tmp_path / "context-data",
            docs_enabled=False,
            model_endpoint="",
        )
    )
    digest = sha256(ARTIFACT_BODY).hexdigest()
    artifact = runtime.put_artifact(digest, ARTIFACT_BODY, media_type="image/png")
    artifact_ref = {
        "artifact_id": digest,
        "kind": "image",
        "uri": artifact["uri"],
        "media_type": "image/png",
        "size_bytes": len(ARTIFACT_BODY),
        "sha256": digest,
    }
    events = [
        _source_event(
            "event-old-editor",
            source_id="editor",
            event_type="updated",
            occurred_at="2026-01-01T09:00:00Z",
            text="query anchor old editor evidence",
        ),
        _source_event(
            "event-fts-alpha",
            source_id="editor",
            event_type="updated",
            occurred_at="2026-01-01T10:00:00Z",
            text="query anchor lexical evidence",
            artifacts=[artifact_ref],
        ),
        _source_event(
            "event-semantic-beta",
            source_id="editor",
            event_type="updated",
            occurred_at="2026-01-01T10:30:00Z",
            text="semantic neighbor design review evidence",
        ),
        _source_event(
            "event-terminal-gamma",
            source_id="terminal",
            event_type="observed",
            occurred_at="2026-01-01T11:00:00Z",
            text="query anchor terminal evidence",
        ),
        _source_event(
            "event-late-editor",
            source_id="editor",
            event_type="observed",
            occurred_at="2026-01-01T12:00:00Z",
            text="query anchor late editor evidence",
        ),
    ]
    ingest = runtime.ingest_batch(_capture_envelope(events), idempotency_key="query-ingestion")
    assert ingest["accepted"] == len(events)
    assert ingest["captures"] == ["capture:query-ingestion-integration"]

    try:
        yield {"runtime": runtime, "digest": digest, "artifact": artifact}
    finally:
        runtime.shutdown()


class _FilteringSemanticBackend:
    """A deterministic semantic-index double over one ingested source event."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.requests: list[QueryRequest] = []

    def search(self, request: Any) -> list[SearchHit]:
        canonical = QueryRequest.from_value(request)
        self.requests.append(canonical)
        filters = dict(canonical.filters)
        source_ids = filters.get("source_ids") or filters.get("source_id")
        kinds = filters.get("kinds") or filters.get("kind")
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        if isinstance(kinds, str):
            kinds = [kinds]

        start = _as_datetime(canonical.start)
        end = _as_datetime(canonical.end)
        matches: list[SearchHit] = []
        for hit in self.hits:
            metadata = dict(hit.metadata)
            if source_ids and metadata.get("source_id") not in {str(value) for value in source_ids}:
                continue
            if kinds and metadata.get("kind") not in {str(value) for value in kinds}:
                continue
            observed_at = _as_datetime(hit.timestamp)
            if start is not None and (observed_at is None or observed_at < start):
                continue
            if end is not None and (observed_at is None or observed_at > end):
                continue
            matches.append(hit)
        return matches


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _ids(response: Any) -> list[str]:
    return [item.id for item in response.results]


def _filtered_request(mode: str) -> dict[str, Any]:
    return {
        "query": "query anchor",
        "mode": mode,
        "limit": 10,
        "filters": {
            "source_ids": ["editor"],
            "kinds": ["source_event:updated"],
        },
        "start": "2026-01-01T09:30:00Z",
        "end": "2026-01-01T10:45:00Z",
        "include_evidence": True,
    }


def test_ingested_source_events_support_fts_filters_and_artifact_evidence(
    ingested_runtime: dict[str, Any],
) -> None:
    runtime = ingested_runtime["runtime"]
    digest = ingested_runtime["digest"]

    response = runtime.search(_filtered_request("fts"))
    expected_url = f"/v1/artifacts/{digest}"

    assert _ids(response) == [ALPHA_ID]
    assert [item.rank for item in response.results] == [1]
    assert response.warnings == ()
    assert response.meta["sources"] == ["fts"]
    assert response.meta["result_count"] == 1
    assert response.results[0].metadata["source_id"] == "editor"
    assert response.results[0].metadata["kind"] == "source_event:updated"
    assert response.results[0].metadata["artifact_url"] == expected_url
    assert response.evidence[0].metadata["artifact_url"] == expected_url
    assert response.to_dict()["evidence"][0]["metadata"]["artifact_url"] == expected_url
    assert runtime.storage.artifacts.read(digest) == ARTIFACT_BODY


def test_hybrid_fusion_uses_ingested_filters_and_stable_rank_tiebreaks(
    ingested_runtime: dict[str, Any],
) -> None:
    runtime = ingested_runtime["runtime"]
    semantic = _FilteringSemanticBackend(
        [
            SearchHit(
                id=BETA_ID,
                text="semantic neighbor design review evidence",
                score=1.0,
                source="editor",
                timestamp="2026-01-01T10:30:00Z",
                metadata={
                    "source_id": "editor",
                    "kind": "source_event:updated",
                },
            )
        ]
    )
    orchestrator = QueryOrchestrator(
        fts=runtime.fts,
        semantic=semantic,
        answerer=runtime.answerer,
    )
    request = _filtered_request("hybrid")

    first = orchestrator.query(request)
    second = orchestrator.query(request)

    assert _ids(first) == [ALPHA_ID, BETA_ID]
    assert [item.rank for item in first.results] == [1, 1]
    assert [item.combined_score for item in first.results] == pytest.approx([0.5, 0.5])
    assert [item.id for item in second.results] == [item.id for item in first.results]
    assert [item.rank for item in second.results] == [item.rank for item in first.results]
    assert [item.combined_score for item in second.results] == pytest.approx(
        [item.combined_score for item in first.results]
    )
    assert first.meta["sources"] == ["fts", "semantic"]
    assert first.meta["weights"] == {"fts": 0.5, "semantic": 0.5}
    assert first.warnings == ()
    assert semantic.requests[-1].filters == {
        "source_ids": ["editor"],
        "kinds": ["source_event:updated"],
    }
    assert _as_datetime(semantic.requests[-1].start) == _as_datetime(request["start"])
    assert _as_datetime(semantic.requests[-1].end) == _as_datetime(request["end"])


def test_hybrid_fallback_and_semantic_mode_report_unavailable_backend_explicitly(
    ingested_runtime: dict[str, Any],
) -> None:
    runtime = ingested_runtime["runtime"]
    request = _filtered_request("hybrid")

    fallback = runtime.search(request)
    semantic_only = runtime.search({**request, "mode": "semantic"})

    assert _ids(fallback) == [ALPHA_ID]
    assert fallback.meta["sources"] == ["fts"]
    assert fallback.meta["weights"] == {"fts": 1.0}
    assert fallback.warnings == EXPECTED_HYBRID_WARNING

    assert semantic_only.results == ()
    assert semantic_only.evidence == ()
    assert semantic_only.meta["sources"] == []
    assert semantic_only.warnings == (
        "semantic retrieval unavailable: no semantic backend is configured",
        "no retrieval backend is configured for the requested mode",
    )
