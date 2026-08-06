"""Contract validation tests for runtime search and timeline projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from contracts import ObservationV1, QueryResponse

from services.context_server.contracts_adapter import (
    COMPATIBILITY_METADATA_KEY,
    QUERY_RESPONSE_UNREPRESENTED_FIELDS,
    adapt_search_response,
    adapt_timeline,
)
from services.context_server.config import Settings
from services.context_server.runtime import ContextRuntime


UTC_TIMESTAMP = "2026-08-05T20:00:00.123456Z"
ARTIFACT_DIGEST = "a" * 64
RAW_TEXT = "  λ\u00a0🧠\r\n\tindented line  \ntrailing-space:   "


def test_timeline_projection_preserves_raw_payload_and_digest_evidence() -> None:
    payload = {
        "raw_text": RAW_TEXT,
        "window_title": "life-context test editor",
        "nested": ["  exact  ", {"line_ending": "\r\n"}],
    }
    timeline = {
        "items": [
            {
                "id": "observation:event-1",
                "capture_id": "capture-1",
                "source_id": "clipboard:local",
                "kind": "source_event:observed",
                "observed_at": UTC_TIMESTAMP,
                "occurred_at": UTC_TIMESTAMP,
                "captured_at": UTC_TIMESTAMP,
                "text": RAW_TEXT,
                "content": RAW_TEXT,
                "payload": payload,
                "metadata": {
                    "event_id": "event-1",
                    "artifact_digest": ARTIFACT_DIGEST,
                    "artifact_url": f"/v1/artifacts/{ARTIFACT_DIGEST}",
                },
            }
        ],
        "total_count": 1,
        "generated_at": "2026-08-05T20:00:01Z",
    }

    projected = adapt_timeline(timeline)
    response = QueryResponse.model_validate(projected)
    observation = response.observations[0]

    assert json.loads(json.dumps(projected, ensure_ascii=False)) == projected
    assert observation.value == payload
    assert observation.value["raw_text"] == RAW_TEXT
    assert observation.source_event_id == "event-1"
    assert len(observation.evidence) == 1
    assert observation.evidence[0].artifact_id == ARTIFACT_DIGEST
    assert observation.evidence[0].sha256 == ARTIFACT_DIGEST
    assert observation.evidence[0].uri == f"/v1/artifacts/{ARTIFACT_DIGEST}"
    assert observation.metadata["capture_id"] == "capture-1"
    compatibility = observation.metadata[COMPATIBILITY_METADATA_KEY]
    assert "capture_id" in compatibility["unrepresented_fields"]
    assert "captured_at" in compatibility["unrepresented_fields"]
    assert compatibility["runtime_fields"]["text"] == RAW_TEXT


def test_search_projection_maps_results_and_nested_artifact_evidence() -> None:
    payload = {"raw_text": RAW_TEXT, "source": "clipboard:local"}
    search = {
        "query": "clipboard",
        "answer": {"text": "A runtime-only answer", "evidence_ids": ["hit-1"]},
        "answer_text": "A runtime-only answer",
        "results": [
            {
                "id": "hit-1",
                "text": RAW_TEXT,
                "source": "clipboard:local",
                "timestamp": UTC_TIMESTAMP,
                "score": 0.8,
                "metadata": {
                    "event_id": "event-1",
                    "source_id": "clipboard:local",
                    "kind": "source_event:observed",
                    "payload": payload,
                },
            }
        ],
        "evidence": [
            {
                "id": "hit-1",
                "excerpt": RAW_TEXT,
                "metadata": {
                    "artifacts": [
                        {
                            "artifact_id": "screen-1",
                            "kind": "image",
                            "uri": "artifact://screen-1",
                            "media_type": "image/jpeg",
                            "size_bytes": 27,
                            "sha256": ARTIFACT_DIGEST,
                            "metadata": {"role": "monitor"},
                        }
                    ]
                },
            }
        ],
        "meta": {"result_count": 3, "sources": ["fts", "semantic"]},
        "warnings": ["semantic backend unavailable"],
    }

    projected = adapt_search_response(search)
    response = QueryResponse.model_validate(projected)
    observation = response.observations[0]

    assert response.total_count == 3
    assert observation.observation_id == "hit-1"
    assert observation.value == payload
    assert observation.confidence == 0.8
    assert len(observation.evidence) == 1
    assert observation.evidence[0].artifact_id == "screen-1"
    assert observation.evidence[0].kind.value == "image"
    assert observation.evidence[0].sha256 == ARTIFACT_DIGEST
    compatibility = observation.metadata[COMPATIBILITY_METADATA_KEY]
    assert compatibility["source_shape"] == "runtime_search"
    assert "score" in compatibility["unrepresented_fields"]
    assert "answer" in QUERY_RESPONSE_UNREPRESENTED_FIELDS or "answer_text" in (
        QUERY_RESPONSE_UNREPRESENTED_FIELDS
    )


def test_timeline_alias_accepts_a_single_runtime_observation() -> None:
    projected = adapt_timeline(
        {
            "id": "observation:manual-1",
            "source_id": "manual:test",
            "kind": "measurement",
            "observed_at": UTC_TIMESTAMP,
            "event_id": "manual-1",
            "payload": {"value": 42, "unit": "count"},
        }
    )

    response = QueryResponse.model_validate(projected)
    observation = ObservationV1.model_validate(response.observations[0].to_dict())
    assert observation.kind.value == "measurement"
    assert observation.predicate == "measurement"
    assert observation.value == {"value": 42, "unit": "count"}
    assert observation.source_event_id == "manual-1"


def test_adapter_projects_actual_runtime_search_and_timeline_outputs(tmp_path: Path) -> None:
    artifact_body = b"contract-adapter-artifact"
    artifact_digest = hashlib.sha256(artifact_body).hexdigest()
    payload = {"raw_text": f"{RAW_TEXT} contract-marker", "sequence": 7}
    runtime = ContextRuntime(
        Settings(data_dir=tmp_path / "server-data", docs_enabled=False)
    )
    runtime.put_artifact(
        artifact_digest,
        artifact_body,
        media_type="image/jpeg",
        metadata={"role": "monitor"},
    )
    runtime.ingest_batch(
        {
            "envelope_id": "envelope:adapter-runtime",
            "captured_at": UTC_TIMESTAMP,
            "source_kind": "application",
            "events": [
                {
                    "event_id": "event:adapter-runtime",
                    "source_id": "clipboard:local",
                    "source_kind": "manual",
                    "event_type": "observed",
                    "occurred_at": UTC_TIMESTAMP,
                    "payload": payload,
                    "artifacts": [
                        {
                            "artifact_id": artifact_digest,
                            "kind": "image",
                            "uri": f"/v1/artifacts/{artifact_digest}",
                            "media_type": "image/jpeg",
                            "sha256": artifact_digest,
                        }
                    ],
                }
            ],
        }
    )
    try:
        timeline = adapt_timeline(runtime.timeline({"query": "contract-marker"}))
        search = adapt_search_response(
            runtime.search({"query": "contract-marker", "include_evidence": True}).to_dict()
        )

        timeline_observation = QueryResponse.model_validate(timeline).observations[0]
        search_observation = QueryResponse.model_validate(search).observations[0]
        assert timeline_observation.value == payload
        assert search_observation.value == payload
        assert timeline_observation.evidence[0].sha256 == artifact_digest
        assert search_observation.evidence[0].sha256 == artifact_digest
    finally:
        runtime.shutdown()
