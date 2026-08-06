from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

import pytest

from services.context_server.config import Settings
from services.context_server.inference.fake import DeterministicFakeModelClient
from services.context_server.query import QueryOrchestrator
from services.context_server.runtime import ContextRuntime


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[ContextRuntime]:
    service = ContextRuntime(
        Settings(
            data_dir=tmp_path / "context-data",
            docs_enabled=False,
            model_endpoint="",
        )
    )
    try:
        yield service
    finally:
        service.shutdown()


def _observe(
    runtime: ContextRuntime,
    event_id: str,
    text: str,
    *,
    source_id: str,
    kind: str,
    observed_at: datetime,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return runtime.observe(
        {
            "event_id": event_id,
            "source_id": source_id,
            "kind": kind,
            "observed_at": observed_at.isoformat(),
            "text": text,
            "payload": {"text": text},
            "metadata": metadata or {},
        }
    )


def test_fts_literal_fallback_quotes_invalid_match_queries(runtime: ContextRuntime) -> None:
    """An FTS operator-looking query can still retrieve its literal text."""

    if not runtime.storage.fts5_available:
        pytest.skip("SQLite build has no FTS5 support")

    source = runtime.storage.create_source(
        "test",
        source_id="source:fts-literal",
        external_id="source:fts-literal",
    )
    matching = runtime.storage.create_observation(
        source_id=source.id,
        kind="note",
        observed_at="2026-01-01T12:00:00Z",
        text="literal alpha OR marker",
        observation_id="observation:fts-matching",
        idempotency_key="fts-matching",
    )
    runtime.storage.create_observation(
        source_id=source.id,
        kind="note",
        observed_at="2026-01-01T12:01:00Z",
        text="alpha marker without the operator",
        observation_id="observation:fts-decoy",
        idempotency_key="fts-decoy",
    )

    # ``alpha OR`` is malformed FTS5 syntax, so the storage adapter must retry
    # it as a quoted literal instead of surfacing sqlite3.OperationalError.
    direct_hits = runtime.storage.search_observations("alpha OR")
    assert [item.id for item in direct_hits] == [matching.id]

    response = runtime.search({"query": "alpha OR", "mode": "fts", "limit": 5})
    assert [item.id for item in response.results] == [matching.id]


def test_query_applies_time_source_and_kind_filters(runtime: ContextRuntime) -> None:
    base = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    entries = (
        ("old-editor-note", "editor", "note", base),
        ("in-editor-note", "editor", "note", base + timedelta(hours=1)),
        ("in-terminal-note", "terminal", "note", base + timedelta(hours=1, minutes=15)),
        ("in-editor-screenshot", "editor", "screenshot", base + timedelta(hours=1, minutes=30)),
        ("new-editor-note", "editor", "note", base + timedelta(hours=2)),
    )
    for event_id, source_id, kind, observed_at in entries:
        _observe(
            runtime,
            event_id,
            "query marker",
            source_id=source_id,
            kind=kind,
            observed_at=observed_at,
        )

    def result_ids(response: Any) -> list[str]:
        return [item.id for item in response.results]

    source_filtered = runtime.search(
        {
            "query": "query marker",
            "mode": "fts",
            "limit": 10,
            "filters": {"source_ids": ["editor"]},
        }
    )
    assert set(result_ids(source_filtered)) == {
        "observation:old-editor-note",
        "observation:in-editor-note",
        "observation:in-editor-screenshot",
        "observation:new-editor-note",
    }

    kind_filtered = runtime.search(
        {
            "query": "query marker",
            "mode": "fts",
            "limit": 10,
            "filters": {"kinds": ["note"]},
        }
    )
    assert set(result_ids(kind_filtered)) == {
        "observation:old-editor-note",
        "observation:in-editor-note",
        "observation:in-terminal-note",
        "observation:new-editor-note",
    }

    time_filtered = runtime.search(
        {
            "query": "query marker",
            "mode": "fts",
            "limit": 10,
            "start": (base + timedelta(minutes=30)).isoformat(),
            "end": (base + timedelta(hours=1, minutes=45)).isoformat(),
        }
    )
    assert result_ids(time_filtered) == [
        "observation:in-editor-note",
        "observation:in-terminal-note",
        "observation:in-editor-screenshot",
    ]

    combined = runtime.search(
        {
            "query": "query marker",
            "mode": "fts",
            "limit": 10,
            "filters": {"source_ids": ["editor"], "kinds": ["note"]},
            "start": (base + timedelta(minutes=30)).isoformat(),
            "end": (base + timedelta(hours=1, minutes=45)).isoformat(),
        }
    )
    assert result_ids(combined) == ["observation:in-editor-note"]


def test_query_preserves_legacy_filter_aliases_through_shared_contract(runtime: ContextRuntime) -> None:
    base = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    entries = (
        ("alias-editor-note", "editor", "note"),
        ("alias-editor-screenshot", "editor", "screenshot"),
        ("alias-terminal-note", "terminal", "note"),
    )
    for index, (event_id, source_id, kind) in enumerate(entries):
        _observe(
            runtime,
            event_id,
            "legacy filter marker",
            source_id=source_id,
            kind=kind,
            observed_at=base + timedelta(minutes=index),
        )

    source_filtered = runtime.search(
        {
            "query": "legacy filter marker",
            "mode": "fts",
            "limit": 10,
            "filters": {"source_id": "editor"},
        }
    )
    kind_filtered = runtime.search(
        {
            "query": "legacy filter marker",
            "mode": "fts",
            "limit": 10,
            "filters": {"kind": "note"},
        }
    )

    assert {item.id for item in source_filtered.results} == {
        "observation:alias-editor-note",
        "observation:alias-editor-screenshot",
    }
    assert {item.id for item in kind_filtered.results} == {
        "observation:alias-editor-note",
        "observation:alias-terminal-note",
    }


class _StaticBackend:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits

    def search(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return [dict(hit) for hit in self.hits]


def test_hybrid_ranking_is_repeatable_and_tie_breaks_by_id() -> None:
    orchestrator = QueryOrchestrator(
        fts=_StaticBackend([{"id": "zeta", "text": "z", "score": 1.0}]),
        semantic=_StaticBackend([{"id": "alpha", "text": "a", "score": 1.0}]),
    )

    first = orchestrator.query({"query": "tie", "limit": 2})
    second = orchestrator.query({"query": "tie", "limit": 2})

    assert [item.id for item in first.results] == ["alpha", "zeta"]
    assert [item.rank for item in first.results] == [1, 1]
    assert [item.combined_score for item in first.results] == pytest.approx([0.5, 0.5])
    assert [item.id for item in second.results] == [item.id for item in first.results]
    assert [item.rank for item in second.results] == [item.rank for item in first.results]


def test_hybrid_fallback_warns_when_semantic_backend_is_unavailable() -> None:
    orchestrator = QueryOrchestrator(
        fts=_StaticBackend([{"id": "fts-only", "text": "lexical", "score": 1.0}])
    )

    response = orchestrator.query({"query": "fallback", "mode": "hybrid", "limit": 5})

    assert [item.id for item in response.results] == ["fts-only"]
    assert response.meta["sources"] == ["fts"]
    assert response.meta["weights"] == {"fts": 1.0}
    assert response.warnings == (
        "semantic retrieval unavailable: no semantic backend is configured",
    )


def test_semantic_merge_preserves_and_normalizes_evidence_urls() -> None:
    artifact_url = "/v1/artifacts/" + "a" * 64
    orchestrator = QueryOrchestrator(
        fts=_StaticBackend(
            [
                {
                    "id": "shared-artifact",
                    "text": "lexical evidence",
                    "score": 1.0,
                    "metadata": {"artifact_url": artifact_url},
                }
            ]
        ),
        semantic=_StaticBackend(
            [
                {
                    "id": "shared-artifact",
                    "text": "semantic evidence",
                    "score": 1.0,
                    "metadata": {"artifact_url": None},
                },
                {
                    "id": "semantic-artifact",
                    "text": "semantic-only evidence",
                    "score": 0.5,
                    "artifact_url": artifact_url,
                },
            ]
        ),
    )

    response = orchestrator.query({"query": "artifact", "limit": 5, "include_evidence": True})
    by_id = {item.id: item for item in response.results}

    assert by_id["shared-artifact"].metadata["artifact_url"] == artifact_url
    assert by_id["semantic-artifact"].metadata["artifact_url"] == artifact_url
    assert {item.metadata["artifact_url"] for item in response.evidence} == {artifact_url}


def test_artifact_evidence_contains_a_retrievable_artifact_url(runtime: ContextRuntime) -> None:
    body = b"query evidence image"
    digest = sha256(body).hexdigest()
    artifact = runtime.put_artifact(digest, body, media_type="image/png")
    _observe(
        runtime,
        "artifact-evidence",
        "artifact evidence marker",
        source_id="editor",
        kind="screenshot",
        observed_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        metadata={"artifact_digest": digest},
    )

    response = runtime.search(
        {"query": "artifact evidence marker", "mode": "fts", "include_evidence": True}
    )
    expected_url = f"/v1/artifacts/{digest}"

    assert artifact["uri"] == expected_url
    assert response.results[0].metadata["artifact_url"] == expected_url
    assert response.evidence[0].metadata["artifact_url"] == expected_url
    assert response.to_dict()["evidence"][0]["metadata"]["artifact_url"] == expected_url
    assert runtime.storage.artifacts.read(digest) == body


def test_derived_observation_query_uses_persisted_primary_artifact_digest(
    runtime: ContextRuntime,
) -> None:
    if not runtime.storage.fts5_available:
        pytest.skip("SQLite build has no FTS5 support")

    body = b"derived inference evidence"
    digest = sha256(body).hexdigest()
    runtime.put_artifact(digest, body, media_type="image/png")
    source = runtime.storage.create_source(
        "inference",
        source_id="source:derived-inference",
        external_id="source:derived-inference",
    )
    record = runtime.storage.create_observation(
        source_id=source.id,
        kind="inference_observation",
        observed_at="2026-01-01T12:00:00Z",
        text_content="derived inference evidence marker",
        payload={"summary": "derived inference evidence marker"},
        metadata={"validation_status": "valid"},
        observation_id="inference:derived-evidence",
        idempotency_key="inference-observation:derived-evidence",
        artifact_digest=digest,
    )

    persisted = runtime.storage.connection.execute(
        "SELECT primary_artifact_digest FROM observations WHERE id = ?",
        (record.id,),
    ).fetchone()
    assert persisted["primary_artifact_digest"] == digest

    response = runtime.search(
        {
            "query": "derived inference evidence marker",
            "mode": "fts",
            "include_evidence": True,
        }
    )
    expected_url = f"/v1/artifacts/{digest}"

    assert response.results[0].metadata["validation_status"] == "valid"
    assert response.results[0].metadata["artifact_digest"] == digest
    assert response.results[0].metadata["artifact_url"] == expected_url
    assert response.evidence[0].metadata["artifact_digest"] == digest
    assert response.evidence[0].metadata["artifact_url"] == expected_url
    assert response.to_dict()["results"][0]["metadata"]["artifact_url"] == expected_url


def test_empty_query_results_have_a_synthesized_no_results_answer(runtime: ContextRuntime) -> None:
    response = runtime.search(
        {"query": "no archived context can match this", "mode": "fts", "limit": 5}
    )

    assert response.results == ()
    assert response.evidence == ()
    assert response.answer.text == "No matching context was found in the local archive."
    assert response.answer.confidence == 0.0
    assert response.answer.status == "no_results"
    assert response.answer.evidence_ids == ()
    assert response.answer.metadata == {"model_configured": False}
    assert response.meta["sources"] == ["fts"]
    assert response.meta["result_count"] == 0
    assert response.meta["evidence_count"] == 0


def test_conversational_hybrid_query_falls_back_to_recent_context(
    runtime: ContextRuntime,
) -> None:
    _observe(
        runtime,
        "recent-work-context",
        "Editing the Life Context natural-language query pipeline.",
        source_id="window:foreground",
        kind="source_event:observed",
        observed_at=datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc),
    )

    response = runtime.search(
        {
            "query": "What was I working on recently?",
            "mode": "hybrid",
            "limit": 5,
            "include_evidence": True,
        }
    )

    assert [item.id for item in response.results] == [
        "observation:recent-work-context"
    ]
    assert response.evidence[0].id == "observation:recent-work-context"
    assert "Life Context natural-language query pipeline" in response.answer.text
    assert response.answer.status == "fallback_evidence_summary"


def test_relative_time_question_filters_context_and_humanizes_terminal_history(
    runtime: ContextRuntime,
) -> None:
    now = datetime.now(timezone.utc)
    _observe(
        runtime,
        "old-context",
        "Old work that must not appear.",
        source_id="window:old",
        kind="source_event:observed",
        observed_at=now - timedelta(minutes=45),
    )
    _observe(
        runtime,
        "recent-terminal",
        (
            '{"commands":["cd .\\\\life-context\\\\",'
            '"claude --permission-mode bypassPermissions","pnpm test"]}'
        ),
        source_id="terminal:history",
        kind="source_event:observed",
        observed_at=now - timedelta(minutes=5),
    )

    response = runtime.search(
        {
            "query": "What have I been doing for the past 20 minutes?",
            "mode": "hybrid",
            "limit": 8,
            "include_evidence": True,
        }
    )

    assert [item.id for item in response.results] == [
        "observation:recent-terminal"
    ]
    assert "terminal-history snapshot" in response.answer.text
    assert "projects life-context" in response.answer.text
    assert "tools Claude, pnpm" in response.answer.text
    assert "Old work" not in response.answer.text
    assert '{"commands"' not in response.answer.text


def test_configured_model_synthesizes_answer_from_recent_context(tmp_path: Path) -> None:
    runtime = ContextRuntime(
        Settings(
            data_dir=tmp_path / "model-query-data",
            docs_enabled=False,
            model_endpoint="http://127.0.0.1:8080/v1",
            model_name="query-test-model",
        )
    )
    fake = DeterministicFakeModelClient(
        ["You were implementing natural-language context retrieval [1]."],
        model_name="query-test-model",
    )
    try:
        _observe(
            runtime,
            "model-query-context",
            "Implemented recent-context retrieval for conversational questions.",
            source_id="git:life-context",
            kind="source_event:observed",
            observed_at=datetime(2026, 8, 5, 20, 5, tzinfo=timezone.utc),
        )
        runtime.inference.client = fake

        response = runtime.search(
            {
                "query": "What was I working on?",
                "mode": "hybrid",
                "limit": 5,
                "include_evidence": True,
            }
        )

        assert response.answer.text == (
            "You were implementing natural-language context retrieval [1]."
        )
        assert response.answer.status == "model_synthesized"
        assert response.answer.evidence_ids == ("observation:model-query-context",)
        assert response.answer.metadata["model_name"] == "query-test-model"
        assert len(fake.calls) == 1
        assert "What was I working on?" in fake.calls[0].messages[1]["content"]
        assert "Implemented recent-context retrieval" in fake.calls[0].messages[1][
            "content"
        ]
    finally:
        runtime.shutdown()
