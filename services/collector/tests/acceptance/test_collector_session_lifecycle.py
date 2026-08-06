from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from contracts import SourceEvent

from services.collector.capture.models import (
    CaptureSequence,
    EncodedImage,
    Monitor,
    MonitorCapture,
)
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import RetryableTransportError
from services.collector.transport.transport import CollectorTransport

CAPTURED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
TICK_COUNT = 3
EVENTS_PER_TICK = 2


class SafeFakeCaptureAdapter:
    """Deterministic capture adapter with no desktop, media, or GPU access."""

    name = "safe-fake-capture"

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.tick_count = 0
        self.closed = False
        self.monitor = Monitor(
            monitor_id="synthetic-display",
            name="Synthetic display",
            left=0,
            top=0,
            width=2,
            height=2,
            is_primary=True,
        )

    def capture_once(self) -> CaptureSequence:
        if self.closed:
            raise RuntimeError("capture adapter is paused")

        self.tick_count += 1
        sequence_id = f"{self.session_id}-tick-{self.tick_count}"
        captured_at = CAPTURED_AT + timedelta(seconds=(self.tick_count - 1) * 5)
        image_bytes = f"synthetic-monitor-jpeg:{sequence_id}".encode("ascii")
        image = EncodedImage(
            jpeg_bytes=image_bytes,
            width=self.monitor.width,
            height=self.monitor.height,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        return CaptureSequence(
            sequence_id=sequence_id,
            captured_at=captured_at,
            monitors=(
                MonitorCapture(
                    sequence_id=sequence_id,
                    monitor=self.monitor,
                    captured_at=captured_at,
                    image=image,
                ),
            ),
            contact_sheet=None,
            metadata={
                "backend": self.name,
                "audio": False,
                "video": False,
            },
        )

    def close(self) -> None:
        self.closed = True


class SafeFakeSourceAdapter:
    """Deterministic source adapter that emits one safe metadata event per tick."""

    def __init__(self) -> None:
        self.tick_count = 0

    def collect(self) -> list[SourceEvent]:
        self.tick_count += 1
        occurred_at = CAPTURED_AT + timedelta(seconds=(self.tick_count - 1) * 5)
        return [
            SourceEvent(
                event_id=f"synthetic-source-event-{self.tick_count}",
                source_id="synthetic:source",
                source_kind="application",
                event_type="observed",
                occurred_at=occurred_at,
                captured_at=occurred_at,
                payload={
                    "tick": self.tick_count,
                    "audio": False,
                    "video": False,
                },
                metadata={
                    "adapter": "safe-fake-source",
                    "keylogging": False,
                },
            )
        ]


class SafeFakeCollectorRuntime(CollectorRuntime):
    """Collector runtime wired only to the deterministic fake source adapter."""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        transport: CollectorTransport,
        capture: SafeFakeCaptureAdapter,
        source: SafeFakeSourceAdapter,
    ) -> None:
        self.source = source
        super().__init__(config, transport=transport, capture=capture)  # type: ignore[arg-type]

    def _source_events(self) -> list[SourceEvent]:
        return self.source.collect()


@dataclass
class IdempotentFakeCollectorServer:
    """Offline/reconnect server double with durable-style idempotency behavior."""

    online: bool = False
    drop_next_batch_response: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)
    received_artifacts: dict[str, bytes] = field(default_factory=dict)
    accepted_batches: dict[str, dict[str, Any]] = field(default_factory=dict)
    accepted_batch_order: list[str] = field(default_factory=list)
    accepted_event_ids: list[str] = field(default_factory=list)
    duplicate_batch_ids: list[str] = field(default_factory=list)

    @staticmethod
    def _raise_offline(operation: str) -> None:
        error = RetryableTransportError(f"{operation} endpoint is offline")
        error.attempts = 1  # type: ignore[attr-defined]
        raise error

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        digest = str(artifact.sha256)
        content = Path(artifact.path).read_bytes()
        self.calls.append(("artifact", digest))
        if not self.online:
            self._raise_offline("artifact")

        if hashlib.sha256(content).hexdigest() != digest:
            raise AssertionError("artifact bytes do not match their content address")
        if digest in self.received_artifacts:
            return ClientResponse(409, {"sha256": digest, "duplicate": True}, {}, 1)

        self.received_artifacts[digest] = content
        return ClientResponse(201, {"sha256": digest}, {}, 1)

    def ingest_batch(
        self,
        payload: object,
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        if not isinstance(payload, dict):
            raise AssertionError("collector envelope must be a mapping")
        if not batch_id:
            raise AssertionError("collector envelope must have a stable batch id")

        self.calls.append(("batch", batch_id))
        if not self.online:
            self._raise_offline("batch")

        for artifact in payload.get("artifacts", []):
            if (
                not isinstance(artifact, dict)
                or artifact.get("sha256") not in self.received_artifacts
            ):
                raise AssertionError("envelope arrived before all referenced artifacts")

        if batch_id in self.accepted_batches:
            self.duplicate_batch_ids.append(batch_id)
            return ClientResponse(202, {"accepted": 0, "duplicate": True}, {}, 1)

        events = payload.get("events")
        if not isinstance(events, list):
            raise AssertionError("collector envelope must contain events")
        event_ids = [event.get("event_id") for event in events if isinstance(event, dict)]
        if len(event_ids) != len(events) or len(event_ids) != len(set(event_ids)):
            raise AssertionError("a batch contained duplicate or malformed event ids")
        if any(event_id in self.accepted_event_ids for event_id in event_ids):
            raise AssertionError("server would have stored a duplicate event")

        self.accepted_batches[batch_id] = payload
        self.accepted_batch_order.append(batch_id)
        self.accepted_event_ids.extend(event_ids)

        if self.drop_next_batch_response:
            self.drop_next_batch_response = False
            # The server has accepted the envelope, but the client must retry
            # because the response was lost.  The retry must remain idempotent.
            self.online = False
            self._raise_offline("batch response")

        return ClientResponse(202, {"accepted": len(events)}, {}, 1)


def _start_collector(
    spool_dir: Path,
    server: IdempotentFakeCollectorServer,
    *,
    session_id: str,
) -> SafeFakeCollectorRuntime:
    config = CollectorConfig(
        interval_seconds=5,
        roots=(),
        clipboard_enabled=False,
        browser_enabled=False,
        include_git_history=False,
        include_processes=False,
        spool_dir=spool_dir,
    )
    transport = CollectorTransport(
        TransportConfig(
            server_url="https://collector.test:8080",
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        ),
        client=server,
    )
    return SafeFakeCollectorRuntime(
        config,
        transport=transport,
        capture=SafeFakeCaptureAdapter(session_id),
        source=SafeFakeSourceAdapter(),
    )


def test_collector_session_pause_resume_replays_offline_spool_idempotently(
    tmp_path: Path,
) -> None:
    """Persist offline ticks, cross a pause/resume boundary, then replay safely."""

    spool_dir = tmp_path / "collector-spool"
    server = IdempotentFakeCollectorServer()
    collector = _start_collector(spool_dir, server, session_id="session-one")
    capture = collector.capture

    try:
        for index in range(1, TICK_COUNT + 1):
            report = collector.tick()
            assert report["transport"]["ok"] is False
            assert report["transport"]["artifacts"]["attempted"] == index
            assert report["transport"]["batches"]["attempted"] == index
            status = collector.transport.status()
            assert status["pending_artifacts"] == index
            assert status["pending_batches"] == index
            assert status["processing_batches"] == 0
    finally:
        collector.close()

    assert capture.closed is True
    with pytest.raises(RuntimeError, match="paused"):
        capture.capture_once()

    offline_status = collector.transport.status()
    assert offline_status["batches_enqueued"] == TICK_COUNT
    assert offline_status["records_enqueued"] == TICK_COUNT * EVENTS_PER_TICK
    assert offline_status["artifacts_stored"] == TICK_COUNT
    assert offline_status["artifacts_replayed"] == sum(range(1, TICK_COUNT + 1))
    assert offline_status["batches_replayed"] == sum(range(1, TICK_COUNT + 1))
    assert offline_status["artifacts_failed"] == sum(range(1, TICK_COUNT + 1))
    assert offline_status["batches_failed"] == sum(range(1, TICK_COUNT + 1))
    assert offline_status["replay_runs"] == TICK_COUNT
    assert offline_status["uploaded_artifacts"] == 0
    assert offline_status["pending_artifacts"] == TICK_COUNT
    assert offline_status["pending_batches"] == TICK_COUNT
    assert offline_status["processing_batches"] == 0

    expected_batch_ids = [
        batch.batch_id for batch in collector.transport.spool.list_pending_batches()
    ]
    expected_artifact_ids = [
        artifact.sha256 for artifact in collector.transport.spool.list_pending_artifacts()
    ]
    assert len(expected_batch_ids) == TICK_COUNT
    assert len(expected_artifact_ids) == TICK_COUNT

    # Desktop pause stops the collector process; resume starts a fresh one.
    # Reopening the same spool proves that queued work crosses that boundary.
    resumed = _start_collector(spool_dir, server, session_id="session-two")
    try:
        resumed_before_reconnect = resumed.transport.status()
        assert resumed_before_reconnect["pending_artifacts"] == TICK_COUNT
        assert resumed_before_reconnect["pending_batches"] == TICK_COUNT
        assert resumed_before_reconnect["processing_batches"] == 0

        server.online = True
        server.drop_next_batch_response = True
        reconnect_start = len(server.calls)
        first_reconnect = resumed.transport.replay()
        reconnect_calls = server.calls[reconnect_start:]

        assert first_reconnect.ok is False
        assert first_reconnect.artifacts_attempted == TICK_COUNT
        assert first_reconnect.artifacts_uploaded == TICK_COUNT
        assert first_reconnect.batches_attempted == TICK_COUNT
        assert first_reconnect.batches_uploaded == 0
        assert first_reconnect.batches_failed == TICK_COUNT
        expected_reconnect_calls = [
            ("artifact", artifact_id) for artifact_id in expected_artifact_ids
        ] + [("batch", batch_id) for batch_id in expected_batch_ids]
        assert reconnect_calls == expected_reconnect_calls
        assert len(server.accepted_event_ids) == EVENTS_PER_TICK

        partial_status = resumed.transport.status()
        assert partial_status["pending_artifacts"] == 0
        assert partial_status["uploaded_artifacts"] == TICK_COUNT
        assert partial_status["pending_batches"] == TICK_COUNT
        assert partial_status["processing_batches"] == 0
        assert partial_status["artifacts_uploaded"] == TICK_COUNT
        assert partial_status["batches_uploaded"] == 0
        assert partial_status["batches_failed"] == TICK_COUNT

        server.online = True
        second_replay = resumed.transport.replay()
        assert second_replay.ok is True
        assert second_replay.artifacts_attempted == 0
        assert second_replay.batches_attempted == TICK_COUNT
        assert second_replay.batches_uploaded == TICK_COUNT
        assert second_replay.batches_failed == 0
        assert server.duplicate_batch_ids == [expected_batch_ids[0]]

        final_status = resumed.transport.status()
        assert final_status["pending_artifacts"] == 0
        assert final_status["pending_batches"] == 0
        assert final_status["processing_batches"] == 0
        assert final_status["uploaded_artifacts"] == TICK_COUNT
        assert final_status["batches_uploaded"] == TICK_COUNT
        assert final_status["records_uploaded"] == TICK_COUNT * EVENTS_PER_TICK
    finally:
        resumed.close()

    assert server.accepted_batch_order == expected_batch_ids
    assert len(server.accepted_event_ids) == TICK_COUNT * EVENTS_PER_TICK
    assert len(server.accepted_event_ids) == len(set(server.accepted_event_ids))
    assert all(
        event["metadata"].get("keylogging", False) is False
        and event["payload"].get("audio", False) is False
        and event["payload"].get("video", False) is False
        for batch in server.accepted_batches.values()
        for event in batch["events"]
    )
