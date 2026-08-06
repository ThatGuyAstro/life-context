"""Process-boundary acceptance coverage for the context event streams.

This probe starts the shipped ASGI application in a separate uvicorn process.
It keeps the archive in a temporary directory and uses a deterministic
loopback OpenAI-compatible HTTP stub for inference, so no real model or GPU is
involved.  The WebSocket client is intentionally implemented with the Python
standard library because the product package does not require a WebSocket
client dependency.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import socket
import struct
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from contracts import CaptureEnvelope, SourceEvent, SourceEventType, SourceKind

_PROCESS_HELPER_SPEC = importlib.util.spec_from_file_location(
    "life_context_process_acceptance_helper",
    Path(__file__).with_name("test_server_process_lifecycle.py"),
)
assert _PROCESS_HELPER_SPEC is not None and _PROCESS_HELPER_SPEC.loader is not None
_PROCESS_HELPER_MODULE = importlib.util.module_from_spec(_PROCESS_HELPER_SPEC)
_PROCESS_HELPER_SPEC.loader.exec_module(_PROCESS_HELPER_MODULE)
_RealUvicornProcess = _PROCESS_HELPER_MODULE._RealUvicornProcess

_AUTH_TOKEN = "event-stream-acceptance-token"
_MODEL_NAME = "event-stream-fake-model"
_MODEL_REVISION = "event-stream-fake-revision"
_CAPTURE_ID = "event-stream-process-envelope-001"
_EVENT_ID = "event-stream-process-event-001"
_MARKER = "event-stream-process-acceptance-marker"


class _FakeModelServer:
    """Deterministic local OpenAI-compatible endpoint for the real worker."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name="fake-model-server")

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> _FakeModelServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                with owner._lock:
                    owner.requests.append({"path": self.path, "method": "GET"})
                if self.path == "/health":
                    self._send_json(200, {"status": "ok", "ready": True})
                    return
                if self.path == "/v1/models":
                    self._send_json(
                        200,
                        {
                            "data": [
                                {
                                    "id": _MODEL_NAME,
                                    "capabilities": {
                                        "supports_json_schema": True,
                                        "supports_chat": True,
                                    },
                                }
                            ]
                        },
                    )
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                try:
                    request_body = json.loads(raw_body.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid json"})
                    return
                with owner._lock:
                    owner.requests.append({"path": self.path, "body": request_body})
                if self.path != "/v1/chat/completions":
                    self._send_json(404, {"error": "not found"})
                    return
                content = json.dumps(
                    {
                        "summary": "deterministic inference for the event stream probe",
                        "activity": "acceptance probe",
                        "project": None,
                        "application": "life-context",
                        "confidence": 0.99,
                        "tags": ["acceptance", "event-stream"],
                    },
                    separators=(",", ":"),
                )
                self._send_json(
                    200,
                    {
                        "id": "chatcmpl-event-stream-001",
                        "model": _MODEL_NAME,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                )

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler


class _EventStreamUvicornProcess(_RealUvicornProcess):
    """Existing real-process harness with auth and a temporary fake model."""

    def __init__(self, repository_root: Path, data_dir: Path, model_endpoint: str) -> None:
        super().__init__(repository_root, data_dir, uvicorn_ws="websockets")
        self.model_endpoint = model_endpoint

    def _environment(self) -> dict[str, str]:
        environment = super()._environment()
        environment.update(
            {
                # Make the application exercise the remote/authenticated
                # branch while uvicorn still binds only to loopback.
                "CONTEXT_SERVER_HOST": "remote.example.test",
                "CONTEXT_SERVER_AUTH_TOKEN": _AUTH_TOKEN,
                "CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE": "false",
                "CONTEXT_SERVER_MODEL_ENDPOINT": self.model_endpoint,
                "CONTEXT_SERVER_MODEL_NAME": _MODEL_NAME,
                "CONTEXT_SERVER_MODEL_REVISION": _MODEL_REVISION,
                "CONTEXT_SERVER_WORKER_COUNT": "1",
                "CONTEXT_MODEL_API_KEY": "",
            }
        )
        return environment


def _http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
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
    except HTTPError as error:
        status = int(error.code)
        response_body = error.read()
    assert status == expected_status, (
        f"{method} {path} returned {status}, expected {expected_status}: "
        f"{response_body.decode('utf-8', errors='replace')}"
    )
    decoded = json.loads(response_body.decode("utf-8")) if response_body else {}
    assert isinstance(decoded, dict)
    return decoded


class _SSEConnection:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.connection: http.client.HTTPConnection | None = None
        self.response: http.client.HTTPResponse | None = None

    def __enter__(self) -> _SSEConnection:
        parsed = urlsplit(self.base_url)
        assert parsed.hostname is not None and parsed.port is not None
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        self.connection.request(
            "GET",
            "/v1/events/stream",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {_AUTH_TOKEN}",
            },
        )
        self.response = self.connection.getresponse()
        assert self.response.status == 200, self.response.reason
        assert self.response.headers.get_content_type() == "text/event-stream"
        assert self.response.headers.get("Cache-Control") == "no-cache"
        return self

    def next_event(self) -> dict[str, Any]:
        assert self.response is not None
        fields: dict[str, Any] = {}
        while True:
            line = self.response.readline()
            assert line, "SSE stream closed before the expected event"
            decoded = line.decode("utf-8").rstrip("\r\n")
            if not decoded:
                if fields:
                    return fields
                continue
            if decoded.startswith(":"):
                continue
            name, separator, value = decoded.partition(":")
            assert separator, f"invalid SSE line: {decoded!r}"
            value = value[1:] if value.startswith(" ") else value
            fields[name] = json.loads(value) if name == "data" else value

    def __exit__(self, *_: object) -> None:
        if self.response is not None:
            self.response.close()
        if self.connection is not None:
            self.connection.close()


def _read_http_headers(connection: socket.socket) -> tuple[int, dict[str, str], bytes]:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(4096)
        assert chunk, "WebSocket peer closed during handshake"
        buffer += chunk
        assert len(buffer) <= 64 * 1024, "WebSocket handshake headers are too large"
    header_bytes, remainder = buffer.split(b"\r\n\r\n", 1)
    lines = header_bytes.decode("iso-8859-1").split("\r\n")
    status_line = lines[0].split(" ", 2)
    assert len(status_line) >= 2, f"invalid WebSocket handshake status: {lines[0]!r}"
    status = int(status_line[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return status, headers, remainder


def _websocket_upgrade(
    base_url: str, path: str, *, token: str | None
) -> tuple[socket.socket, int, dict[str, str], bytes]:
    parsed = urlsplit(base_url)
    assert parsed.hostname is not None and parsed.port is not None
    connection = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = [
        f"GET {path} HTTP/1.1",
        f"Host: {parsed.hostname}:{parsed.port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
    ]
    if token is not None:
        headers.append(f"Authorization: Bearer {token}")
    connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    status, response_headers, remainder = _read_http_headers(connection)
    if status == 101:
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        assert response_headers.get("sec-websocket-accept") == expected_accept
    return connection, status, response_headers, remainder


def _websocket_status(base_url: str, path: str, *, token: str | None) -> int:
    connection, status, _, _ = _websocket_upgrade(base_url, path, token=token)
    connection.close()
    return status


class _RawWebSocket:
    def __init__(self, connection: socket.socket, remainder: bytes) -> None:
        self.connection = connection
        self._buffer = remainder
        self._closed = False

    @classmethod
    def connect(cls, base_url: str, path: str) -> _RawWebSocket:
        connection, status, _, remainder = _websocket_upgrade(base_url, path, token=_AUTH_TOKEN)
        assert status == 101, f"WebSocket upgrade returned HTTP {status}"
        return cls(connection, remainder)

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.connection.recv(max(4096, size - len(self._buffer)))
            assert chunk, "WebSocket peer closed before a complete frame"
            self._buffer += chunk
        result, self._buffer = self._buffer[:size], self._buffer[size:]
        return result

    def receive_json(self) -> dict[str, Any]:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._read_exact(4) if masked else None
            payload = bytearray(self._read_exact(length))
            if mask is not None:
                for index in range(length):
                    payload[index] ^= mask[index % 4]
            if opcode == 0x9:
                self._send_frame(0xA, bytes(payload))
                continue
            assert opcode == 0x1, f"unexpected WebSocket opcode {opcode}"
            decoded = json.loads(bytes(payload).decode("utf-8"))
            assert isinstance(decoded, dict)
            return decoded

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
        self.connection.sendall(header + mask + masked)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def __enter__(self) -> _RawWebSocket:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _capture_envelope() -> dict[str, Any]:
    timestamp = "2026-08-05T12:00:00Z"
    return CaptureEnvelope(
        envelope_id=_CAPTURE_ID,
        captured_at=timestamp,
        source_kind=SourceKind.APPLICATION,
        producer="event-stream-process-acceptance",
        events=[
            SourceEvent(
                event_id=_EVENT_ID,
                source_id="application:event-stream-acceptance",
                source_kind=SourceKind.APPLICATION,
                event_type=SourceEventType.OBSERVED,
                occurred_at=timestamp,
                captured_at=timestamp,
                payload={"text": _MARKER, "window_title": "acceptance"},
                metadata={"deterministic": True, "model_free": True},
            )
        ],
        metadata={"deterministic": True, "model_free": True},
    ).to_dict()


def test_event_stream_real_process_acceptance() -> None:
    """Verify SSE/WS delivery, auth, disconnect behavior, and worker events."""

    repository_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix="life-context-event-stream-acceptance-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"
        with _FakeModelServer() as model:
            with _EventStreamUvicornProcess(repository_root, data_dir, model.base_url) as server:
                ready = _http_json(server.base_url, "GET", "/v1/health/ready")
                assert ready == {"status": "ok", "service": "context-server", "ready": True}

                status = _http_json(
                    server.base_url,
                    "GET",
                    "/v1/status",
                    headers={"Authorization": f"Bearer {_AUTH_TOKEN}"},
                )
                assert status["model"]["configured"] is True
                assert status["model"]["active_workers"] == 1

                with _SSEConnection(server.base_url) as stream:
                    ready_event = stream.next_event()
                    assert ready_event["event"] == "ready"
                    assert ready_event["data"] == {"service": "context-server"}

                    started = _http_json(
                        server.base_url,
                        "POST",
                        "/v1/start",
                        payload={"reason": "event-stream-acceptance"},
                        headers={"Authorization": f"Bearer {_AUTH_TOKEN}"},
                    )
                    assert started["status"] == "running"
                    capture_started = stream.next_event()
                    assert capture_started["event"] == "capture.started"
                    assert capture_started["data"] == {"reason": "event-stream-acceptance"}

                    ingest = _http_json(
                        server.base_url,
                        "POST",
                        "/v1/ingest/batches",
                        payload={"records": [_capture_envelope()]},
                        headers={
                            "Authorization": f"Bearer {_AUTH_TOKEN}",
                            "Idempotency-Key": "event-stream-process-batch-001",
                        },
                        expected_status=202,
                    )
                    assert ingest["status"] == "accepted"
                    assert ingest["accepted"] == 1

                    capture_event = stream.next_event()
                    assert capture_event["event"] == "context.observation"
                    assert capture_event["id"] == f"observation:{_EVENT_ID}"
                    assert capture_event["data"]["text"] == _MARKER
                    assert capture_event["data"]["metadata"]["event_id"] == _EVENT_ID

                    inference_event = stream.next_event()
                    assert inference_event["event"] == "context.inference"
                    assert inference_event["data"]["kind"] == "inference_observation"
                    assert inference_event["data"]["text"] == (
                        "deterministic inference for the event stream probe"
                    )
                    assert inference_event["data"]["metadata"]["source_event_id"] == _EVENT_ID

                assert (
                    _websocket_status(
                        server.base_url,
                        "/v1/events/ws?topic=context.inference",
                        token=None,
                    )
                    == 401
                )

                with _RawWebSocket.connect(
                    server.base_url, "/v1/events/ws?topic=context.inference"
                ) as websocket:
                    filtered = websocket.receive_json()
                    assert filtered["type"] == "context.inference"
                    assert filtered["id"] == inference_event["id"]
                    assert filtered["data"]["kind"] == "inference_observation"

                # Close a live stream before another event is published.  A
                # healthy process and clean log after the subsequent publish
                # provide the process-boundary evidence that the route's
                # disconnect task canceled its subscription/send task.
                with _RawWebSocket.connect(
                    server.base_url, "/v1/events/ws?topic=context.observation"
                ) as websocket:
                    assert websocket.receive_json()["id"] == capture_event["id"]
                    websocket.close()

                _http_json(
                    server.base_url,
                    "POST",
                    "/v1/ingest/batches",
                    payload={"records": [_capture_envelope()]},
                    headers={
                        "Authorization": f"Bearer {_AUTH_TOKEN}",
                        "Idempotency-Key": "event-stream-process-batch-duplicate-001",
                    },
                    expected_status=202,
                )
                time.sleep(0.25)
                live = _http_json(server.base_url, "GET", "/v1/health/live")
                assert live == {"status": "ok", "service": "context-server"}
                assert "Exception in ASGI application" not in server.output
                assert "Task exception was never retrieved" not in server.output

            requested_paths = [request["path"] for request in model.requests]
            assert "/v1/chat/completions" in requested_paths
            assert "/v1/models" in requested_paths
