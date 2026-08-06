"""Contract audit tests for the collector-to-runtime integration seam.

These tests deliberately exercise the public collector/runtime entry points
instead of reconstructing their payloads by hand.  The collector envelope is
expected to validate directly against the shared Pydantic contract.  The
runtime query response is also checked for the richer answer/evidence shape;
one strict xfail records the current incompatibility with the narrower shared
``contracts.QueryResponse`` model so it cannot be mistaken for coverage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts import (
    ArtifactKind,
    ArtifactRef,
    CaptureEnvelope,
    ObservationKind,
    ObservationStatus,
    ObservationV1,
    QueryResponse as SharedQueryResponse,
)
from services.collector.capture.models import (
    CaptureSequence,
    EncodedImage,
    Monitor,
    MonitorCapture,
)
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.sources.browser_clipboard import (
    ClipboardSnapshot,
    ClipboardSource,
)
from services.collector.transport.config import TransportConfig
from services.collector.transport.spool import StoredArtifact
from services.context_server.config import Settings
from services.context_server.runtime import ContextRuntime


UTC = timezone.utc
CAPTURED_AT = datetime(2026, 8, 5, 20, 0, 0, 123456, tzinfo=UTC)
CLIPBOARD_MARKER = "  λ\u00a0🧠\r\n\tindented line  \ntrailing-space:   "


@dataclass(frozen=True, slots=True)
class _FixedCapture:
    sequence: CaptureSequence

    def capture_once(self) -> CaptureSequence:
        return self.sequence

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Window:
    timestamp: float

    def as_dict(self) -> dict[str, object]:
        return {
            "window_title": "life-context test editor",
            "process_name": "test-editor.exe",
            "window_handle": 123,
        }


@dataclass(slots=True)
class _Replay:
    def as_dict(self) -> dict[str, object]:
        return {"ok": True, "batches": {"attempted": 0, "uploaded": 0}}


@dataclass(slots=True)
class _RecordingTransport:
    """Small transport double that records exactly what the collector emits."""

    root: Path
    config: TransportConfig = field(init=False)
    batches: list[dict[str, object]] = field(default_factory=list)
    artifact_bytes: dict[str, bytes] = field(default_factory=dict)
    artifact_metadata: dict[str, dict[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config = TransportConfig(
            server_url="http://127.0.0.1:8000",
            spool_dir=self.root / "spool",
        )

    def enqueue_artifact(
        self,
        source: object,
        *,
        metadata: dict[str, object] | None = None,
    ) -> StoredArtifact:
        assert isinstance(source, bytes)
        digest = hashlib.sha256(source).hexdigest()
        artifact_path = self.root / "artifacts" / digest
        artifact_metadata = dict(metadata or {})
        self.artifact_bytes[digest] = source
        self.artifact_metadata[digest] = artifact_metadata
        return StoredArtifact(
            sha256=digest,
            path=artifact_path,
            size=len(source),
            metadata=artifact_metadata,
        )

    def enqueue_batch(
        self,
        records: dict[str, object],
        *,
        batch_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del batch_id, payload
        self.batches.append(dict(records))
        return dict(records)

    def replay(self) -> _Replay:
        return _Replay()

    def status(self) -> dict[str, object]:
        return {"pending_batches": 0, "pending_artifacts": 0}


def _capture_sequence() -> CaptureSequence:
    monitor = Monitor(
        monitor_id="monitor-1",
        name="Primary",
        left=0,
        top=0,
        width=1280,
        height=720,
        is_primary=True,
    )
    monitor_bytes = b"synthetic-monitor-jpeg"
    contact_sheet_bytes = b"synthetic-contact-sheet-jpeg"
    monitor_image = EncodedImage(
        jpeg_bytes=monitor_bytes,
        width=1280,
        height=720,
        sha256=hashlib.sha256(monitor_bytes).hexdigest(),
    )
    contact_sheet = EncodedImage(
        jpeg_bytes=contact_sheet_bytes,
        width=640,
        height=360,
        sha256=hashlib.sha256(contact_sheet_bytes).hexdigest(),
    )
    return CaptureSequence(
        sequence_id="capture-contract-audit",
        captured_at=CAPTURED_AT,
        monitors=(
            MonitorCapture(
                sequence_id="capture-contract-audit",
                monitor=monitor,
                captured_at=CAPTURED_AT,
                image=monitor_image,
            ),
        ),
        contact_sheet=contact_sheet,
        metadata={"backend": "contract-test", "audio": False, "video": False},
    )


def _collector_run(tmp_path: Path) -> tuple[CaptureEnvelope, _RecordingTransport]:
    transport = _RecordingTransport(tmp_path)
    collector = CollectorRuntime(
        CollectorConfig(
            interval_seconds=5.0,
            roots=(),
            clipboard_enabled=True,
            browser_enabled=False,
            include_git_history=False,
            include_processes=False,
            spool_dir=tmp_path / "collector-spool",
        ),
        transport=transport,
        capture=_FixedCapture(_capture_sequence()),
    )
    collector.window_source = _Window(CAPTURED_AT.timestamp())
    collector.clipboard_source = ClipboardSource(
        enabled=True,
        reader=lambda: ClipboardSnapshot(CLIPBOARD_MARKER, sequence_number=41),
        platform_name="windows",
        prefer_listener=False,
        clock=lambda: CAPTURED_AT,
    )

    report = collector.tick()
    assert report["accepted"] >= 3
    assert len(transport.batches) == 1
    return CaptureEnvelope.model_validate(transport.batches[0]), transport


def _runtime_for_envelope(
    tmp_path: Path,
    envelope: CaptureEnvelope,
    transport: _RecordingTransport,
) -> ContextRuntime:
    runtime = ContextRuntime(
        Settings(
            data_dir=tmp_path / "server-data",
            docs_enabled=False,
            raw_retention_hours=24.0,
            keyframe_retention_days=30.0,
        )
    )
    for digest, body in transport.artifact_bytes.items():
        runtime.put_artifact(
            digest,
            body,
            media_type="image/jpeg",
            metadata=transport.artifact_metadata[digest],
        )
    result = runtime.ingest_batch(
        envelope.to_dict(),
        idempotency_key=envelope.envelope_id,
    )
    assert result["accepted"] == len(envelope.events)
    return runtime


def test_collector_generated_envelope_validates_and_preserves_clipboard_exactly(
    tmp_path: Path,
) -> None:
    envelope, _ = _collector_run(tmp_path)

    clipboard_events = [
        event for event in envelope.events if event.source_id == "clipboard:local"
    ]
    assert len(clipboard_events) == 1
    clipboard_event = clipboard_events[0]
    assert clipboard_event.payload["raw_text"] == CLIPBOARD_MARKER
    assert clipboard_event.payload["raw_text"].encode("utf-8") == CLIPBOARD_MARKER.encode(
        "utf-8"
    )

    # Validate the exact JSON boundary, not only the already-instantiated
    # Pydantic object.  This catches serializer-side whitespace or Unicode
    # changes in the collector transport.
    wire_payload = json.loads(envelope.to_json())
    wire_clipboard = next(
        event for event in wire_payload["events"] if event["source_id"] == "clipboard:local"
    )
    assert wire_clipboard["payload"]["raw_text"] == CLIPBOARD_MARKER
    assert wire_clipboard["payload"]["raw_text"].encode("utf-8") == CLIPBOARD_MARKER.encode(
        "utf-8"
    )

    assert envelope.metadata["audio"] is False
    assert envelope.metadata["video"] is False
    assert all(ref.kind is ArtifactKind.IMAGE for ref in envelope.artifacts)
    assert all(isinstance(ref, ArtifactRef) for ref in envelope.artifacts)


def test_runtime_query_and_evidence_keep_the_original_clipboard_marker(
    tmp_path: Path,
) -> None:
    envelope, transport = _collector_run(tmp_path)
    runtime = _runtime_for_envelope(tmp_path, envelope, transport)
    try:
        response = runtime.search(
            {
                "query": CLIPBOARD_MARKER[2:5],
                "limit": 20,
                "include_evidence": True,
            }
        )
        payload = response.to_dict()

        evidence = next(
            item for item in payload["evidence"] if item["source"] == "clipboard:local"
        )
        result = next(
            item for item in payload["results"] if item["source"] == "clipboard:local"
        )
        assert evidence["excerpt"] == CLIPBOARD_MARKER
        assert result["text"] == CLIPBOARD_MARKER
        assert evidence["excerpt"].encode("utf-8") == CLIPBOARD_MARKER.encode("utf-8")
        assert result["metadata"]["payload"]["raw_text"] == CLIPBOARD_MARKER

        # The runtime's observation can be projected onto the shared
        # ObservationV1 contract without losing the original source-event
        # payload or artifact citation.  This is the compatibility shape the
        # direct-query xfail below documents as still missing on the wire.
        metadata = result["metadata"]
        digest = metadata.get("artifact_digest")
        evidence_refs = []
        if digest:
            evidence_refs.append(
                ArtifactRef(
                    artifact_id=str(digest),
                    kind=ArtifactKind.IMAGE,
                    uri=metadata.get("artifact_url"),
                    sha256=str(digest),
                )
            )
        observation = ObservationV1(
            observation_id=result["id"],
            subject=result["source"],
            predicate=metadata["kind"],
            value=metadata["payload"],
            kind=ObservationKind.FACT,
            status=ObservationStatus.CANDIDATE,
            confidence=float(result["score"]),
            observed_at=result["timestamp"],
            source_event_id=metadata["event_id"],
            evidence=evidence_refs,
            metadata=metadata,
        )
        assert observation.value["raw_text"] == CLIPBOARD_MARKER
    finally:
        runtime.shutdown()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known contract drift: runtime QueryResponse emits answer/evidence/results, "
        "while contracts.QueryResponse only accepts observations/pagination"
    ),
)
def test_runtime_query_payload_is_directly_accepted_by_shared_query_response(
    tmp_path: Path,
) -> None:
    envelope, transport = _collector_run(tmp_path)
    runtime = _runtime_for_envelope(tmp_path, envelope, transport)
    try:
        payload = runtime.search(
            {"query": CLIPBOARD_MARKER[2:5], "include_evidence": True}
        ).to_dict()
        SharedQueryResponse.model_validate(payload)
    finally:
        runtime.shutdown()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known contract drift: runtime timeline items are source-event records and "
        "do not directly contain ObservationV1 subject/predicate/value fields"
    ),
)
def test_runtime_timeline_item_is_directly_accepted_by_observation_v1(
    tmp_path: Path,
) -> None:
    envelope, transport = _collector_run(tmp_path)
    runtime = _runtime_for_envelope(tmp_path, envelope, transport)
    try:
        item = runtime.observations({"query": CLIPBOARD_MARKER[2:5]})["items"][0]
        ObservationV1.model_validate(item)
    finally:
        runtime.shutdown()
