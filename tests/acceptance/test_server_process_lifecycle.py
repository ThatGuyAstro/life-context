"""Real-process acceptance coverage for the Python context server.

The existing full-story acceptance test runs Uvicorn in a thread.  This probe
crosses the actual Python process boundary so startup, signal handling, and
worker cleanup are covered as an operator would experience them.  It is
deliberately model-free and confines all server-owned files to a temporary
directory.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import queue
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import psutil
from contracts import (
    ArtifactKind,
    ArtifactRef,
    CaptureEnvelope,
    SourceEvent,
    SourceEventType,
    SourceKind,
)

_STARTUP_PORT = re.compile(r"running on http://127\.0\.0\.1:(\d+)", re.IGNORECASE)
_EXPECTED_MARKER = "process-lifecycle-acceptance-marker"


def _http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    expected_status: int | tuple[int, ...] = 200,
) -> tuple[int, bytes, Mapping[str, str]]:
    if payload is not None and body is not None:
        raise ValueError("request cannot contain both a JSON payload and raw body")

    request_headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if body is not None:
        request_headers.setdefault("Content-Type", "application/octet-stream")
    if headers:
        request_headers.update({str(key): str(value) for key, value in headers.items()})

    request = Request(
        f"{base_url}{path}",
        data=body,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with urlopen(request, timeout=5) as response:
            status = int(response.status)
            response_body = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as error:
        status = int(error.code)
        response_body = error.read()
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
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    expected_status: int | tuple[int, ...] = 200,
) -> dict[str, Any]:
    _, body, _ = _http_request(
        base_url,
        method,
        path,
        payload=payload,
        body=body,
        headers=headers,
        expected_status=expected_status,
    )
    decoded = json.loads(body.decode("utf-8")) if body else {}
    assert isinstance(decoded, dict), f"expected an object response, got {type(decoded)!r}"
    return decoded


def _read_sse_ready(base_url: str) -> dict[str, Any]:
    """Read exactly the first SSE event, then close the long-lived stream."""

    parsed = urlsplit(base_url)
    assert parsed.hostname is not None and parsed.port is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    response: http.client.HTTPResponse | None = None
    try:
        connection.request(
            "GET",
            "/v1/events/stream",
            headers={"Accept": "text/event-stream"},
        )
        response = connection.getresponse()
        assert response.status == 200, response.reason
        assert response.headers.get_content_type() == "text/event-stream"
        assert response.headers.get("Cache-Control") == "no-cache"

        fields: dict[str, Any] = {}
        while True:
            line = response.readline()
            assert line, "SSE stream closed before emitting a ready event"
            decoded = line.decode("utf-8").strip()
            if not decoded:
                break
            name, value = decoded.split(": ", 1)
            fields[name] = json.loads(value) if name == "data" else value

        assert fields["event"] == "ready"
        assert fields["data"] == {"service": "context-server"}
        return fields
    finally:
        if response is not None:
            response.close()
        connection.close()


class _RealUvicornProcess:
    """Own one real Uvicorn process and verify its graceful shutdown path."""

    def __init__(
        self, repository_root: Path, data_dir: Path, *, uvicorn_ws: str | None = None
    ) -> None:
        self.repository_root = repository_root
        self.data_dir = data_dir
        self.uvicorn_ws = uvicorn_ws
        self.process: subprocess.Popen[str] | None = None
        self._output_lines: list[str] = []
        self._output_queue: queue.Queue[str] = queue.Queue()
        self._reader: Thread | None = None
        self.base_url = ""
        self._owned_pids: set[int] = set()

    @property
    def output(self) -> str:
        return "".join(self._output_lines)

    def _read_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self._output_lines.append(line)
            self._output_queue.put(line)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # The process must not inherit an operator's archive, model endpoint,
        # auth token, or config file.  Only the explicit acceptance settings
        # below are allowed to reach the application.
        for key in tuple(environment):
            if key.startswith("CONTEXT_SERVER_"):
                environment.pop(key, None)
        environment.update(
            {
                "CONTEXT_SERVER_DATA_DIR": str(self.data_dir),
                "CONTEXT_SERVER_HOST": "127.0.0.1",
                "CONTEXT_SERVER_API_PREFIX": "",
                "CONTEXT_SERVER_DOCS_ENABLED": "false",
                "CONTEXT_SERVER_JSON_LOGS": "false",
                "CONTEXT_SERVER_LOG_LEVEL": "INFO",
                "CONTEXT_SERVER_RELOAD": "false",
                "CONTEXT_SERVER_MODEL_ENDPOINT": "",
                "CONTEXT_SERVER_MODEL_NAME": "",
                "CONTEXT_SERVER_MODEL_REVISION": "",
                "CONTEXT_SERVER_AUTH_TOKEN": "",
                # Keep any accidental CUDA discovery from becoming part of
                # this model-free acceptance process.
                "CUDA_VISIBLE_DEVICES": "",
            }
        )
        return environment

    def __enter__(self) -> _RealUvicornProcess:
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        )
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "services.context_server.application:app",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--lifespan",
            "on",
            "--log-level",
            "info",
            "--no-access-log",
        ]
        if self.uvicorn_ws is not None:
            command.extend(["--ws", self.uvicorn_ws])
        self.process = subprocess.Popen(
            command,
            cwd=self.repository_root,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
        self._reader = Thread(
            target=self._read_output,
            name="life-context-acceptance-process-output",
            daemon=True,
        )
        self._reader.start()

        try:
            deadline = time.monotonic() + 15
            port: int | None = None
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError(
                        f"Uvicorn exited during startup with code {self.process.returncode}:\n"
                        f"{self.output}"
                    )
                try:
                    line = self._output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                match = _STARTUP_PORT.search(line)
                if match:
                    port = int(match.group(1))
                    break
            assert port is not None, f"timed out waiting for Uvicorn port:\n{self.output}"
            self.base_url = f"http://127.0.0.1:{port}"
            return self
        except BaseException:
            self._stop(force=True)
            raise

    def _process_tree(self) -> set[int]:
        assert self.process is not None
        try:
            return {
                self.process.pid,
                *(child.pid for child in psutil.Process(self.process.pid).children(recursive=True)),
            }
        except psutil.NoSuchProcess:
            return {self.process.pid}

    def _assert_no_orphans(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            live: list[int] = []
            for pid in self._owned_pids:
                if psutil.pid_exists(pid):
                    live.append(pid)
            if not live:
                return
            time.sleep(0.05)
        assert not live, f"owned server/worker processes remain after shutdown: {live}"

    def _stop(self, *, force: bool = False) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self._owned_pids = self._process_tree()
            if force:
                self.process.terminate()
            elif os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
                raise AssertionError("Uvicorn did not stop after graceful shutdown signal")
        if self._reader is not None:
            self._reader.join(timeout=5)
        accepted_returncodes = (0, 3) if os.name == "nt" else (0,)
        assert self.process.returncode in accepted_returncodes, (
            f"Uvicorn exited with code {self.process.returncode}, expected "
            f"one of {accepted_returncodes}:\n{self.output}"
        )

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        cleanup_error: BaseException | None = None
        try:
            self._stop()
            if exc_type is None:
                assert "Application shutdown complete." in self.output, self.output
                assert "Finished server process" in self.output, self.output
            self._assert_no_orphans()
        except BaseException as error:
            cleanup_error = error
            if self.process is not None and self.process.poll() is None:
                try:
                    self._stop(force=True)
                except BaseException:
                    pass
        if cleanup_error is not None and exc_type is None:
            raise cleanup_error


def _acceptance_envelope(artifact: ArtifactRef) -> CaptureEnvelope:
    return CaptureEnvelope(
        envelope_id="process-lifecycle-envelope-001",
        captured_at="2026-08-05T12:00:00Z",
        source_kind=SourceKind.APPLICATION,
        producer="process-lifecycle-acceptance",
        events=[
            SourceEvent(
                event_id="process-lifecycle-event-001",
                source_id="application:acceptance",
                source_kind=SourceKind.APPLICATION,
                event_type=SourceEventType.OBSERVED,
                occurred_at="2026-08-05T12:00:00Z",
                captured_at="2026-08-05T12:00:00Z",
                payload={"text": _EXPECTED_MARKER, "window_title": "acceptance"},
                artifacts=[artifact],
                metadata={"deterministic": True},
            )
        ],
        metadata={"deterministic": True, "model_free": True},
    )


def test_python_server_real_process_lifecycle_acceptance() -> None:
    """Exercise the real process, API lifecycle, evidence path, and shutdown."""

    repository_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="life-context-process-acceptance-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"
        assert data_dir.is_relative_to(root)
        assert data_dir != Path(r"E:\LifeContext\data").resolve()

        with _RealUvicornProcess(repository_root, data_dir) as server:
            live = _http_json(server.base_url, "GET", "/v1/health/live")
            assert live == {"status": "ok", "service": "context-server"}

            health = _http_json(server.base_url, "GET", "/v1/health")
            assert health["status"] == "ok"
            assert health["ready"] is True
            ready = _http_json(server.base_url, "GET", "/v1/health/ready")
            assert ready == {"status": "ok", "service": "context-server", "ready": True}

            capabilities = _http_json(server.base_url, "GET", "/v1/capabilities")
            assert capabilities["status"] == "ok"
            assert {
                "capture-envelopes",
                "hybrid-query",
                "sse-events",
                "websocket-events",
            }.issubset(capabilities["capabilities"])

            initial_status = _http_json(server.base_url, "GET", "/v1/status")
            assert initial_status["status"] == "running"
            assert initial_status["ready"] is True
            assert initial_status["model"]["configured"] is False
            assert initial_status["model"]["active_workers"] == 0
            assert initial_status["model"]["worker_status"] == "not_configured"
            assert Path(initial_status["storage"]["database"]).resolve() == (
                data_dir / "context.sqlite3"
            ).resolve()

            sse_ready = _read_sse_ready(server.base_url)
            assert sse_ready["event"] == "ready"
            assert sse_ready["data"] == {"service": "context-server"}

            paused = _http_json(server.base_url, "POST", "/v1/pause")
            assert paused["status"] == "paused"
            assert _http_json(server.base_url, "GET", "/v1/status")["status"] == "paused"

            started = _http_json(
                server.base_url,
                "POST",
                "/v1/start",
                payload={"reason": "process-lifecycle-acceptance"},
            )
            assert started["status"] == "running"
            assert started["ready"] is True

            config_update = {
                "capture_interval_seconds": 2.5,
                "queue_size": 64,
                "source_routing": {
                    "clipboard": {"route": "local-only", "enabled": True},
                    "screenshots": {"route": "local", "enabled": True},
                },
            }
            updated_config = _http_json(
                server.base_url,
                "POST",
                "/v1/config",
                payload={"operation": "update", "settings": config_update},
            )
            assert updated_config["settings"]["capture_interval_seconds"] == 2.5
            assert updated_config["settings"]["queue_size"] == 64
            assert updated_config["settings"]["source_routing"] == config_update[
                "source_routing"
            ]

            round_trip = _http_json(server.base_url, "GET", "/v1/config")
            assert round_trip["settings"]["capture_interval_seconds"] == 2.5
            assert round_trip["settings"]["queue_size"] == 64
            assert round_trip["settings"]["source_routing"] == config_update["source_routing"]
            persisted = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            assert persisted == config_update

            artifact_body = b"deterministic-process-lifecycle-evidence"
            digest = hashlib.sha256(artifact_body).hexdigest()
            artifact = ArtifactRef(
                artifact_id=digest,
                kind=ArtifactKind.TEXT,
                media_type="text/plain",
                size_bytes=len(artifact_body),
                sha256=digest,
                metadata={"deterministic": True},
            )
            uploaded = _http_json(
                server.base_url,
                "PUT",
                f"/v1/artifacts/{digest}",
                body=artifact_body,
                headers={
                    "Content-Type": "text/plain",
                    "X-Artifact-Sha256": digest,
                    "X-Artifact-Metadata": json.dumps(
                        {"retention_tier": "raw", "deterministic": True}
                    ),
                },
                expected_status=201,
            )
            assert uploaded["sha256"] == digest
            assert uploaded["size_bytes"] == len(artifact_body)

            ingest = _http_json(
                server.base_url,
                "POST",
                "/v1/ingest/batches",
                payload={"records": [_acceptance_envelope(artifact).to_dict()]},
                headers={"Idempotency-Key": "process-lifecycle-batch-001"},
                expected_status=202,
            )
            assert ingest["status"] == "accepted"
            assert ingest["accepted"] == 1
            assert ingest["records"] == 1

            query = _http_json(
                server.base_url,
                "POST",
                "/v1/query",
                payload={
                    "query": _EXPECTED_MARKER,
                    "mode": "fts",
                    "limit": 5,
                    "include_evidence": True,
                },
            )
            assert len(query["results"]) == 1
            result = query["results"][0]
            assert result["id"] == "observation:process-lifecycle-event-001"
            assert _EXPECTED_MARKER in result["text"]
            assert query["answer"]["evidence_ids"] == [result["id"]]
            assert len(query["evidence"]) == 1
            assert query["evidence"][0]["id"] == result["id"]
            evidence_url = query["evidence"][0]["metadata"]["artifact_url"]
            assert evidence_url == f"/v1/artifacts/{digest}"
            _, downloaded, _ = _http_request(server.base_url, "GET", evidence_url)
            assert downloaded == artifact_body

            final_status = _http_json(server.base_url, "GET", "/v1/status")
            assert final_status["status"] == "running"
            assert final_status["event_count"] == 1
            assert final_status["artifact_count"] == 1
            assert final_status["model"]["configured"] is False
            assert final_status["model"]["active_workers"] == 0

        assert (data_dir / "context.sqlite3").is_file()
        assert (data_dir / "config.json").is_file()
