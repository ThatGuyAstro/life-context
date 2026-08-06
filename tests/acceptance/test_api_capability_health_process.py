"""Real-uvicorn acceptance coverage for the public API diagnostics surface.

The probe deliberately uses temporary storage and no model process.  The model
availability cases use a loopback HTTP responder that serves only health and
model-list responses; no completion is generated and no GPU is involved.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from typing import Any

_PROCESS_HELPER_SPEC = importlib.util.spec_from_file_location(
    "life_context_api_capability_health_process_helper",
    Path(__file__).with_name("test_server_process_lifecycle.py"),
)
assert _PROCESS_HELPER_SPEC is not None and _PROCESS_HELPER_SPEC.loader is not None
_PROCESS_HELPER_MODULE = importlib.util.module_from_spec(_PROCESS_HELPER_SPEC)
_PROCESS_HELPER_SPEC.loader.exec_module(_PROCESS_HELPER_MODULE)

_RealUvicornProcess = _PROCESS_HELPER_MODULE._RealUvicornProcess
_http_json = _PROCESS_HELPER_MODULE._http_json
_http_request = _PROCESS_HELPER_MODULE._http_request

_AUTH_TOKEN = "api-capability-health-acceptance-token"
_MODEL_NAME = "api-capability-health-probe-model"
_MODEL_REVISION = "api-capability-health-probe-revision"
_QUEUE_MARKER = "api-capability-health-queue-marker"


class _ProbeEndpoint:
    """Serve deterministic model diagnostics without serving model output."""

    def __init__(self, mode: str) -> None:
        if mode not in {"unavailable", "degraded"}:
            raise ValueError(f"unsupported probe mode: {mode}")
        self.mode = mode
        self._lock = Lock()
        self.requests: list[tuple[str, str]] = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name=f"{mode}-model-probe")

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> _ProbeEndpoint:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        assert not self._thread.is_alive(), "model probe endpoint did not stop"

    def request_snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.requests)

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method: str) -> None:
                with owner._lock:
                    owner.requests.append((method, self.path))

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
                self._record("GET")
                if owner.mode == "unavailable":
                    self._send_json(404, {"error": "model service unavailable"})
                    return
                if self.path == "/health":
                    self._send_json(200, {"status": "degraded", "ready": False})
                    return
                if self.path == "/v1/models":
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "id": _MODEL_NAME,
                                    "capabilities": {
                                        "supports_chat": True,
                                        "supports_json_schema": False,
                                    },
                                }
                            ]
                        },
                    )
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
                self._record("POST")
                self._send_json(500, {"error": "completion endpoint is intentionally disabled"})

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler


class _ApiProbeUvicornProcess(_RealUvicornProcess):
    """Real uvicorn process with explicit, test-only environment settings."""

    def __init__(
        self,
        repository_root: Path,
        data_dir: Path,
        *,
        model_endpoint: str = "",
        server_host: str = "127.0.0.1",
        auth_token: str = "",
        require_https_remote: bool = False,
    ) -> None:
        super().__init__(repository_root, data_dir)
        self.model_endpoint = model_endpoint
        self.server_host = server_host
        self.auth_token = auth_token
        self.require_https_remote = require_https_remote

    def _environment(self) -> dict[str, str]:
        environment = super()._environment()
        environment.update(
            {
                "CONTEXT_SERVER_HOST": self.server_host,
                "CONTEXT_SERVER_AUTH_TOKEN": self.auth_token,
                "CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE": str(self.require_https_remote).lower(),
                "CONTEXT_SERVER_MODEL_ENDPOINT": self.model_endpoint,
                "CONTEXT_SERVER_MODEL_NAME": _MODEL_NAME if self.model_endpoint else "",
                "CONTEXT_SERVER_MODEL_REVISION": _MODEL_REVISION if self.model_endpoint else "",
                "CONTEXT_MODEL_API_KEY": "",
            }
        )
        return environment


def _queue_probe_envelope() -> dict[str, Any]:
    return {
        "envelope_id": "api-capability-health-queue-envelope",
        "schema_version": "1",
        "captured_at": "2026-08-05T12:00:00Z",
        "source_kind": "application",
        "producer": "api-capability-health-acceptance",
        "events": [
            {
                "event_id": "api-capability-health-queue-event",
                "source_id": "api-capability-health-queue-source",
                "source_kind": "application",
                "event_type": "observed",
                "occurred_at": "2026-08-05T12:00:00Z",
                "captured_at": "2026-08-05T12:00:00Z",
                "payload": {"text": _QUEUE_MARKER},
                "artifacts": [],
                "metadata": {},
            }
        ],
        "artifacts": [],
        "metadata": {"model_free": True},
    }


def _age_pending_job(data_dir: Path, *, seconds_old: int = 120) -> str:
    old = datetime.now(UTC) - timedelta(seconds=seconds_old)
    timestamp = old.isoformat(timespec="microseconds")
    connection = sqlite3.connect(data_dir / "context.sqlite3", timeout=5)
    try:
        row = connection.execute(
            "SELECT id, status FROM jobs ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[1] == "pending"
        connection.execute(
            """
            UPDATE jobs
            SET created_at = ?, updated_at = ?, available_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (timestamp, timestamp, timestamp, row[0]),
        )
        assert connection.total_changes == 1
        connection.commit()
    finally:
        connection.close()
    return timestamp


def test_api_capability_health_status_queue_and_artifact_probe_real_uvicorn() -> None:
    """Probe diagnostics, model-free queue aging, and artifact status at the socket."""

    repository_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="life-context-api-probe-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"

        with _RealUvicornProcess(repository_root, data_dir) as server:
            health = _http_json(server.base_url, "GET", "/v1/health")
            assert health == {
                "status": "ok",
                "service": "context-server",
                "version": "0.1.0",
                "ready": True,
            }

            capabilities = _http_json(server.base_url, "GET", "/v1/capabilities")
            assert capabilities["status"] == "ok"
            assert {
                "capture-envelopes",
                "content-addressed-artifacts",
                "sqlite-fts5",
                "hybrid-query",
                "sse-events",
                "websocket-events",
                "plaintext-local-archive",
            }.issubset(capabilities["capabilities"])

            initial_status = _http_json(server.base_url, "GET", "/v1/status")
            assert initial_status["status"] == "running"
            assert initial_status["ready"] is True
            assert initial_status["model"] == {
                "endpoint": None,
                "name": None,
                "revision": None,
                "configured": False,
                "status": "not_configured",
                "worker_status": "not_configured",
                "model_status": "not_configured",
                "availability": "not_configured",
                "health": None,
                "capabilities": None,
                "model": None,
                "model_revision": None,
                "structured_output_mode": None,
                "configured_structured_output_mode": None,
                "worker_count": 1,
                "active_workers": 0,
                "last_error": None,
                "started_at": None,
                "processed": 0,
            }

            storage = initial_status["storage"]
            assert Path(storage["database"]).resolve() == (data_dir / "context.sqlite3").resolve()
            assert Path(storage["artifact_root"]).resolve() == (data_dir / "artifacts").resolve()
            assert isinstance(storage["schema_version"], int)
            assert isinstance(storage["fts5"], bool)
            assert set(storage["sqlite_vec"]) >= {"available", "state", "reason"}
            assert storage["sqlite_vec"]["available"] is False

            artifact_body = b"api capability health acceptance artifact"
            digest = hashlib.sha256(artifact_body).hexdigest()
            uploaded = _http_json(
                server.base_url,
                "PUT",
                f"/v1/artifacts/{digest}",
                body=artifact_body,
                headers={
                    "Content-Type": "text/plain",
                    "X-Artifact-Sha256": digest,
                },
                expected_status=201,
            )
            assert uploaded["sha256"] == digest
            assert uploaded["size_bytes"] == len(artifact_body)
            artifact_path = data_dir / "artifacts" / digest[:2] / digest[2:4] / digest
            assert artifact_path.is_file()

            downloaded_status, downloaded, _ = _http_request(
                server.base_url,
                "GET",
                f"/v1/artifacts/{digest}",
            )
            assert downloaded_status == 200
            assert downloaded == artifact_body

            artifact_status = _http_json(server.base_url, "GET", "/v1/status")
            assert artifact_status["artifact_count"] == 1
            assert artifact_status["storage"]["artifact_root"] == storage["artifact_root"]

            ingest = _http_json(
                server.base_url,
                "POST",
                "/v1/ingest/batches",
                payload={"records": [_queue_probe_envelope()]},
                expected_status=202,
            )
            assert ingest["status"] == "accepted"
            assert ingest["accepted"] == 1
            assert ingest["records"] == 1
            assert ingest["captures"] == ["api-capability-health-queue-envelope"]
            assert ingest["queue"]["pending_count"] == 1

            # Age only this isolated synthetic row; the observable assertion
            # remains the real HTTP status response, not a private runtime API.
            expected_oldest_pending_at = _age_pending_job(data_dir)
            stale_status = _http_json(server.base_url, "GET", "/v1/status")
            queue = stale_status["queue"]
            assert queue["pending"] == 1
            assert queue["processing"] == 0
            assert queue["completed"] == 0
            assert queue["failed"] == 0
            assert queue["oldest_pending_at"] == expected_oldest_pending_at
            assert queue["oldest_pending_age_seconds"] >= 100
            assert stale_status["backlog"]["oldest_pending_age_seconds"] >= 100
            assert stale_status["model"]["worker_status"] == "not_configured"


def test_model_unavailable_and_degraded_reporting_real_uvicorn() -> None:
    """Exercise unavailable and degraded model diagnostics without a real model."""

    repository_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="life-context-model-probe-") as temporary:
        root = Path(temporary).resolve()

        with _ProbeEndpoint("unavailable") as endpoint:
            with _ApiProbeUvicornProcess(
                repository_root,
                root / "unavailable-data",
                model_endpoint=endpoint.base_url,
            ) as server:
                status = _http_json(server.base_url, "GET", "/v1/status")
                model = status["model"]
                assert model["configured"] is True
                assert model["status"] == "unavailable"
                assert model["worker_status"] == "running"
                assert model["model_status"] == "unavailable"
                assert model["availability"] == "unavailable"
                assert model["active_workers"] == 1
                assert model["health"]["status"] == "unavailable"
                assert model["health"]["http_status"] == 404
                assert model["capabilities"]["status"] == "unavailable"
                assert model["capabilities"]["http_status"] == 404
            assert endpoint.request_snapshot() == [
                ("GET", "/health"),
                ("GET", "/v1/models"),
            ]

        with _ProbeEndpoint("degraded") as endpoint:
            with _ApiProbeUvicornProcess(
                repository_root,
                root / "degraded-data",
                model_endpoint=endpoint.base_url,
            ) as server:
                status = _http_json(server.base_url, "GET", "/v1/status")
                model = status["model"]
                assert model["configured"] is True
                assert model["status"] == "degraded"
                assert model["worker_status"] == "running"
                assert model["model_status"] == "degraded"
                assert model["availability"] == "degraded"
                assert model["active_workers"] == 1
                assert model["health"]["status"] == "degraded"
                assert model["health"]["raw"] == {"status": "degraded", "ready": False}
                assert model["capabilities"]["status"] == "degraded"
                assert model["capabilities"]["capabilities"]["model_listed"] is True
                assert model["capabilities"]["capabilities"]["supports_json_schema"] is False
            assert endpoint.request_snapshot() == [
                ("GET", "/health"),
                ("GET", "/v1/models"),
            ]


def test_remote_auth_behavior_real_uvicorn() -> None:
    """Verify public diagnostics and bearer-protected feature routes at the socket."""

    repository_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="life-context-auth-probe-") as temporary:
        data_dir = Path(temporary).resolve() / "server-data"
        with _ApiProbeUvicornProcess(
            repository_root,
            data_dir,
            server_host="remote.example.test",
            auth_token=_AUTH_TOKEN,
            require_https_remote=False,
        ) as server:
            for path in ("/v1/health", "/v1/capabilities", "/v1/status"):
                public_response = _http_json(server.base_url, "GET", path)
                assert public_response["status"] in {"ok", "running"}

            missing = _http_json(
                server.base_url,
                "GET",
                "/v1/timeline",
                expected_status=401,
            )
            assert missing == {"detail": "bearer authentication required"}

            incorrect = _http_json(
                server.base_url,
                "GET",
                "/v1/timeline",
                headers={"Authorization": "Bearer incorrect-token"},
                expected_status=401,
            )
            assert incorrect == {"detail": "bearer authentication required"}

            authenticated_timeline = _http_json(
                server.base_url,
                "GET",
                "/v1/timeline",
                headers={"Authorization": f"Bearer {_AUTH_TOKEN}"},
            )
            assert authenticated_timeline["items"] == []
            assert authenticated_timeline["total_count"] == 0

            authenticated_ingest = _http_json(
                server.base_url,
                "POST",
                "/v1/ingest/batches",
                payload={"records": [_queue_probe_envelope()]},
                headers={"Authorization": f"Bearer {_AUTH_TOKEN}"},
                expected_status=202,
            )
            assert authenticated_ingest["status"] == "accepted"
            assert authenticated_ingest["accepted"] == 1
