from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import RetryableTransportError
from services.collector.transport.spool import LocalSpool
from services.collector.transport.transport import CollectorTransport


class FakeClient:
    def __init__(
        self,
        *,
        fail_batches: bool = False,
        fail_artifacts: bool = False,
        duplicate_batches: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_batches = fail_batches
        self.fail_artifacts = fail_artifacts
        self.duplicate_batches = duplicate_batches

    def upload_artifact(self, artifact: object) -> ClientResponse:
        sha256 = artifact.sha256  # type: ignore[attr-defined]
        self.calls.append(("artifact", sha256))
        if self.fail_artifacts:
            error = RetryableTransportError("artifact endpoint offline")
            error.attempts = 2  # type: ignore[attr-defined]
            raise error
        return ClientResponse(201, {"sha256": sha256}, {}, 1)

    def ingest_batch(self, payload: object, *, batch_id: str | None = None) -> ClientResponse:
        self.calls.append(("batch", batch_id or ""))
        if self.fail_batches:
            error = RetryableTransportError("batch endpoint offline")
            error.attempts = 2  # type: ignore[attr-defined]
            raise error
        if self.duplicate_batches:
            return ClientResponse(409, {"duplicate": True}, {}, 1)
        return ClientResponse(202, {"accepted": True}, {}, 1)


class CollectorTransportTests(unittest.TestCase):
    def test_replay_delivers_artifacts_before_batches_and_reports_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = TransportConfig(server_url="http://localhost:8080", spool_dir=root)
            client = FakeClient()
            transport = CollectorTransport(config, client=client)
            artifact = transport.enqueue_artifact(
                b"payload",
                metadata={"content_type": "application/test"},
            )
            transport.enqueue_batch(
                [{"artifact_sha256": artifact.sha256, "kind": "event"}],
                batch_id="batch-1",
            )

            report = transport.replay()
            status = transport.status()

            self.assertTrue(report.ok)
            self.assertEqual(report.artifacts_uploaded, 1)
            self.assertEqual(report.batches_uploaded, 1)
            self.assertEqual([kind for kind, _ in client.calls], ["artifact", "batch"])
            self.assertEqual(status["pending_artifacts"], 0)
            self.assertEqual(status["pending_batches"], 0)
            self.assertEqual(status["artifacts_uploaded"], 1)
            self.assertEqual(status["batches_uploaded"], 1)
            self.assertEqual(status["records_uploaded"], 1)

    def test_failed_replay_keeps_batch_and_returns_failure_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = TransportConfig(server_url="http://localhost:8080", spool_dir=temporary)
            client = FakeClient(fail_batches=True)
            transport = CollectorTransport(config, client=client)
            transport.enqueue_batch([{"kind": "event"}], batch_id="batch-fails")

            report = transport.replay()

            self.assertFalse(report.ok)
            self.assertEqual(report.batches_failed, 1)
            self.assertEqual(transport.spool.status()["pending_batches"], 1)
            self.assertEqual(transport.spool.status()["processing_batches"], 0)
            self.assertEqual(transport.status()["batches_failed"], 1)
            self.assertEqual(transport.status()["retry_exhausted"], 1)

    def test_duplicate_batch_replay_is_acknowledged_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = TransportConfig(server_url="http://localhost:8080", spool_dir=temporary)
            client = FakeClient(duplicate_batches=True)
            transport = CollectorTransport(config, client=client)
            transport.enqueue_batch([{"kind": "event"}], batch_id="batch-duplicate")

            report = transport.replay()

            self.assertTrue(report.ok)
            self.assertEqual(report.batches_uploaded, 1)
            self.assertEqual(transport.spool.status()["pending_batches"], 0)

    def test_duplicate_artifact_enqueue_is_counted_without_duplicate_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = TransportConfig(server_url="http://localhost:8080", spool_dir=temporary)
            transport = CollectorTransport(config, client=FakeClient())
            first = transport.enqueue_artifact(b"same")
            second = transport.enqueue_artifact(b"same")

            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(transport.status()["artifacts_stored"], 1)
            self.assertEqual(transport.status()["artifacts_deduplicated"], 1)
            self.assertEqual(len(transport.spool.list_pending_artifacts()), 1)


if __name__ == "__main__":
    unittest.main()
