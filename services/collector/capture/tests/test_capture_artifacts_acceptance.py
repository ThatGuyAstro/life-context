"""Deterministic capture-artifact acceptance coverage.

The capture backend, clock, sequence IDs, and transport client are all safe
injected fakes.  The test still exercises the real JPEG encoder, contact-sheet
builder, collector artifact references, and content-addressed local spool.
No desktop, microphone, camera, or GPU is touched.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from services.collector.capture import (
    CaptureConfig,
    Monitor,
    RawFrame,
    ScreenCaptureService,
)
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.transport import CollectorTransport

CAPTURED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
SEQUENCE_IDS = ("capture-artifact-001", "capture-artifact-002")


class _SafeMultiMonitorBackend:
    """Deterministic RGB-only backend with a negative virtual-desktop origin."""

    name = "safe-injected-multi-monitor"

    monitors = (
        Monitor("display-primary", "Primary", 0, 0, 4, 3, True),
        Monitor("display-left", "Left", -4, 0, 4, 3),
        Monitor("display-above", "Above", 0, -3, 4, 3),
    )

    _COLORS = {
        "display-primary": (220, 40, 40),
        "display-left": (40, 180, 80),
        "display-above": (40, 80, 220),
    }

    def __init__(self) -> None:
        self.capture_calls: list[str] = []
        self.closed = False

    def enumerate_monitors(self) -> tuple[Monitor, ...]:
        return self.monitors

    def capture(self, monitor: Monitor) -> RawFrame:
        self.capture_calls.append(monitor.monitor_id)
        color = self._COLORS[monitor.monitor_id]
        return RawFrame(
            width=monitor.width,
            height=monitor.height,
            pixel_bytes=bytes(color) * (monitor.width * monitor.height),
            pixel_format="RGB",
            metadata={"fixture": "safe-injected", "audio": False, "video": False},
        )

    def close(self) -> None:
        self.closed = True


class _CaptureOnlyRuntime(CollectorRuntime):
    """Collector runtime with all non-capture sources replaced by no-op output."""

    def _source_events(self) -> list[Any]:
        return []


class _RecordingArtifactClient:
    """Offline server double that verifies uploaded bytes and retains batches."""

    def __init__(self) -> None:
        self.artifact_calls: list[Any] = []
        self.artifacts_by_sha: dict[str, bytes] = {}
        self.batch_calls: list[tuple[dict[str, Any], str | None]] = []

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        content = Path(artifact.path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        assert digest == artifact.sha256
        self.artifact_calls.append(artifact)
        self.artifacts_by_sha[artifact.sha256] = content
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        for reference in payload["artifacts"]:
            assert reference["sha256"] in self.artifacts_by_sha
        self.batch_calls.append((payload, batch_id))
        return ClientResponse(202, {"accepted": len(payload["events"])}, {}, 1)


def _assert_media_disabled(value: object) -> None:
    violations: list[str] = []

    def walk(item: object, path: str = "$") -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                child_path = f"{path}.{key}"
                if normalized in {"audio", "video"} and nested is not False:
                    violations.append(f"{child_path}={nested!r}")
                walk(nested, child_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                walk(nested, f"{path}[{index}]")

    walk(value)
    assert not violations, f"audio/video capture found at: {violations}"


def _decode_jpeg(content: bytes) -> Image.Image:
    image = Image.open(BytesIO(content))
    image.load()
    return image


def test_multimonitor_capture_artifacts_hash_deduplicate_and_retain_events(
    tmp_path: Path,
) -> None:
    backend = _SafeMultiMonitorBackend()
    timestamps = iter((CAPTURED_AT, CAPTURED_AT + timedelta(seconds=5)))
    capture = ScreenCaptureService(
        backend,
        config=CaptureConfig(
            interval_seconds=5,
            jpeg_quality=85,
            contact_sheet_columns=2,
            contact_sheet_max_tile_size=(4, 3),
            contact_sheet_padding=1,
            contact_sheet_label_height=2,
        ),
        now=lambda: next(timestamps),
        sequence_id_factory=iter(SEQUENCE_IDS).__next__,
    )
    client = _RecordingArtifactClient()
    spool_dir = tmp_path / "spool"
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
    runtime = _CaptureOnlyRuntime(
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
        capture=capture,
    )

    try:
        first_report = runtime.tick()
        second_report = runtime.tick()
    finally:
        runtime.close()

    assert backend.closed is True
    assert (
        backend.capture_calls
        == [
            "display-primary",
            "display-left",
            "display-above",
        ]
        * 2
    )
    assert first_report["monitor_count"] == 3
    assert second_report["monitor_count"] == 3
    assert first_report["event_count"] == 4
    assert second_report["event_count"] == 4

    status = transport.status()
    assert status["batches_enqueued"] == 2
    assert status["batches_uploaded"] == 2
    assert status["records_enqueued"] == 8
    assert status["records_uploaded"] == 8
    assert status["artifacts_stored"] == 4
    assert status["artifacts_deduplicated"] == 4
    assert status["artifacts_uploaded"] == 4
    assert status["pending_batches"] == 0
    assert status["pending_artifacts"] == 0
    assert status["uploaded_artifacts"] == 4
    assert len(list(transport.spool.artifact_blobs_dir.iterdir())) == 4

    assert len(client.artifact_calls) == 4
    artifacts_by_name = {artifact.metadata["name"]: artifact for artifact in client.artifact_calls}
    assert set(artifacts_by_name) == {
        "display-primary.jpg",
        "display-left.jpg",
        "display-above.jpg",
        "contact-sheet.jpg",
    }
    assert len({artifact.sha256 for artifact in client.artifact_calls}) == 4

    for monitor_id, expected_bounds in {
        "display-primary": (0, 0, 4, 3),
        "display-left": (-4, 0, 0, 3),
        "display-above": (0, -3, 4, 0),
    }.items():
        artifact = artifacts_by_name[f"{monitor_id}.jpg"]
        assert artifact.metadata == {
            "content_type": "image/jpeg",
            "retention_tier": "raw",
            "name": f"{monitor_id}.jpg",
            "monitor_id": monitor_id,
        }
        content = Path(artifact.path).read_bytes()
        assert content.startswith(b"\xff\xd8")
        assert artifact.sha256 == hashlib.sha256(content).hexdigest()
        image = _decode_jpeg(content)
        assert image.format == "JPEG"
        assert image.size == (4, 3)
        image.close()

        for payload, _batch_id in client.batch_calls:
            event = next(
                event for event in payload["events"] if event["source_id"] == f"screen:{monitor_id}"
            )
            assert event["payload"]["sha256"] == artifact.sha256
            monitor_metadata = event["payload"]["monitor"]
            assert (
                tuple(monitor_metadata[field] for field in ("left", "top", "right", "bottom"))
                == expected_bounds
            )
            reference = next(
                reference
                for reference in payload["artifacts"]
                if reference["name"] == f"{monitor_id}.jpg"
            )
            assert reference["sha256"] == artifact.sha256
            assert reference["metadata"] == {
                "monitor_id": monitor_id,
                "retention_tier": "raw",
            }

    contact_artifact = artifacts_by_name["contact-sheet.jpg"]
    assert contact_artifact.metadata == {
        "content_type": "image/jpeg",
        "retention_tier": "keyframe",
        "name": "contact-sheet.jpg",
        "monitor_id": None,
    }
    contact_content = Path(contact_artifact.path).read_bytes()
    assert contact_artifact.sha256 == hashlib.sha256(contact_content).hexdigest()
    contact_image = _decode_jpeg(contact_content)
    assert contact_image.format == "JPEG"
    assert contact_image.size == (11, 13)
    contact_image.close()

    all_event_ids = [
        event["event_id"]
        for payload, _batch_id in client.batch_calls
        for event in payload["events"]
    ]
    assert len(all_event_ids) == 8
    assert len(set(all_event_ids)) == 8
    assert [batch_id for _payload, batch_id in client.batch_calls] == list(SEQUENCE_IDS)
    assert all(
        sum(event["payload"].get("contact_sheet", False) is True for event in payload["events"])
        == 1
        for payload, _batch_id in client.batch_calls
    )
    assert all(
        len(
            [
                event
                for event in payload["events"]
                if event["source_id"].startswith("screen:display-")
            ]
        )
        == 3
        for payload, _batch_id in client.batch_calls
    )
    for payload, _batch_id in client.batch_calls:
        assert payload["metadata"]["audio"] is False
        assert payload["metadata"]["video"] is False
        assert all(reference["kind"] == "image" for reference in payload["artifacts"])
        assert all(reference["media_type"] == "image/jpeg" for reference in payload["artifacts"])
        _assert_media_disabled(payload)

    assert not list(spool_dir.rglob("*.wav"))
    assert not list(spool_dir.rglob("*.mp4"))


def test_multimonitor_capture_cadence_is_immediate_then_fixed_interval() -> None:
    backend = _SafeMultiMonitorBackend()
    monotonic_now = [0.0]
    sleeps: list[float] = []
    sequence_ids = iter(("cadence-001", "cadence-002", "cadence-003"))

    def monotonic() -> float:
        return monotonic_now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        monotonic_now[0] += seconds

    service = ScreenCaptureService(
        backend,
        config=CaptureConfig(interval_seconds=5, include_contact_sheet=False),
        now=lambda: CAPTURED_AT,
        monotonic=monotonic,
        sleep=sleep,
        sequence_id_factory=sequence_ids.__next__,
    )

    emitted_ids: list[str] = []
    emitted = service.run(
        on_capture=lambda sequence: emitted_ids.append(sequence.sequence_id),
        max_sequences=3,
    )

    assert emitted == 3
    assert emitted_ids == ["cadence-001", "cadence-002", "cadence-003"]
    assert sleeps == pytest.approx([5.0, 5.0])
    assert monotonic_now[0] == pytest.approx(10.0)
    assert (
        backend.capture_calls
        == [
            "display-primary",
            "display-left",
            "display-above",
        ]
        * 3
    )
    service.close()
    assert backend.closed is True
