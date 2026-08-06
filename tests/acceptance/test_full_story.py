"""Deterministic, ephemeral full-story acceptance coverage.

This module intentionally composes the server in the test instead of importing
the process-global application object.  The process-global object uses the
normal operator data directory, while this acceptance lane must never touch a
user archive.  Every server database, artifact, and collector spool therefore
lives below one ``TemporaryDirectory``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import uvicorn

from contracts import (
    ArtifactKind,
    ArtifactRef,
    CaptureEnvelope,
    SourceEvent,
    SourceEventType,
    SourceKind,
)
from services.collector.capture.models import Monitor, RawFrame
from services.collector.capture.service import CaptureConfig, ScreenCaptureService
from services.collector.transport.config import TransportConfig
from services.collector.transport.spool import LocalSpool, StoredArtifact
from services.collector.transport.transport import CollectorTransport
from services.context_server.api.routes import ingest, query
from services.context_server.app import create_app
from services.context_server.config import Settings
from services.context_server.inference import (
    DeterministicFakeModelClient,
    InferenceWorker,
    RetryPolicy,
    SQLitePersistentJobAdapter,
)
from services.context_server.inference.contracts import PromptBundle
from services.context_server.inference.runtime_worker import ArtifactResolvingModelClient
from services.context_server.query.interfaces import RouteServices
from services.context_server.runtime import ContextRuntime


# These are wire-level fields that would indicate a capture mode outside this
# MVP's scope.  The assertion is recursive so a prohibited field cannot hide
# inside event payload, metadata, query evidence, or an answer envelope.
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "audio",
        "audio_capture",
        "audio_bytes",
        "audio_path",
        "video",
        "video_capture",
        "video_bytes",
        "video_path",
        "keyboard",
        "key_event",
        "key_events",
        "key_log",
        "keylogging",
        "keystroke",
        "keystrokes",
        "raw_keys",
    }
)


def _assert_no_forbidden_fields(value: Any) -> None:
    """Fail if a wire payload contains audio/video/keylogging field names."""

    def walk(item: Any, path: str = "$") -> Iterator[tuple[str, str]]:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                key_text = str(key)
                normalized = key_text.casefold().replace("-", "_")
                if normalized in _FORBIDDEN_FIELD_NAMES:
                    yield path, key_text
                yield from walk(nested, f"{path}.{key_text}")
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                yield from walk(nested, f"{path}[{index}]")

    violations = list(walk(value))
    assert not violations, f"prohibited capture fields found: {violations}"


def _http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    expected_status: int | tuple[int, ...] = 200,
) -> tuple[int, bytes, Mapping[str, str]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = int(response.status)
            response_body = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as error:
        response_body = error.read()
        status = int(error.code)
        response_headers = dict(error.headers.items()) if error.headers else {}

    accepted = (expected_status,) if isinstance(expected_status, int) else expected_status
    assert status in accepted, (
        f"{method} {path} returned {status}, expected {accepted}: "
        f"{response_body.decode('utf-8', errors='replace')}"
    )
    return status, response_body, response_headers


def _http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    expected_status: int | tuple[int, ...] = 200,
) -> dict[str, Any]:
    _, body, _ = _http_request(
        base_url,
        method,
        path,
        payload=payload,
        expected_status=expected_status,
    )
    decoded = json.loads(body.decode("utf-8")) if body else {}
    assert isinstance(decoded, dict), f"expected an object response, got {type(decoded)!r}"
    return decoded


class _InProcessUvicorn:
    """Run one FastAPI app on an ephemeral localhost port in a test thread."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.server: uvicorn.Server | None = None
        self.thread: Thread | None = None
        self.error: BaseException | None = None
        self.base_url = ""

    def __enter__(self) -> "_InProcessUvicorn":
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            lifespan="on",
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)

        def run() -> None:
            try:
                assert self.server is not None
                self.server.run()
            except BaseException as error:  # pragma: no cover - startup failure path
                self.error = error

        self.thread = Thread(target=run, name="life-context-acceptance-server", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.server is not None and not self.server.started:
            if self.error is not None:
                raise AssertionError("in-process uvicorn failed to start") from self.error
            if self.thread is not None and not self.thread.is_alive():
                raise AssertionError("in-process uvicorn exited before startup")
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for in-process uvicorn startup")
            time.sleep(0.01)

        assert self.server is not None
        sockets = list(self.server.servers or [])
        assert sockets and sockets[0].sockets, "uvicorn did not expose a bound socket"
        port = int(sockets[0].sockets[0].getsockname()[1])
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)
            assert not self.thread.is_alive(), "in-process uvicorn did not shut down"
        if self.error is not None and exc_value is None:
            raise AssertionError("in-process uvicorn failed") from self.error


@dataclass(frozen=True, slots=True)
class _FakeMonitorBackend:
    """Deterministic two-monitor backend used by the capture service."""

    tick: int

    @property
    def name(self) -> str:
        return "acceptance-fake-multi-monitor"

    def enumerate_monitors(self) -> tuple[Monitor, ...]:
        return (
            Monitor(
                monitor_id="fake-monitor-1",
                name="Fake Monitor 1",
                left=0,
                top=0,
                width=48,
                height=32,
                is_primary=True,
            ),
            Monitor(
                monitor_id="fake-monitor-2",
                name="Fake Monitor 2",
                left=48,
                top=0,
                width=48,
                height=32,
            ),
        )

    def capture(self, monitor: Monitor) -> RawFrame:
        monitor_index = 0 if monitor.monitor_id.endswith("1") else 1
        width, height = monitor.width, monitor.height
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels.extend(
                    (
                        (self.tick * 29 + monitor_index * 71 + x * 3) % 256,
                        (self.tick * 47 + monitor_index * 37 + y * 5) % 256,
                        (self.tick * 61 + monitor_index * 19 + x + y) % 256,
                    )
                )
        return RawFrame(
            width=width,
            height=height,
            pixel_bytes=bytes(pixels),
            pixel_format="RGB",
            metadata={"monitor_id": monitor.monitor_id, "fake_tick": self.tick},
        )

    def close(self) -> None:
        return None


class _FakeMultiMonitorCapture:
    """Produce ten five-second capture sequences without reading the desktop."""

    def __init__(self, start: datetime) -> None:
        self.start = start

    def sequences(self) -> tuple[Any, ...]:
        result = []
        for tick in range(10):
            captured_at = self.start + timedelta(seconds=tick * 5)
            service = ScreenCaptureService(
                backend=_FakeMonitorBackend(tick),
                config=CaptureConfig(
                    interval_seconds=5,
                    jpeg_quality=80,
                    contact_sheet_max_tile_size=(48, 32),
                    contact_sheet_padding=3,
                    contact_sheet_label_height=10,
                ),
                now=lambda captured_at=captured_at: captured_at,
                sequence_id_factory=lambda tick=tick: f"acceptance-sequence-{tick:02d}",
            )
            result.append(service.capture_once())
        return tuple(result)


class _FakeCaptureTransport:
    """Test-owned transport wrapper using only an isolated spool and localhost."""

    def __init__(self, base_url: str, spool_root: Path) -> None:
        config = TransportConfig(
            server_url=base_url,
            spool_dir=spool_root,
            ingest_path="/v1/ingest/batches",
            artifact_path="/v1/artifacts/{sha256}",
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        )
        self.inner = CollectorTransport(
            config,
            spool=LocalSpool(spool_root, fsync=False),
        )
        self.staged_artifacts: list[StoredArtifact] = []

    def stage_artifact(self, data: bytes, *, label: str) -> StoredArtifact:
        artifact = self.inner.enqueue_artifact(
            data,
            metadata={
                "content_type": (
                    "image/jpeg" if label.endswith(".jpg") else "application/octet-stream"
                ),
                "retention_tier": "raw",
                "evidence_label": label,
            },
        )
        self.staged_artifacts.append(artifact)
        return artifact

    def deliver(self, envelope: Mapping[str, Any], *, batch_id: str) -> dict[str, Any]:
        self.inner.enqueue_batch(
            {"records": [dict(envelope)]},
            batch_id=batch_id,
        )
        return self.inner.replay(raise_on_error=True).as_dict()

    def replay_duplicate(self, envelope: Mapping[str, Any], *, batch_id: str) -> Any:
        return self.inner.client.ingest_batch(
            {"records": [dict(envelope)]},
            batch_id=batch_id,
        )


class _InspectingFakeVLM(DeterministicFakeModelClient):
    """Scripted VLM that records the resolved multimodal request blocks."""

    def __init__(self, output: Mapping[str, Any]) -> None:
        super().__init__(
            [output],
            model_name="acceptance-fake-vlm",
            returned_model="acceptance-fake-vlm",
            endpoint="fake://acceptance-vlm",
            model_revision="acceptance-revision-001",
        )
        self.resolved_user_content: list[tuple[Mapping[str, Any], ...]] = []

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        if isinstance(messages, PromptBundle) and messages.user_content is not None:
            self.resolved_user_content.append(tuple(messages.user_content))
        return super().complete(messages, **kwargs)


def _build_envelope(
    sequences: tuple[Any, ...],
    transport: _FakeCaptureTransport,
) -> tuple[CaptureEnvelope, list[ArtifactRef], str]:
    events: list[SourceEvent] = []
    all_refs: list[ArtifactRef] = []
    queried_contact_sheet_bytes = b""

    for index, sequence in enumerate(sequences):
        refs: list[ArtifactRef] = []
        assert sequence.contact_sheet is not None
        contact_sheet = sequence.contact_sheet
        if index == min(7, len(sequences) - 1):
            queried_contact_sheet_bytes = contact_sheet.jpeg_bytes
        stored_contact = transport.stage_artifact(
            contact_sheet.jpeg_bytes,
            label=f"{sequence.sequence_id}-contact-sheet.jpg",
        )
        refs.append(
            ArtifactRef(
                artifact_id=stored_contact.sha256,
                kind=ArtifactKind.IMAGE,
                media_type="image/jpeg",
                size_bytes=stored_contact.size,
                sha256=stored_contact.sha256,
                metadata={"role": "contact-sheet", "sequence_id": sequence.sequence_id},
            )
        )
        for monitor_capture in sequence.monitors:
            stored_monitor = transport.stage_artifact(
                monitor_capture.jpeg_bytes,
                label=f"{sequence.sequence_id}-{monitor_capture.monitor.monitor_id}.jpg",
            )
            refs.append(
                ArtifactRef(
                    artifact_id=stored_monitor.sha256,
                    kind=ArtifactKind.IMAGE,
                    media_type="image/jpeg",
                    size_bytes=stored_monitor.size,
                    sha256=stored_monitor.sha256,
                    metadata={
                        "role": "monitor-frame",
                        "sequence_id": sequence.sequence_id,
                        "monitor_id": monitor_capture.monitor.monitor_id,
                    },
                )
            )

        all_refs.extend(refs)
        events.append(
            SourceEvent(
                event_id=f"acceptance-event-{index:02d}",
                source_id="screen:acceptance-machine",
                source_kind=SourceKind.APPLICATION,
                event_type=SourceEventType.OBSERVED,
                occurred_at=sequence.captured_at,
                captured_at=sequence.captured_at,
                payload={
                    "text": f"life-context coding activity-{index:02d}",
                    "window_title": "VS Code - life-context",
                    "project": "life-context",
                    "monitor_count": len(sequence.monitors),
                    "monitor_ids": [item.monitor.monitor_id for item in sequence.monitors],
                    "capture_sequence": sequence.sequence_id,
                },
                artifacts=refs,
                metadata={
                    "capture_mode": "screenshots-only",
                    "monitor_count": len(sequence.monitors),
                    "routing": {"screenshots": "local"},
                },
            )
        )

    envelope = CaptureEnvelope(
        envelope_id="acceptance-envelope-001",
        captured_at=sequences[-1].captured_at,
        source_kind=SourceKind.APPLICATION,
        events=events,
        producer="acceptance-harness",
        metadata={
            "capture_cadence_seconds": 5,
            "monitor_count": 2,
            "capture_mode": "screenshots-only",
        },
    )
    return envelope, all_refs, queried_contact_sheet_bytes


def _build_test_app(data_dir: Path) -> tuple[Any, ContextRuntime]:
    settings = Settings(
        host="127.0.0.1",
        port=8000,
        api_prefix="/v1",
        docs_enabled=False,
        json_logs=False,
        data_dir=data_dir,
        capture_interval_seconds=5,
        worker_count=1,
        queue_size=100,
        raw_retention_hours=0,
        keyframe_retention_days=0,
    )
    runtime = ContextRuntime(settings)
    services = RouteServices(
        query=runtime.search,
        timeline=runtime,
        observation=runtime,
        status=runtime,
        lifecycle=runtime,
        purge=runtime,
        config=runtime,
        events=runtime.events,
    )
    app = create_app(
        settings,
        routers=(query.router, ingest.router),
        startup_hooks=(runtime.startup,),
        shutdown_hooks=(runtime.shutdown,),
    )
    app.state.context_runtime = runtime
    app.state.context_services = services
    return app, runtime


def test_full_story_acceptance_harness_is_ephemeral() -> None:
    """Verify fake capture -> transport -> API -> storage -> query -> purge."""

    with TemporaryDirectory(prefix="life-context-acceptance-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"
        spool_dir = root / "collector-spool"
        app, runtime = _build_test_app(data_dir)

        # This explicit path check is part of the safety contract: the
        # acceptance harness may only create files below its temporary root.
        assert data_dir.is_relative_to(root)
        assert spool_dir.is_relative_to(root)
        assert data_dir != Path(r"E:\LifeContext\data").resolve()
        assert Path(runtime.storage.database).resolve().is_relative_to(root)
        assert runtime.storage.artifacts.root.is_relative_to(root)

        with _InProcessUvicorn(app) as server:
            health = _http_json(server.base_url, "GET", "/v1/health")
            assert health["status"] == "ok"
            assert health["ready"] is True

            capabilities = _http_json(server.base_url, "GET", "/v1/capabilities")
            assert "capture-envelopes" in capabilities["capabilities"]
            _assert_no_forbidden_fields(capabilities)

            fake_capture = _FakeMultiMonitorCapture(
                datetime.now(timezone.utc) - timedelta(minutes=2)
            )
            sequences = fake_capture.sequences()
            assert len(sequences) == 10
            assert all(len(sequence.monitors) == 2 for sequence in sequences)
            timestamps = [sequence.captured_at for sequence in sequences]
            assert [
                int((right - left).total_seconds())
                for left, right in zip(timestamps, timestamps[1:])
            ] == [5] * 9

            transport = _FakeCaptureTransport(server.base_url, spool_dir)
            envelope, refs, evidence_bytes = _build_envelope(sequences, transport)
            envelope_dict = envelope.to_dict()
            _assert_no_forbidden_fields(envelope_dict)
            assert len(envelope.events) == 10
            assert all(len(event.artifacts) == 3 for event in envelope.events)
            assert len({event.event_id for event in envelope.events}) == 10
            assert len(refs) == 30
            assert len({ref.sha256 for ref in refs}) == 30

            # One unreferenced raw artifact gives the purge assertion a
            # directly removable artifact while capture evidence remains
            # reference-safe in the database.
            retention_probe = transport.stage_artifact(
                b"acceptance-retention-probe", label="probe.bin"
            )
            report = transport.deliver(envelope_dict, batch_id="acceptance-batch-001")
            assert report["ok"] is True
            assert report["batches"] == {"attempted": 1, "uploaded": 1, "failed": 0}
            assert report["artifacts"] == {"attempted": 31, "uploaded": 31, "failed": 0}
            assert transport.inner.spool.list_pending_batches() == []
            assert transport.inner.spool.list_pending_artifacts() == []

            status = _http_json(server.base_url, "GET", "/v1/status")
            assert status["event_count"] == 10
            assert status["artifact_count"] == 31
            assert status["queue"]["pending"] == 10
            _assert_no_forbidden_fields(status)

            timeline = _http_json(
                server.base_url,
                "GET",
                f"/v1/timeline?{urlencode({'limit': 20})}",
            )
            assert timeline["total_count"] == 10
            assert len(timeline["items"]) == 10
            assert all(item["captured_at"] for item in timeline["items"])

            query_response = _http_json(
                server.base_url,
                "POST",
                "/v1/query",
                payload={
                    "query": "activity-07",
                    "limit": 5,
                    "include_evidence": True,
                },
            )
            assert len(query_response["results"]) == 1
            assert query_response["answer"]["evidence_ids"] == [query_response["results"][0]["id"]]
            assert "activity-07" in query_response["answer_text"]
            evidence = query_response["evidence"]
            assert len(evidence) == 1
            artifact_url = evidence[0]["metadata"]["artifact_url"]
            assert artifact_url.startswith("/v1/artifacts/")
            _, downloaded, _ = _http_request(server.base_url, "GET", artifact_url)
            assert downloaded == evidence_bytes
            _assert_no_forbidden_fields(query_response)

            # Replay the same idempotency key and full payload after the
            # durable spool has been acknowledged.  The HTTP path is still
            # exercised, but storage must remain at exactly ten observations.
            duplicate = transport.replay_duplicate(
                envelope_dict,
                batch_id="acceptance-batch-001",
            )
            assert duplicate.status_code == 202
            assert duplicate.body["accepted"] == 10
            status_after_duplicate = _http_json(server.base_url, "GET", "/v1/status")
            assert status_after_duplicate["event_count"] == 10
            assert status_after_duplicate["artifact_count"] == 31

            purge = _http_json(server.base_url, "POST", "/v1/purge", payload={})
            assert purge["expired_observations"] == 10
            assert purge["deleted_observations"] == 10
            assert purge["expired_artifacts"] == 31
            assert purge["deleted_artifacts"] == 1
            assert purge["skipped_referenced_artifacts"] == 30
            _assert_no_forbidden_fields(purge)

            after_purge = _http_json(
                server.base_url,
                "POST",
                "/v1/query",
                payload={"query": "activity-07", "limit": 5},
            )
            assert after_purge["results"] == []
            assert after_purge["answer"]["status"] == "no_results"
            _, _, _ = _http_request(
                server.base_url,
                "GET",
                f"/v1/artifacts/{retention_probe.sha256}",
                expected_status=404,
            )
            _assert_no_forbidden_fields(after_purge)

        # The server has closed its database before this check.  All material
        # that did exist was rooted in the temporary directory and is removed
        # by TemporaryDirectory on exit; no default archive or user path was
        # eligible for writes.
        assert runtime.data_dir.is_relative_to(root)


def test_full_story_capture_to_fake_vlm_query_evidence() -> None:
    """Exercise capture, HTTP ingest, fake VLM completion, and evidence query."""

    with TemporaryDirectory(prefix="life-context-cross-service-") as temporary:
        root = Path(temporary).resolve()
        app, runtime = _build_test_app(root / "server-data")

        with _InProcessUvicorn(app) as server:
            capture = _FakeMultiMonitorCapture(datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
            sequences = capture.sequences()[:1]
            transport = _FakeCaptureTransport(server.base_url, root / "collector-spool")
            envelope, refs, evidence_bytes = _build_envelope(sequences, transport)

            delivery = transport.deliver(
                envelope.to_dict(),
                batch_id="cross-service-batch-001",
            )
            assert delivery["ok"] is True
            assert delivery["artifacts"]["uploaded"] == 3
            assert delivery["batches"]["uploaded"] == 1

            fake_model = _InspectingFakeVLM(
                {
                    "summary": "life-context coding activity-00 fake-vlm marker",
                    "activity": "coding",
                    "project": "life-context",
                    "application": "VS Code",
                    "confidence": 0.91,
                    "tags": ["coding", "life-context"],
                }
            )
            adapter = SQLitePersistentJobAdapter(
                runtime.storage,
                worker_id="cross-service-fake-vlm",
                on_complete=runtime.inference._on_complete,
            )
            worker = InferenceWorker(
                adapter,
                {
                    "default": ArtifactResolvingModelClient(
                        fake_model,
                        runtime.storage.artifacts,
                    )
                },
                default_model="default",
                batch_size=1,
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    base_delay_s=0,
                    max_delay_s=0,
                ),
                idle_sleep_s=0,
            )
            run = worker.run_once()
            assert run.claimed == 1
            assert run.succeeded == 1
            assert run.retried == 0
            assert run.dead_lettered == 0
            assert len(fake_model.calls) == 1

            # The persisted job carries artifact:// references; the actual
            # model boundary must receive local data URLs, never a filesystem
            # path or an unresolved artifact URI.
            assert len(fake_model.resolved_user_content) == 1
            image_blocks = [
                block
                for block in fake_model.resolved_user_content[0]
                if isinstance(block.get("image_url"), Mapping)
            ]
            assert len(image_blocks) == 3
            assert all(
                str(block["image_url"]["url"]).startswith("data:image/jpeg;base64,")
                for block in image_blocks
            )
            assert all(
                "artifact://" not in str(block["image_url"]["url"]) for block in image_blocks
            )

            status = _http_json(server.base_url, "GET", "/v1/status")
            assert status["queue"]["pending"] == 0
            assert status["queue"]["completed"] == 1

            query_response = _http_json(
                server.base_url,
                "POST",
                "/v1/query",
                payload={
                    "query": "activity-00",
                    "limit": 5,
                    "include_evidence": True,
                    "evidence_limit": 5,
                },
            )
            results = query_response["results"]
            result = next(item for item in results if item["id"].startswith("inference:"))
            source_result = next(item for item in results if item["id"].startswith("observation:"))
            assert result["text"] == "life-context coding activity-00 fake-vlm marker"
            evidence = query_response["evidence"]
            evidence_by_id = {item["id"]: item for item in evidence}
            assert result["id"] in evidence_by_id
            assert source_result["id"] in evidence_by_id
            assert set(query_response["answer"]["evidence_ids"]) == set(evidence_by_id)
            assert evidence_by_id[result["id"]]["citation"]
            artifact_url = evidence_by_id[source_result["id"]]["metadata"]["artifact_url"]
            _, downloaded, _ = _http_request(server.base_url, "GET", artifact_url)
            assert downloaded == evidence_bytes

            observation = runtime.storage.get_observation(result["id"])
            assert observation is not None
            assert observation.metadata["validation_status"] == "valid"
            assert observation.metadata["inference"]["model"]["model_revision"] == (
                "acceptance-revision-001"
            )
            assert observation.metadata["prompt_revision"] == "life-context-observation-v1"
            assert observation.metadata["confidence"] == 0.91
            artifact_row = runtime.storage.connection.execute(
                "SELECT primary_artifact_digest FROM observations WHERE id = ?",
                (result["id"],),
            ).fetchone()
            assert artifact_row["primary_artifact_digest"] == refs[0].sha256

            _assert_no_forbidden_fields(query_response)
            _assert_no_forbidden_fields(status)

        assert runtime.data_dir.is_relative_to(root)
