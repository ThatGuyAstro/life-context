from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.errors import RetryableTransportError
from services.collector.transport.transport import CollectorTransport


class RecordingCollectorClient:
    """Small transport-only server double that retains received artifact bytes."""

    def __init__(self) -> None:
        self.online = True
        self.fail_artifact_uploads = False
        self.drop_next_artifact_response = False
        self.calls: list[tuple[str, str]] = []
        self.received_artifacts: dict[str, bytes] = {}
        self.received_batches: list[tuple[str | None, dict[str, Any]]] = []

    @staticmethod
    def _offline(operation: str) -> None:
        error = RetryableTransportError(f"{operation} endpoint offline")
        error.attempts = 1  # type: ignore[attr-defined]
        raise error

    def upload_artifact(self, artifact: object) -> ClientResponse:
        digest = artifact.sha256  # type: ignore[attr-defined]
        path = artifact.path  # type: ignore[attr-defined]
        content = Path(path).read_bytes()
        self.calls.append(("artifact", digest))

        if not self.online or self.fail_artifact_uploads:
            self._offline("artifact")

        if hashlib.sha256(content).hexdigest() != digest:
            raise AssertionError("client received bytes under the wrong digest")

        if self.drop_next_artifact_response:
            self.drop_next_artifact_response = False
            self.received_artifacts[digest] = content
            self.online = False
            self._offline("artifact response")

        if digest in self.received_artifacts:
            return ClientResponse(409, {"sha256": digest}, {}, 1)

        self.received_artifacts[digest] = content
        return ClientResponse(201, {"sha256": digest}, {}, 1)

    def ingest_batch(
        self,
        payload: object,
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        self.calls.append(("batch", batch_id or ""))
        if not self.online:
            self._offline("batch")
        if not isinstance(payload, dict):
            raise AssertionError("batch payload must be a mapping")
        self.received_batches.append((batch_id, payload))
        return ClientResponse(202, {"accepted": True}, {}, 1)


class ArtifactIntegrityTransportTests(unittest.TestCase):
    @staticmethod
    def _transport(root: Path, client: RecordingCollectorClient) -> CollectorTransport:
        config = TransportConfig(
            server_url="http://127.0.0.1:8080",
            spool_dir=root,
            fsync=False,
        )
        return CollectorTransport(config, client=client)

    def test_content_addressed_dedup_preserves_retrievable_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordingCollectorClient()
            transport = self._transport(root, client)
            content = b"artifact\x00bytes\x01"
            expected_digest = hashlib.sha256(content).hexdigest()

            first = transport.enqueue_artifact(
                content,
                metadata={"content_type": "application/octet-stream", "source": "first"},
            )
            duplicate = transport.enqueue_artifact(
                content,
                metadata={"content_type": "text/plain", "source": "duplicate"},
            )

            self.assertEqual(first.sha256, expected_digest)
            self.assertTrue(duplicate.deduplicated)
            self.assertEqual(first.path, duplicate.path)
            self.assertEqual(transport.spool.artifact(expected_digest).path.read_bytes(), content)
            self.assertEqual(
                transport.spool.artifact(expected_digest).metadata["source"],
                "first",
            )
            self.assertEqual(
                [item.sha256 for item in transport.spool.list_pending_artifacts()],
                [expected_digest],
            )
            self.assertEqual(transport.status()["artifacts_stored"], 1)
            self.assertEqual(transport.status()["artifacts_deduplicated"], 1)

    def test_offline_replay_orders_artifact_before_batch_and_replays_after_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordingCollectorClient()
            client.online = False
            transport = self._transport(root, client)
            content = b"queued while offline"
            artifact = transport.enqueue_artifact(content)
            batch_id = "batch-after-reconnect"
            transport.enqueue_batch(
                [{"artifact_sha256": artifact.sha256, "kind": "event"}],
                batch_id=batch_id,
            )

            offline = transport.replay()

            self.assertFalse(offline.ok)
            self.assertEqual(client.calls, [("artifact", artifact.sha256), ("batch", batch_id)])
            self.assertEqual(transport.status()["pending_artifacts"], 1)
            self.assertEqual(transport.status()["pending_batches"], 1)

            client.online = True
            reconnected = transport.replay()

            self.assertTrue(reconnected.ok)
            self.assertEqual(
                client.calls,
                [
                    ("artifact", artifact.sha256),
                    ("batch", batch_id),
                    ("artifact", artifact.sha256),
                    ("batch", batch_id),
                ],
            )
            self.assertEqual(client.received_artifacts[artifact.sha256], content)
            self.assertEqual(
                client.received_batches[0],
                (
                    batch_id,
                    {"records": [{"artifact_sha256": artifact.sha256, "kind": "event"}]},
                ),
            )
            self.assertEqual(transport.status()["pending_artifacts"], 0)
            self.assertEqual(transport.status()["pending_batches"], 0)

    def test_idempotent_artifact_replay_after_reconnect_accepts_existing_server_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordingCollectorClient()
            client.drop_next_artifact_response = True
            transport = self._transport(root, client)
            content = b"server accepted before the connection dropped"
            artifact = transport.enqueue_artifact(content)

            first = transport.replay()

            self.assertFalse(first.ok)
            self.assertEqual(first.artifacts_failed, 1)
            self.assertEqual(transport.status()["pending_artifacts"], 1)
            self.assertEqual(client.received_artifacts[artifact.sha256], content)

            client.online = True
            second = transport.replay()

            self.assertTrue(second.ok)
            self.assertEqual(second.artifacts_uploaded, 1)
            self.assertTrue(transport.spool.is_artifact_uploaded(artifact.sha256))
            self.assertEqual(transport.status()["pending_artifacts"], 0)
            self.assertEqual(
                client.calls,
                [("artifact", artifact.sha256), ("artifact", artifact.sha256)],
            )

            third = transport.replay()
            self.assertTrue(third.ok)
            self.assertEqual(third.artifacts_attempted, 0)
            self.assertEqual(len(client.calls), 2)

    def test_failed_artifact_upload_retains_blob_for_later_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = RecordingCollectorClient()
            client.fail_artifact_uploads = True
            transport = self._transport(root, client)
            content = b"retain this artifact after a failed upload"
            artifact = transport.enqueue_artifact(content)

            failed = transport.replay()

            self.assertFalse(failed.ok)
            self.assertEqual(failed.artifacts_attempted, 1)
            self.assertEqual(failed.artifacts_failed, 1)
            self.assertEqual(failed.artifacts_uploaded, 0)
            self.assertFalse(transport.spool.is_artifact_uploaded(artifact.sha256))
            self.assertEqual(transport.spool.artifact(artifact.sha256).path.read_bytes(), content)
            self.assertEqual(transport.status()["pending_artifacts"], 1)

            client.fail_artifact_uploads = False
            recovered = transport.replay()

            self.assertTrue(recovered.ok)
            self.assertEqual(recovered.artifacts_uploaded, 1)
            self.assertEqual(client.received_artifacts[artifact.sha256], content)
            self.assertEqual(transport.status()["pending_artifacts"], 0)


if __name__ == "__main__":
    unittest.main()
