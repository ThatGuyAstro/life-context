from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from services.collector.transport.spool import LocalSpool


class LocalSpoolTests(unittest.TestCase):
    def test_batch_is_durable_and_processing_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = LocalSpool(root)
            queued = first.enqueue_batch(
                [{"kind": "note", "value": 3}, {"kind": "tag", "value": "x"}],
                batch_id="stable-batch-1",
            )
            self.assertEqual(first.status()["pending_batches"], 1)
            self.assertEqual(queued.records[0]["kind"], "note")

            claimed = first.claim_batch()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(first.status()["processing_batches"], 1)

            restarted = LocalSpool(root)
            recovered = restarted.list_pending_batches()
            self.assertEqual([item.batch_id for item in recovered], ["stable-batch-1"])
            self.assertEqual(recovered[0].payload["records"][1]["value"], "x")
            restarted.acknowledge_batch(restarted.claim_batch())  # type: ignore[arg-type]
            self.assertEqual(restarted.status()["pending_batches"], 0)

    def test_contract_payload_can_be_preserved_without_source_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(temporary)
            batch = spool.enqueue_batch(
                {"events": [{"event_type": "generic", "attributes": {"a": 1}}]},
                batch_id="contract-batch",
            )
            self.assertEqual(batch.payload["events"][0]["event_type"], "generic")
            self.assertEqual(batch.records[0]["event_type"], "generic")

    def test_artifacts_are_content_addressed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(temporary)
            content = b"artifact bytes\x00\x01"
            expected_sha = hashlib.sha256(content).hexdigest()
            first = spool.put_artifact(
                content,
                metadata={"content_type": "application/octet-stream"},
            )
            second = spool.put_artifact(content, metadata={"content_type": "text/plain"})

            self.assertEqual(first.sha256, expected_sha)
            self.assertFalse(first.deduplicated)
            self.assertTrue(second.deduplicated)
            self.assertEqual(first.path, second.path)
            self.assertEqual(second.metadata["content_type"], "application/octet-stream")
            self.assertEqual(len(spool.list_pending_artifacts()), 1)
            self.assertEqual(first.path.read_bytes(), content)

            spool.mark_artifact_uploaded(expected_sha, {"accepted": True})
            self.assertTrue(spool.is_artifact_uploaded(expected_sha))
            self.assertEqual(spool.list_pending_artifacts(), [])
            self.assertEqual(spool.status()["uploaded_artifacts"], 1)

    def test_mapping_artifact_is_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(temporary)
            artifact = spool.put_artifact({"z": 1, "a": [True, None]})
            self.assertEqual(artifact.path.read_bytes(), b'{"a":[true,null],"z":1}')


if __name__ == "__main__":
    unittest.main()
