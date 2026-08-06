"""Collector-to-contract serialization acceptance coverage.

This suite injects every platform-dependent collector boundary and inspects the
JSON batch produced by :class:`CollectorRuntime`.  It intentionally does not
exercise source routing policy; the purpose here is the runtime-to-contract
wire shape and its failure boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from contracts import CaptureEnvelope

from services.collector.capture.models import (
    CaptureSequence,
    EncodedImage,
    Monitor,
    MonitorCapture,
)
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.sources.browser_clipboard import BrowserMetadata, ClipboardEvent
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.transport import CollectorTransport

CAPTURED_AT = datetime(2026, 8, 5, 12, 34, 56, tzinfo=UTC)
RAW_CLIPBOARD = "  \t\u03bb\u00a0\U0001f9e0\r\n\tindented line  \ntrailing-space:   "

SOURCE_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "source_id",
        "source_kind",
        "event_type",
        "occurred_at",
        "captured_at",
        "payload",
        "artifacts",
        "metadata",
    }
)
CAPTURE_ENVELOPE_FIELDS = frozenset(
    {
        "envelope_id",
        "schema_version",
        "captured_at",
        "source_kind",
        "events",
        "artifacts",
        "producer",
        "metadata",
    }
)
ARTIFACT_REF_FIELDS = frozenset(
    {
        "artifact_id",
        "kind",
        "uri",
        "name",
        "media_type",
        "size_bytes",
        "sha256",
        "metadata",
    }
)
LEGACY_ALIAS_FIELDS = frozenset({"id", "capture_id", "source_type", "type"})


class _FakeCapture:
    def __init__(self) -> None:
        monitor_bytes = b"contract-monitor-image"
        contact_sheet_bytes = b"contract-contact-sheet"
        monitor = Monitor("display-contract", "Contract display", 0, 0, 2, 2, True)
        self.sequence = CaptureSequence(
            sequence_id="contract-sequence",
            captured_at=CAPTURED_AT,
            monitors=(
                MonitorCapture(
                    sequence_id="contract-sequence",
                    monitor=monitor,
                    captured_at=CAPTURED_AT,
                    image=EncodedImage(
                        jpeg_bytes=monitor_bytes,
                        width=2,
                        height=2,
                        sha256=hashlib.sha256(monitor_bytes).hexdigest(),
                    ),
                ),
            ),
            contact_sheet=EncodedImage(
                jpeg_bytes=contact_sheet_bytes,
                width=2,
                height=2,
                sha256=hashlib.sha256(contact_sheet_bytes).hexdigest(),
            ),
            metadata={"backend": "injected-contract-capture"},
        )
        self.capture_calls = 0
        self.close_calls = 0

    def capture_once(self) -> CaptureSequence:
        self.capture_calls += 1
        return self.sequence

    def close(self) -> None:
        self.close_calls += 1


class _FakeBrowserSource:
    def read(self) -> BrowserMetadata:
        return BrowserMetadata(
            title="Contract browser fixture",
            url="https://example.invalid/contracts",
            browser="fixture-browser",
            captured_at=CAPTURED_AT,
            available=True,
        )


class _FakeClipboardSource:
    def poll_once(self) -> ClipboardEvent:
        return ClipboardEvent(
            text=RAW_CLIPBOARD,
            captured_at=CAPTURED_AT,
            sequence_number=23,
        )


class _CapturingHTTPClient:
    def __init__(self) -> None:
        self.artifact_calls: list[Any] = []
        self.batch_calls: list[tuple[dict[str, Any], str | None]] = []

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        self.artifact_calls.append(artifact)
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        self.batch_calls.append((payload, batch_id))
        return ClientResponse(202, {"accepted": True}, {}, 1)


def _runtime(
    tmp_path: Path,
) -> tuple[CollectorRuntime, _CapturingHTTPClient, _FakeCapture]:
    spool_dir = tmp_path / "collector-spool"
    client = _CapturingHTTPClient()
    transport = CollectorTransport(
        TransportConfig(
            server_url="http://127.0.0.1:8000",
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        ),
        client=client,
    )
    capture = _FakeCapture()
    runtime = CollectorRuntime(
        CollectorConfig(
            interval_seconds=5,
            roots=(),
            clipboard_enabled=True,
            browser_enabled=True,
            include_git_history=False,
            include_processes=False,
            remote_clipboard=True,
            spool_dir=spool_dir,
            source_routing={
                "screenshots": {"enabled": True, "route": "remote"},
                "foreground": {"enabled": False, "route": "remote"},
                "filesystem": {"enabled": False, "route": "remote"},
                "git": {"enabled": False, "route": "remote"},
                "powershell": {"enabled": False, "route": "remote"},
                "process": {"enabled": False, "route": "remote"},
                "browser": {"enabled": True, "route": "remote"},
                "clipboard": {"enabled": True, "route": "remote"},
            },
        ),
        transport=transport,
        capture=capture,
    )
    runtime.browser_source = _FakeBrowserSource()
    runtime.clipboard_source = _FakeClipboardSource()
    return runtime, client, capture


def test_runtime_capture_batch_round_trips_through_strict_contract_wire_shape(
    tmp_path: Path,
) -> None:
    runtime, client, capture = _runtime(tmp_path)
    try:
        report = runtime.tick()
    finally:
        runtime.close()

    assert capture.capture_calls == 1
    assert capture.close_calls == 1
    assert report["sequence_id"] == "contract-sequence"
    assert report["event_count"] == 4
    assert len(client.artifact_calls) == 2
    assert len(client.batch_calls) == 1

    wire, batch_id = client.batch_calls[0]
    assert batch_id == "contract-sequence"
    assert set(wire) == CAPTURE_ENVELOPE_FIELDS
    assert not LEGACY_ALIAS_FIELDS.intersection(wire)
    assert wire["envelope_id"] == "contract-sequence"
    assert wire["schema_version"] == "1"
    assert wire["captured_at"] == "2026-08-05T12:34:56Z"

    validated = CaptureEnvelope.model_validate(wire)
    restored = CaptureEnvelope.from_json(validated.to_json())
    assert restored == validated

    canonical = json.loads(validated.to_json())
    assert set(canonical) == CAPTURE_ENVELOPE_FIELDS
    assert not LEGACY_ALIAS_FIELDS.intersection(canonical)

    for event in canonical["events"]:
        assert set(event) == SOURCE_EVENT_FIELDS
        assert not LEGACY_ALIAS_FIELDS.intersection(event)
        assert event["occurred_at"] == "2026-08-05T12:34:56Z"
        assert event["captured_at"] == "2026-08-05T12:34:56Z"

    for artifact in canonical["artifacts"]:
        assert set(artifact) == ARTIFACT_REF_FIELDS
        assert not LEGACY_ALIAS_FIELDS.intersection(artifact)
        assert artifact["kind"] == "image"
        assert artifact["media_type"] == "image/jpeg"
        assert artifact["artifact_id"] == artifact["sha256"]
        assert artifact["uri"] == f"/v1/artifacts/{artifact['sha256']}"
        assert artifact["size_bytes"] > 0

    refs_by_id = {artifact.artifact_id: artifact for artifact in validated.artifacts}
    event_refs = {
        artifact.artifact_id for event in validated.events for artifact in event.artifacts
    }
    assert event_refs == set(refs_by_id)
    assert all(artifact.kind.value == "image" for artifact in refs_by_id.values())

    screen_events = [event for event in validated.events if event.source_id.startswith("screen:")]
    assert len(screen_events) == 2
    assert all(event.artifacts for event in screen_events)
    assert all(
        artifact.artifact_id in refs_by_id
        for event in screen_events
        for artifact in event.artifacts
    )

    browser_event = next(
        event for event in validated.events if event.source_id == "browser:foreground"
    )
    clipboard_event = next(
        event for event in validated.events if event.source_id == "clipboard:local"
    )
    assert browser_event.payload["captured_at"] == "2026-08-05T12:34:56+00:00"
    assert browser_event.payload["audio"] is None
    assert browser_event.payload["video"] is None
    assert browser_event.payload["routing"]["supports_audio"] is False
    assert browser_event.payload["routing"]["supports_video"] is False
    assert browser_event.payload["routing"]["audio"] is False
    assert browser_event.payload["routing"]["video"] is False

    assert clipboard_event.payload["raw_text"] == RAW_CLIPBOARD
    assert clipboard_event.payload["raw_text"].encode("utf-8") == RAW_CLIPBOARD.encode("utf-8")
    assert clipboard_event.payload["routing"]["supports_audio"] is False
    assert clipboard_event.payload["routing"]["supports_video"] is False
    assert clipboard_event.payload["routing"]["audio"] is False
    assert clipboard_event.payload["routing"]["video"] is False

    assert validated.metadata["audio"] is False
    assert validated.metadata["video"] is False
    assert validated.metadata["keylogging"] is False


def test_runtime_rejects_malformed_source_payload_before_enveloping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import services.collector.runtime as runtime_module

    malformed = {
        "event_id": "malformed-source-event",
        "source_id": "git_terminal:malformed",
        "source_kind": "application",
        "event_type": "observed",
        "occurred_at": "2026-08-05T12:34:56Z",
        "captured_at": "2026-08-05T12:34:56Z",
        "payload": {"event_kind": "git_status", "repository": "fixture"},
        "artifacts": [],
        "metadata": {},
        "unexpected": "reject me",
    }
    monkeypatch.setattr(
        runtime_module,
        "discover_git_repositories",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        runtime_module,
        "collect_git_terminal_events",
        lambda **_kwargs: [malformed],
    )

    spool_dir = tmp_path / "collector-spool"
    client = _CapturingHTTPClient()
    transport = CollectorTransport(
        TransportConfig(
            server_url="http://127.0.0.1:8000",
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        ),
        client=client,
    )
    runtime = CollectorRuntime(
        CollectorConfig(
            interval_seconds=5,
            roots=(),
            clipboard_enabled=False,
            browser_enabled=False,
            include_git_history=False,
            include_processes=False,
            spool_dir=spool_dir,
            source_routing={
                "screenshots": {"enabled": False, "route": "remote"},
                "foreground": {"enabled": False, "route": "remote"},
                "filesystem": {"enabled": False, "route": "remote"},
                "git": {"enabled": True, "route": "remote"},
                "powershell": {"enabled": False, "route": "remote"},
                "process": {"enabled": False, "route": "remote"},
                "browser": {"enabled": False, "route": "remote"},
                "clipboard": {"enabled": False, "route": "remote"},
            },
        ),
        transport=transport,
        capture=_FakeCapture(),
    )

    try:
        report = runtime.tick()
    finally:
        runtime.close()

    assert report["accepted"] == 0
    assert "event_count" not in report
    assert client.batch_calls == []
    assert runtime.last_error is not None
    assert runtime.last_error.startswith("git/terminal metadata:")
