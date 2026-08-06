from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from services.collector.transport.client import JsonBatchClient
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import RetryableTransportError


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        if body is None:
            self._raw = b""
        elif isinstance(body, bytes):
            self._raw = body
        elif isinstance(body, str):
            self._raw = body.encode("utf-8")
        else:
            self._raw = json.dumps(body).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._raw

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: object, **kwargs: object) -> object:
        self.calls.append(
            {
                "url": request.full_url,  # type: ignore[attr-defined]
                "method": request.get_method(),  # type: ignore[attr-defined]
                "headers": dict(request.headers),  # type: ignore[attr-defined]
                "data": request.data,  # type: ignore[attr-defined]
                "kwargs": kwargs,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class JsonBatchClientTests(unittest.TestCase):
    def test_batch_is_json_and_has_bearer_and_idempotency_headers(self) -> None:
        opener = RecordingOpener([FakeResponse(202, {"accepted": 2})])
        config = TransportConfig(
            server_url="https://collector.test:8080/",
            bearer_token="secret-token",
            max_attempts=1,
        )
        client = JsonBatchClient(config, opener=opener)

        response = client.ingest_batch([{"id": 1}, {"id": 2}], batch_id="batch-7")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.body, {"accepted": 2})
        call = opener.calls[0]
        self.assertEqual(call["url"], "https://collector.test:8080/v1/ingest/batch")
        headers = call["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Idempotency-key"], "batch-7")
        self.assertEqual(
            json.loads(bytes(call["data"]).decode("utf-8")),
            {"records": [{"id": 1}, {"id": 2}]},
        )

    def test_batch_duplicate_conflict_is_an_accepted_idempotent_response(self) -> None:
        opener = RecordingOpener([FakeResponse(409, {"duplicate": True})])
        config = TransportConfig(
            server_url="https://collector.test:8080",
            max_attempts=1,
        )
        client = JsonBatchClient(config, opener=opener)

        response = client.ingest_batch({"events": [{"event_id": "event-1"}]}, batch_id="batch-1")

        self.assertFalse(response.ok)
        self.assertTrue(response.idempotent_conflict)
        self.assertEqual(response.status_code, 409)

    def test_retry_backoff_retries_server_failure(self) -> None:
        opener = RecordingOpener(
            [
                FakeResponse(503, {"error": "busy"}),
                FakeResponse(502),
                FakeResponse(200, {"ok": True}),
            ]
        )
        sleeps: list[float] = []
        config = TransportConfig(
            server_url="http://localhost:9000",
            max_attempts=3,
            backoff_initial_seconds=0.1,
            backoff_max_seconds=1.0,
        )
        client = JsonBatchClient(config, opener=opener, sleep=sleeps.append)

        response = client.ingest_batch({"records": [{"value": "x"}]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.attempts, 3)
        self.assertEqual(sleeps, [0.1, 0.2])
        self.assertEqual(len(opener.calls), 3)

    def test_network_failure_exhaustion_reports_attempt_count(self) -> None:
        opener = RecordingOpener([URLError("offline"), URLError("offline")])
        sleeps: list[float] = []
        config = TransportConfig(server_url="http://localhost:9000", max_attempts=2)
        client = JsonBatchClient(config, opener=opener, sleep=sleeps.append)

        with self.assertRaises(RetryableTransportError) as context:
            client.ingest_batch([])

        self.assertEqual(getattr(context.exception, "attempts"), 2)
        self.assertEqual(len(sleeps), 1)

    def test_artifact_uses_digest_url_and_accepts_conflict_as_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "photo.bin"
            path.write_bytes(b"photo")
            digest = hashlib.sha256(b"photo").hexdigest()
            opener = RecordingOpener([FakeResponse(409, {"already": "there"})])
            config = TransportConfig(
                server_url="https://collector.example",
                artifact_path="/api/artifacts/{sha256}",
                bearer_token="token",
                insecure_tls=True,
                max_attempts=1,
            )
            client = JsonBatchClient(config, opener=opener)

            response = client.upload_artifact(path)

            self.assertTrue(response.idempotent_conflict)
            call = opener.calls[0]
            self.assertEqual(call["method"], "PUT")
            self.assertEqual(call["url"], f"https://collector.example/api/artifacts/{digest}")
            self.assertEqual(call["headers"]["X-artifact-sha256"], digest)
            self.assertEqual(call["headers"]["Authorization"], "Bearer token")
            self.assertEqual(call["data"], b"photo")
            self.assertIsNotNone(call["kwargs"].get("context"))


if __name__ == "__main__":
    unittest.main()
