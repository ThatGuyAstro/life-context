from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

try:
    from contracts import CaptureEnvelope, QueryRequest, SourceEvent, SourceEventType, SourceKind
except ModuleNotFoundError:  # Running unittest directly from the source checkout.
    from packages.contracts.python.contracts import (
        CaptureEnvelope,
        QueryRequest,
        SourceEvent,
        SourceEventType,
        SourceKind,
    )

from apps.cli.client import ContextClientError, UrllibContextClient


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        if body is None:
            self.raw = b""
        elif isinstance(body, bytes):
            self.raw = body
        elif isinstance(body, str):
            self.raw = body.encode("utf-8")
        else:
            self.raw = json.dumps(body).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self.raw

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Any, float]] = []

    def __call__(self, request: Any, *, timeout: float) -> object:
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def header(request: Any, name: str) -> str | None:
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


class UrllibContextClientTests(unittest.TestCase):
    def test_versioned_health_status_query_timeline_and_auth(self) -> None:
        opener = RecordingOpener(
            [
                FakeResponse(200, {"status": "ok"}),
                FakeResponse(200, {"state": "running"}),
                FakeResponse(200, {"results": []}),
                FakeResponse(200, {"items": []}),
            ]
        )
        client = UrllibContextClient(
            "https://context.test/v1/",
            timeout=4.5,
            bearer_token="client-secret",
        )

        with patch("apps.cli.client.urlopen", side_effect=opener):
            self.assertEqual(client.health(), {"status": "ok"})
            self.assertEqual(client.status(), {"state": "running"})
            client.query(QueryRequest(query="coffee", subject="alice", limit=5))
            client.timeline({"limit": 3, "subject": "alice", "ignored": None})

        self.assertEqual(
            [request.full_url for request, _ in opener.requests[:3]],
            [
                "https://context.test/v1/health",
                "https://context.test/v1/status",
                "https://context.test/v1/query",
            ],
        )
        self.assertEqual(
            opener.requests[3][0].full_url,
            "https://context.test/v1/timeline?limit=3&subject=alice",
        )
        for request, timeout in opener.requests:
            self.assertEqual(header(request, "Authorization"), "Bearer client-secret")
            self.assertEqual(timeout, 4.5)

        query_request = opener.requests[2][0]
        self.assertEqual(
            json.loads(bytes(query_request.data).decode("utf-8")),
            QueryRequest(query="coffee", subject="alice", limit=5).to_dict(),
        )

    def test_ingest_batch_serializes_shared_envelope_and_idempotency_header(self) -> None:
        opener = RecordingOpener([FakeResponse(202, {"status": "accepted"})])
        client = UrllibContextClient("https://context.test", bearer_token="ingest-secret")
        event = SourceEvent(
            event_id="event-1",
            source_id="desktop",
            source_kind=SourceKind.APPLICATION,
            event_type=SourceEventType.OBSERVED,
            occurred_at="2026-08-05T12:00:00Z",
            payload={"window": "editor"},
        )
        envelope = CaptureEnvelope(
            envelope_id="capture-1",
            captured_at="2026-08-05T12:00:00Z",
            source_kind=SourceKind.APPLICATION,
            events=[event],
        )

        with patch("apps.cli.client.urlopen", side_effect=opener):
            result = client.ingest_batch(envelope, batch_id="batch-1")

        self.assertEqual(result, {"status": "accepted"})
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, "https://context.test/v1/ingest/batches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(header(request, "Authorization"), "Bearer ingest-secret")
        self.assertEqual(header(request, "Idempotency-Key"), "batch-1")
        self.assertEqual(
            json.loads(bytes(request.data).decode("utf-8")),
            envelope.to_dict(),
        )

    def test_artifact_upload_uses_digest_url_raw_bytes_metadata_and_auth(self) -> None:
        content = b"artifact bytes"
        digest = hashlib.sha256(content).hexdigest()
        opener = RecordingOpener([FakeResponse(201, {"sha256": digest})])
        client = UrllibContextClient("https://context.test", bearer_token="artifact-secret")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.bin"
            path.write_bytes(content)
            with patch("apps.cli.client.urlopen", side_effect=opener):
                result = client.upload_artifact(
                    path,
                    media_type="application/octet-stream",
                    metadata={"retention_tier": "raw", "label": "capture"},
                )

        self.assertEqual(result, {"sha256": digest})
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, f"https://context.test/v1/artifacts/{digest}")
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.data, content)
        self.assertEqual(header(request, "Authorization"), "Bearer artifact-secret")
        self.assertEqual(header(request, "Content-Type"), "application/octet-stream")
        self.assertEqual(header(request, "X-Artifact-SHA256"), digest)
        self.assertEqual(header(request, "X-Artifact-Size"), str(len(content)))
        self.assertEqual(
            json.loads(header(request, "X-Artifact-Metadata") or "{}"),
            {"retention_tier": "raw", "label": "capture"},
        )

    def test_http_and_network_errors_are_structured_and_user_safe(self) -> None:
        response_error_opener = RecordingOpener([FakeResponse(503, {"detail": "busy"})])
        client = UrllibContextClient("https://context.test", bearer_token="secret")
        with patch("apps.cli.client.urlopen", side_effect=response_error_opener):
            with self.assertRaises(ContextClientError) as context:
                client.status()
        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.body, {"detail": "busy"})
        self.assertIn("HTTP 503: busy", str(context.exception))
        self.assertNotIn("secret", str(context.exception))

        http_error = HTTPError(
            "https://context.test/v1/status",
            422,
            "invalid",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"detail":"bad request"}'),
        )
        with patch("apps.cli.client.urlopen", side_effect=http_error):
            with self.assertRaises(ContextClientError) as context:
                client.status()
        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(context.exception.body, {"detail": "bad request"})
        self.assertIn("HTTP 422: bad request", str(context.exception))

        with patch("apps.cli.client.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(ContextClientError) as context:
                client.status()
        self.assertIsNone(context.exception.status_code)
        self.assertIn("could not reach context server", str(context.exception))

    def test_successful_non_json_response_is_rejected_as_stale_endpoint(self) -> None:
        response = FakeResponse(
            200,
            "<html><body>dashboard</body></html>",
            {"Content-Type": "text/html"},
        )
        client = UrllibContextClient("http://127.0.0.1:8000")

        with patch("apps.cli.client.urlopen", return_value=response):
            with self.assertRaises(ContextClientError) as context:
                client.health()

        self.assertIsNone(context.exception.status_code)
        self.assertIn("non-JSON response", str(context.exception))
        self.assertIn("text/html", str(context.exception))

    def test_non_loopback_http_is_rejected_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-loopback HTTP"):
            UrllibContextClient("http://context.test", bearer_token="secret")

    def test_embedded_credentials_are_rejected_before_network(self) -> None:
        for endpoint in (
            "https://user:password@context.test",
            "https://:password@context.test",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "embedded credentials"):
                    UrllibContextClient(endpoint)

    def test_loopback_http_endpoints_remain_usable(self) -> None:
        for endpoint, expected_base_url in (
            ("http://localhost:8000", "http://localhost:8000"),
            ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000"),
            ("http://[::1]:8000/v1/", "http://[::1]:8000"),
        ):
            with self.subTest(endpoint=endpoint):
                client = UrllibContextClient(endpoint)
                self.assertEqual(client.base_url, expected_base_url)


if __name__ == "__main__":
    unittest.main()
