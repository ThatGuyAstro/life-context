from __future__ import annotations

import asyncio
import unittest

from services.context_server.query import (
    EventStreamStub,
    QueryOrchestrator,
    QueryRequest,
    event_to_sse,
)


class FtsBackend:
    def search(self, request):
        return [
            {"id": "shared", "text": "Lexical evidence", "score": 10, "source": "notes"},
            {"id": "lexical", "text": "Only lexical", "score": 5, "source": "notes"},
        ]


class SemanticBackend:
    def search(self, query, limit=10, filters=None):
        self.last = (query, limit, filters)
        return [
            {"id": "shared", "text": "Semantic evidence", "score": 1.0, "source": "embedding"},
            {"id": "semantic", "text": "Only semantic", "score": 0.5, "source": "embedding"},
        ]


class Answerer:
    def answer(self, request, evidence):
        return {
            "answer": f"Answer for {request.query}",
            "confidence": 0.8,
            "evidence_ids": [item.id for item in evidence[:1]],
        }


class AsyncBackend:
    async def search(self, req):
        await asyncio.sleep(0)
        return [{"id": "async", "text": req.query, "score": 1.0}]


class QueryOrchestratorTests(unittest.TestCase):
    def test_hybrid_fusion_deduplicates_and_shapes_answer(self):
        semantic = SemanticBackend()
        response = QueryOrchestrator(FtsBackend(), semantic, Answerer()).query(
            {"query": "where", "limit": 4, "evidence_limit": 2}
        )

        self.assertEqual(response.answer.text, "Answer for where")
        self.assertEqual(response.answer.evidence_ids, ("shared",))
        self.assertEqual([hit.id for hit in response.results], ["shared", "lexical", "semantic"])
        self.assertEqual(response.results[0].retrieval_sources, ("fts", "semantic"))
        self.assertEqual(len(response.evidence), 2)
        payload = response.to_dict()
        self.assertIn("answer", payload)
        self.assertIn("evidence", payload)
        self.assertEqual(semantic.last, ("where", 4, {}))

    def test_mode_can_use_only_one_backend_and_warn_when_missing(self):
        response = QueryOrchestrator(fts=FtsBackend()).query("lexical", mode="semantic")
        self.assertEqual(response.results, ())
        self.assertTrue(response.warnings)
        self.assertEqual(response.meta["sources"], [])

    def test_async_backend_is_supported(self):
        async def run():
            return await QueryOrchestrator(semantic=AsyncBackend()).aquery(
                QueryRequest("async", mode="semantic")
            )

        response = asyncio.run(run())
        self.assertEqual(response.results[0].id, "async")
        self.assertEqual(response.results[0].text, "async")


class EventStreamTests(unittest.TestCase):
    def test_event_stream_snapshot_and_sse_encoding(self):
        stream = EventStreamStub()
        event = stream.publish({"kind": "observation"}, event_type="observation")
        self.assertEqual(stream.snapshot("observation"), (event,))
        encoded = event_to_sse(event)
        self.assertIn("event: observation", encoded)
        self.assertIn('"kind":"observation"', encoded)
