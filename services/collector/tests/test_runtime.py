from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from contracts import SourceEvent
from services.collector.capture import CaptureConfig, Monitor, RawFrame, ScreenCaptureService
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import RetryableTransportError
from services.collector.transport.transport import CollectorTransport


CAPTURED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeMonitorBackend:
    name = "fake-monitor-backend"

    def __init__(self) -> None:
        self.monitors = (
            Monitor("display-1", "DISPLAY1", 0, 0, 2, 2, True),
            Monitor("display-2", "DISPLAY2", 2, 0, 2, 2, False),
        )
        self.captured: list[str] = []
        self.closed = False

    def enumerate_monitors(self) -> tuple[Monitor, ...]:
        return self.monitors

    def capture(self, monitor: Monitor) -> RawFrame:
        self.captured.append(monitor.monitor_id)
        color = (220, 40, 40) if monitor.monitor_id == "display-1" else (40, 40, 220)
        return RawFrame(
            width=monitor.width,
            height=monitor.height,
            pixel_bytes=bytes(color) * (monitor.width * monitor.height),
            metadata={"fixture": True},
        )

    def close(self) -> None:
        self.closed = True


class FakeHTTPClient:
    """Injectable HTTP client used to make online/offline delivery deterministic."""

    def __init__(self, *, online: bool) -> None:
        self.online = online
        self.artifact_calls: list[Any] = []
        self.batch_calls: list[tuple[dict[str, Any], str | None]] = []

    def _raise_offline(self) -> None:
        error = RetryableTransportError("fake HTTP endpoint is offline")
        error.attempts = 1  # type: ignore[attr-defined]
        raise error

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        self.artifact_calls.append(artifact)
        if not self.online:
            self._raise_offline()
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        self.batch_calls.append((payload, batch_id))
        if not self.online:
            self._raise_offline()
        return ClientResponse(202, {"accepted": True}, {}, 1)


def _capture_service(backend: FakeMonitorBackend, sequence_id: str) -> ScreenCaptureService:
    return ScreenCaptureService(
        backend,
        config=CaptureConfig(
            interval_seconds=5,
            jpeg_quality=80,
            contact_sheet_max_tile_size=(12, 12),
            contact_sheet_padding=1,
            contact_sheet_label_height=4,
        ),
        now=lambda: CAPTURED_AT,
        sequence_id_factory=lambda: sequence_id,
    )


def _runtime(
    tmp_path: Path,
    client: FakeHTTPClient,
    *,
    server_url: str = "http://127.0.0.1:8000",
    capture: ScreenCaptureService | None = None,
) -> CollectorRuntime:
    spool_dir = tmp_path / "spool"
    transport = CollectorTransport(
        TransportConfig(
            server_url=server_url,
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
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
        ),
        transport=transport,
        capture=capture or _capture_service(FakeMonitorBackend(), "capture-test"),
    )
    runtime._source_events = lambda: []  # type: ignore[method-assign]
    return runtime


def test_one_tick_uploads_monitor_and_contact_sheet_refs_with_safe_metadata(tmp_path: Path) -> None:
    backend = FakeMonitorBackend()
    client = FakeHTTPClient(online=True)
    runtime = _runtime(tmp_path, client, capture=_capture_service(backend, "capture-one"))

    try:
        report = runtime.tick()
    finally:
        runtime.close()

    assert backend.captured == ["display-1", "display-2"]
    assert report["sequence_id"] == "capture-one"
    assert report["monitor_count"] == 2
    assert report["event_count"] == 3
    assert len(client.artifact_calls) == 3
    assert len(client.batch_calls) == 1

    envelope, batch_id = client.batch_calls[0]
    assert batch_id == "capture-one"
    refs = envelope["artifacts"]
    assert {ref["name"] for ref in refs} == {
        "display-1.jpg",
        "display-2.jpg",
        "contact-sheet.jpg",
    }
    monitor_refs = [ref for ref in refs if ref["name"] != "contact-sheet.jpg"]
    assert {ref["metadata"]["monitor_id"] for ref in monitor_refs} == {
        "display-1",
        "display-2",
    }
    assert all(ref["kind"] == "image" for ref in refs)
    assert all(ref["media_type"] == "image/jpeg" for ref in refs)
    assert all(ref["uri"] == f"/v1/artifacts/{ref['sha256']}" for ref in refs)

    assert envelope["metadata"]["audio"] is False
    assert envelope["metadata"]["video"] is False
    assert envelope["metadata"]["keylogging"] is False
    for event in envelope["events"]:
        assert event["source_id"].startswith("screen:")
        assert event["payload"].get("audio", False) is False
        assert event["payload"].get("video", False) is False
        assert event["metadata"].get("keylogging", False) is False


def test_offline_tick_keeps_artifacts_and_batch_in_durable_spool_until_replay(
    tmp_path: Path,
) -> None:
    client = FakeHTTPClient(online=False)
    runtime = _runtime(tmp_path, client)

    try:
        report = runtime.tick()
        assert report["transport"]["ok"] is False

        spool = runtime.transport.spool
        assert spool.status()["pending_artifacts"] == 3
        assert spool.status()["pending_batches"] == 1

        # A newly opened spool sees the same items, demonstrating that the
        # offline queue is durable rather than only held in runtime memory.
        from services.collector.transport.spool import LocalSpool

        reopened = LocalSpool(tmp_path / "spool")
        assert len(reopened.list_pending_artifacts()) == 3
        assert len(reopened.list_pending_batches()) == 1

        client.online = True
        replay = runtime.transport.replay()
    finally:
        runtime.close()

    assert replay.artifacts_uploaded == 3
    assert replay.batches_uploaded == 1
    assert runtime.transport.spool.status()["pending_artifacts"] == 0
    assert runtime.transport.spool.status()["pending_batches"] == 0


def test_remote_server_routes_clipboard_event_to_local_only_spool(tmp_path: Path) -> None:
    client = FakeHTTPClient(online=True)
    runtime = _runtime(
        tmp_path,
        client,
        server_url="https://remote.example.test",
    )
    clipboard_event = SourceEvent(
        event_id="clipboard-event",
        source_id="clipboard:local",
        source_kind="manual",
        event_type="observed",
        occurred_at=CAPTURED_AT,
        captured_at=CAPTURED_AT,
        payload={"raw_text": "secret clipboard value", "routing": {"local_only": True}},
        metadata={"plaintext": True},
    )
    runtime._source_events = lambda: [clipboard_event]  # type: ignore[method-assign]

    try:
        report = runtime.tick()
        remote_envelope, _ = client.batch_calls[0]
        local_batches = runtime.local_only_spool.list_pending_batches()
    finally:
        runtime.close()

    assert report["local_only_events"] == 1
    assert all(event["source_id"] != "clipboard:local" for event in remote_envelope["events"])
    assert "secret clipboard value" not in repr(remote_envelope)
    assert len(local_batches) == 1
    assert local_batches[0].payload["events"][0]["source_id"] == "clipboard:local"
    assert local_batches[0].payload["events"][0]["payload"]["raw_text"] == (
        "secret clipboard value"
    )


@pytest.mark.parametrize("server_url", ["http://127.0.0.1:8000", "http://localhost:8000"])
def test_local_server_does_not_move_clipboard_event_to_local_only_spool(
    tmp_path: Path,
    server_url: str,
) -> None:
    client = FakeHTTPClient(online=True)
    runtime = _runtime(tmp_path, client, server_url=server_url)
    runtime._source_events = lambda: [
        SourceEvent(
            event_id="clipboard-local",
            source_id="clipboard:local",
            source_kind="manual",
            event_type="observed",
            occurred_at=CAPTURED_AT,
            captured_at=CAPTURED_AT,
            payload={"raw_text": "local clipboard"},
        )
    ]  # type: ignore[method-assign]

    try:
        runtime.tick()
        remote_envelope, _ = client.batch_calls[0]
        local_batches = runtime.local_only_spool.list_pending_batches()
    finally:
        runtime.close()

    assert any(event["source_id"] == "clipboard:local" for event in remote_envelope["events"])
    assert local_batches == []
