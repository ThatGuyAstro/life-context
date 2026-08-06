"""Focused remote self-hosted acceptance coverage for collector delivery."""

from __future__ import annotations

import json
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from contracts import (
    ArtifactKind,
    ArtifactRef,
    CaptureEnvelope,
    SourceEvent,
    SourceEventType,
    SourceKind,
)
from fastapi.testclient import TestClient

from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.transport.client import JsonBatchClient
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import HttpTransportError
from services.collector.transport.spool import LocalSpool
from services.collector.transport.transport import CollectorTransport
from services.context_server.api.routes import ingest, query
from services.context_server.app import create_app
from services.context_server.config import Settings
from services.context_server.query.interfaces import RouteServices
from services.context_server.runtime import ContextRuntime

REMOTE_HOST = "remote.example.test"
REMOTE_BASE_URL = f"https://{REMOTE_HOST}"
REMOTE_TOKEN = "remote-acceptance-token"
AUTH_HEADERS = {"Authorization": f"Bearer {REMOTE_TOKEN}"}
ARTIFACT_BYTES = b"remote self-hosted artifact retrieval probe\x00"
QUERY_MARKER = "remote-self-hosted-query-marker"
CLIPBOARD_MARKER = "clipboard-must-remain-local"
BATCH_ID = "remote-self-hosted-acceptance-batch"


class _AdapterResponse:
    """urllib-compatible response facade over one TestClient response."""

    def __init__(self, response: Any) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self._response = response

    def read(self) -> bytes:
        return self._response.content

    def close(self) -> None:
        self._response.close()


class _TestClientOpener:
    """Drive the real urllib transport client through an HTTPS-scheme TestClient."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, **kwargs: object) -> _AdapterResponse:
        body = request.data
        if isinstance(body, (bytearray, memoryview)):
            body = bytes(body)
        headers = dict(request.header_items())
        request_kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            request_kwargs["content"] = body
        response = self.client.request(
            request.get_method(),
            request.full_url,
            **request_kwargs,
        )
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": headers,
                "body": body,
                "context": kwargs.get("context"),
                "status": response.status_code,
            }
        )
        return _AdapterResponse(response)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _build_remote_app(data_dir: Path) -> Any:
    settings = Settings(
        host=REMOTE_HOST,
        api_prefix="/v1",
        docs_enabled=False,
        json_logs=False,
        data_dir=data_dir,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
        model_endpoint="",
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
    return app


def _transport_config(spool_dir: Path, *, bearer_token: str | None) -> TransportConfig:
    return TransportConfig(
        server_url=REMOTE_BASE_URL,
        spool_dir=spool_dir,
        ingest_path="/v1/ingest/batches",
        artifact_path="/v1/artifacts/{sha256}",
        bearer_token=bearer_token,
        max_attempts=1,
        backoff_initial_seconds=0,
        backoff_max_seconds=0,
        fsync=False,
    )


def test_remote_self_hosted_transport_and_artifact_probe() -> None:
    """Verify authenticated HTTPS-compatible delivery and local-only clipboard routing."""

    with TemporaryDirectory(prefix="life-context-remote-acceptance-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"
        spool_dir = root / "collector-spool"
        app = _build_remote_app(data_dir)

        with TestClient(app, base_url=REMOTE_BASE_URL) as remote:
            opener = _TestClientOpener(remote)
            config = _transport_config(spool_dir, bearer_token=REMOTE_TOKEN)
            client = JsonBatchClient(config, opener=opener)
            transport = CollectorTransport(
                config,
                spool=LocalSpool(spool_dir, fsync=False),
                client=client,
            )

            unauthenticated = JsonBatchClient(
                _transport_config(spool_dir, bearer_token=None),
                opener=opener,
            )
            with pytest.raises(HttpTransportError) as auth_error:
                unauthenticated.ingest_batch({"records": []}, batch_id="missing-bearer")
            assert auth_error.value.status_code == 401
            assert _header(opener.calls[-1]["headers"], "Authorization") is None

            artifact = transport.enqueue_artifact(
                ARTIFACT_BYTES,
                metadata={
                    "content_type": "application/octet-stream",
                    "retention_tier": "raw",
                },
            )
            artifact_ref = ArtifactRef(
                artifact_id=artifact.sha256,
                kind=ArtifactKind.TEXT,
                uri=f"/v1/artifacts/{artifact.sha256}",
                name="remote-acceptance.bin",
                media_type="application/octet-stream",
                size_bytes=artifact.size,
                sha256=artifact.sha256,
            )
            captured_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            screen_event = SourceEvent(
                event_id="remote-acceptance-screen-event",
                source_id="screen:remote-acceptance",
                source_kind=SourceKind.APPLICATION,
                event_type=SourceEventType.OBSERVED,
                occurred_at=captured_at,
                captured_at=captured_at,
                payload={"text": QUERY_MARKER},
                artifacts=[artifact_ref],
                metadata={"routing": {"screenshots": "remote"}},
            )
            clipboard_event = SourceEvent(
                event_id="remote-acceptance-clipboard-event",
                source_id="clipboard:local",
                source_kind=SourceKind.MANUAL,
                event_type=SourceEventType.OBSERVED,
                occurred_at=captured_at,
                captured_at=captured_at,
                payload={"raw_text": CLIPBOARD_MARKER},
                metadata={"remote_route": "explicit_opt_in", "routing": {"route": "local"}},
            )

            collector = CollectorRuntime(
                config=CollectorConfig(
                    roots=(),
                    clipboard_enabled=True,
                    browser_enabled=False,
                    include_git_history=False,
                    include_processes=False,
                    spool_dir=spool_dir,
                ),
                transport=transport,
                capture=object(),
            )
            assert collector.server_is_remote is True
            assert collector.config.remote_clipboard is False
            safe_events = collector._remote_safe_events(
                [screen_event, clipboard_event],
                "remote-acceptance-envelope",
            )
            assert [event.source_id for event in safe_events] == [screen_event.source_id]
            local_only_batches = collector.local_only_spool.list_pending_batches()
            assert len(local_only_batches) == 1
            assert local_only_batches[0].payload["events"][0]["source_id"] == "clipboard:local"
            assert (
                local_only_batches[0].payload["events"][0]["payload"]["raw_text"]
                == CLIPBOARD_MARKER
            )

            envelope = CaptureEnvelope(
                envelope_id="remote-acceptance-envelope",
                captured_at=captured_at,
                source_kind=SourceKind.APPLICATION,
                events=safe_events,
                producer="remote-self-hosted-acceptance",
                metadata={"clipboard_local_only": True},
            )
            payload = {"records": [envelope.to_dict()]}
            transport.enqueue_batch(payload, batch_id=BATCH_ID)
            assert len(transport.spool.list_pending_batches()) == 1

            replay = transport.replay(raise_on_error=True)
            assert replay.ok is True
            assert replay.artifacts_attempted == 1
            assert replay.artifacts_uploaded == 1
            assert replay.artifacts_failed == 0
            assert replay.batches_attempted == 1
            assert replay.batches_uploaded == 1
            assert replay.batches_failed == 0
            assert transport.spool.list_pending_artifacts() == []
            assert transport.spool.list_pending_batches() == []

            duplicate_artifact = client.upload_artifact(artifact)
            assert duplicate_artifact.ok is True
            assert duplicate_artifact.body["sha256"] == artifact.sha256

            batch_call = next(
                call
                for call in opener.calls
                if call["method"] == "POST"
                and call["url"].endswith("/v1/ingest/batches")
                and isinstance(call["body"], bytes)
                and QUERY_MARKER.encode("utf-8") in call["body"]
            )
            assert _header(batch_call["headers"], "Authorization") == AUTH_HEADERS["Authorization"]
            assert _header(batch_call["headers"], "Idempotency-Key") == BATCH_ID
            remote_payload_text = json.dumps(
                json.loads(batch_call["body"].decode("utf-8")),
                ensure_ascii=False,
            )
            assert "clipboard:local" not in remote_payload_text
            assert CLIPBOARD_MARKER not in remote_payload_text

            assert config.is_https is True
            assert all(isinstance(call["context"], ssl.SSLContext) for call in opener.calls)

            unauthorized_artifact = remote.get(f"/v1/artifacts/{artifact.sha256}")
            assert unauthorized_artifact.status_code == 401
            artifact_response = remote.get(
                f"/v1/artifacts/{artifact.sha256}",
                headers=AUTH_HEADERS,
            )
            assert artifact_response.status_code == 200
            assert artifact_response.content == ARTIFACT_BYTES

            query_response = remote.post(
                "/v1/query",
                headers=AUTH_HEADERS,
                json={"query": QUERY_MARKER, "mode": "fts", "limit": 5},
            )
            assert query_response.status_code == 200
            query_payload = query_response.json()
            assert query_payload["meta"]["result_count"] == 1
            assert query_payload["answer"]["evidence_ids"] == [query_payload["results"][0]["id"]]
            evidence_url = query_payload["evidence"][0]["metadata"]["artifact_url"]
            assert evidence_url == f"/v1/artifacts/{artifact.sha256}"
            evidence_artifact = remote.get(evidence_url, headers=AUTH_HEADERS)
            assert evidence_artifact.status_code == 200
            assert evidence_artifact.content == ARTIFACT_BYTES

            clipboard_query = remote.post(
                "/v1/query",
                headers=AUTH_HEADERS,
                json={"query": CLIPBOARD_MARKER, "mode": "fts", "limit": 5},
            )
            assert clipboard_query.status_code == 200
            assert clipboard_query.json()["results"] == []

            duplicate_ingest = client.ingest_batch(payload, batch_id=BATCH_ID)
            assert duplicate_ingest.status_code == 202
            status = remote.get("/v1/status", headers=AUTH_HEADERS)
            assert status.status_code == 200
            assert status.json()["event_count"] == 1
            assert status.json()["artifact_count"] == 1
