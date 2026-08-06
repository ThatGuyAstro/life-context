"""Hybrid full-text and semantic query orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .interfaces import QueryDependencies
from .models import Answer, Evidence, QueryRequest, QueryResponse, SearchHit, unwrap_hits
from .shared_contracts import SharedQueryRequest, SharedSearchHit
from .utils import acall_compatible, call_compatible, resolve


def _bounded_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score or score in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, score)


def _rank_scores(hits: Sequence[SearchHit]) -> list[float]:
    """Normalize backend scores while retaining rank signal for scoreless hits."""

    raw = [_bounded_score(hit.score) for hit in hits]
    maximum = max(raw, default=0.0)
    minimum = min(raw, default=0.0)
    if maximum <= 0:
        return [1.0 / (index + 1) for index in range(len(hits))]
    if maximum == minimum:
        return [1.0 / (index + 1) for index in range(len(hits))]
    return [score / maximum for score in raw]


_EVIDENCE_URL_KEYS = frozenset({"artifact_url", "artifact_uri", "uri"})


def _merged_hit(existing: SearchHit, incoming: SearchHit, source: str) -> SearchHit:
    sources = list(existing.retrieval_sources)
    if source not in sources:
        sources.append(source)
    metadata = dict(existing.metadata)
    for key, value in incoming.metadata.items():
        # The lexical backend is commonly the only source that knows the
        # API-facing artifact URL.  A semantic index may carry the same row
        # with a nullable or stale URL; never let that erase a usable one.
        if key in _EVIDENCE_URL_KEYS and metadata.get(key):
            continue
        metadata[key] = value
    text = existing.text or incoming.text
    timestamp = existing.timestamp if existing.timestamp is not None else incoming.timestamp
    return replace(
        existing,
        text=text,
        source=existing.source or incoming.source,
        timestamp=timestamp,
        metadata=metadata,
        retrieval_sources=tuple(sources),
        raw=existing.raw if existing.raw is not None else incoming.raw,
    )


def _normalize_answer(value: Any, evidence: Sequence[Evidence]) -> Answer:
    evidence_ids = tuple(item.id for item in evidence)
    if value is None:
        return Answer(evidence_ids=evidence_ids, status="not_configured")
    if isinstance(value, Answer):
        if value.evidence_ids:
            return value
        return replace(value, evidence_ids=evidence_ids)
    if isinstance(value, str):
        return Answer(text=value, evidence_ids=evidence_ids)
    if isinstance(value, Mapping):
        text = value.get("text", value.get("answer", value.get("content")))
        confidence = value.get("confidence", value.get("score"))
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        ids = value.get("evidence_ids", value.get("citations", evidence_ids))
        if isinstance(ids, str):
            ids = (ids,)
        return Answer(
            text=None if text is None else str(text),
            confidence=confidence,
            evidence_ids=tuple(str(item) for item in (ids or ())),
            status=str(value.get("status", "ok")),
            metadata=dict(value.get("metadata") or {})
            if isinstance(value.get("metadata", {}), Mapping)
            else {"value": value.get("metadata")},
        )
    text = getattr(value, "text", getattr(value, "answer", None))
    confidence = getattr(value, "confidence", None)
    ids = getattr(value, "evidence_ids", evidence_ids)
    if isinstance(ids, str):
        ids = (ids,)
    return Answer(
        text=None if text is None else str(text),
        confidence=float(confidence) if confidence is not None else None,
        evidence_ids=tuple(str(item) for item in (ids or ())),
        status=str(getattr(value, "status", "ok")),
        metadata=getattr(value, "metadata", {}) or {},
    )


class QueryOrchestrator:
    """Fuse FTS and semantic retrieval and shape answer-plus-evidence output.

    ``fts`` and ``semantic`` may be objects with a ``search`` method or plain
    callables.  ``answerer`` may expose ``answer`` or be callable.  All three
    are injected; this class never opens a database, loads a model, or owns an
    embedding implementation.
    """

    def __init__(
        self,
        fts: Any = None,
        semantic: Any = None,
        answerer: Any = None,
        *,
        dependencies: QueryDependencies | None = None,
    ) -> None:
        if dependencies is not None:
            fts = dependencies.fts
            semantic = dependencies.semantic
            answerer = dependencies.answerer
        self.fts = fts
        self.semantic = semantic
        self.answerer = answerer

    def query(
        self, request: QueryRequest | Mapping[str, Any] | str, **overrides: Any
    ) -> QueryResponse:
        """Synchronously execute a query against injected dependencies."""

        canonical = QueryRequest.from_value(request, **overrides)
        return self._query_sync(canonical)

    async def aquery(
        self, request: QueryRequest | Mapping[str, Any] | str, **overrides: Any
    ) -> QueryResponse:
        """Async counterpart used by API handlers."""

        canonical = QueryRequest.from_value(request, **overrides)
        return await self._query_async(canonical)

    def _active_backends(self, request: QueryRequest) -> list[tuple[str, Any]]:
        selected: list[tuple[str, Any]] = []
        if request.mode in {"hybrid", "fts"} and self.fts is not None:
            selected.append(("fts", self.fts))
        if request.mode in {"hybrid", "semantic"} and self.semantic is not None:
            selected.append(("semantic", self.semantic))
        return selected

    @staticmethod
    def _backend_options(request: QueryRequest) -> dict[str, Any]:
        return {
            "limit": request.limit,
            "filters": dict(request.filters),
            "start": request.start,
            "end": request.end,
            "mode": request.mode,
        }

    @staticmethod
    def _shared_request(request: QueryRequest) -> Any:
        """Translate compatible fields into the shared structured contract."""

        if SharedQueryRequest is None or SharedQueryRequest is QueryRequest:
            return request
        filters = dict(request.filters)
        values: dict[str, Any] = {
            "query": request.query or None,
            "limit": request.limit,
            "include_evidence": request.include_evidence,
            "observed_after": request.start,
            "observed_before": request.end,
        }
        filter_aliases = {
            "subject": ("subject",),
            "predicate": ("predicate",),
            "source_ids": ("source_ids", "source_id"),
            "kinds": ("kinds", "kind", "observation_kinds"),
            "statuses": ("statuses", "status"),
            "sort_order": ("sort_order",),
            "cursor": ("cursor",),
        }
        for key, aliases in filter_aliases.items():
            for alias in aliases:
                if alias not in filters:
                    continue
                value = filters[alias]
                if key in {"source_ids", "kinds", "statuses"} and isinstance(value, str):
                    value = [value]
                values[key] = value
                break
        try:
            if hasattr(SharedQueryRequest, "model_validate"):
                return SharedQueryRequest.model_validate(values)
            return SharedQueryRequest(**values)
        except (TypeError, ValueError):
            return request

    def _search_one(
        self, source: str, backend: Any, request: QueryRequest
    ) -> tuple[str, list[SearchHit]]:
        method = getattr(backend, "search", backend)
        shared_request = self._shared_request(request)
        raw = resolve(
            call_compatible(
                method,
                shared_request,
                query=request.query,
                options=self._backend_options(request),
            )
        )
        hits: list[SearchHit] = []
        for value in unwrap_hits(raw)[: request.limit]:
            normalized = SearchHit.from_value(value, default_source=source)
            if SharedSearchHit is not None and isinstance(value, SharedSearchHit):
                normalized = SearchHit.from_value(value, default_source=source)
            hits.append(normalized)
        return source, hits

    async def _search_one_async(
        self, source: str, backend: Any, request: QueryRequest
    ) -> tuple[str, list[SearchHit]]:
        method = getattr(backend, "search", backend)
        shared_request = self._shared_request(request)
        raw = await acall_compatible(
            method,
            shared_request,
            query=request.query,
            options=self._backend_options(request),
        )
        hits = [
            SearchHit.from_value(value, default_source=source)
            for value in unwrap_hits(raw)[: request.limit]
        ]
        return source, hits

    @staticmethod
    def _fuse(
        results: Iterable[tuple[str, Sequence[SearchHit]]], request: QueryRequest
    ) -> tuple[list[SearchHit], dict[str, Any]]:
        by_id: dict[str, SearchHit] = {}
        scores: dict[str, dict[str, float]] = {}
        ranks: dict[str, dict[str, int]] = {}
        active_sources: list[str] = []
        for source, source_hits in results:
            active_sources.append(source)
            normalized_hits = list(source_hits)
            source_scores = _rank_scores(normalized_hits)
            for rank, (hit, normalized_score) in enumerate(
                zip(normalized_hits, source_scores), start=1
            ):
                current = by_id.get(hit.id)
                if current is None:
                    by_id[hit.id] = replace(
                        hit,
                        retrieval_sources=(source,),
                        rank=rank,
                    )
                else:
                    by_id[hit.id] = _merged_hit(current, hit, source)
                scores.setdefault(hit.id, {})[source] = normalized_score
                ranks.setdefault(hit.id, {})[source] = rank
                if source == "fts":
                    by_id[hit.id] = replace(by_id[hit.id], fts_score=normalized_score)
                elif source == "semantic":
                    by_id[hit.id] = replace(by_id[hit.id], semantic_score=normalized_score)

        unique_sources = tuple(dict.fromkeys(active_sources))
        configured_weights = {
            "fts": request.fts_weight,
            "semantic": request.semantic_weight,
        }
        total_weight = sum(configured_weights.get(source, 0.0) for source in unique_sources)
        if total_weight <= 0 and unique_sources:
            normalized_weights = {source: 1.0 / len(unique_sources) for source in unique_sources}
        else:
            normalized_weights = {
                source: configured_weights.get(source, 0.0) / total_weight
                for source in unique_sources
            }

        fused: list[SearchHit] = []
        for identifier, hit in by_id.items():
            combined = sum(
                normalized_weights.get(source, 0.0) * scores.get(identifier, {}).get(source, 0.0)
                for source in unique_sources
            )
            best_rank = min(ranks.get(identifier, {}).values(), default=None)
            fused.append(replace(hit, combined_score=combined, score=combined, rank=best_rank))
        fused.sort(key=lambda hit: (-float(hit.combined_score or 0.0), hit.rank or 10**9, hit.id))
        return fused[: request.limit], {
            "sources": list(unique_sources),
            "weights": normalized_weights,
            "fusion": "weighted_normalized_score",
        }

    def _generate_answer(self, request: QueryRequest, evidence_hits: Sequence[SearchHit]) -> Answer:
        if self.answerer is None:
            return _normalize_answer(None, [Evidence.from_hit(hit) for hit in evidence_hits])
        method = getattr(self.answerer, "answer", self.answerer)
        raw = resolve(
            call_compatible(
                method,
                request,
                query=request.query,
                options={"evidence": evidence_hits, "hits": evidence_hits},
            )
        )
        return _normalize_answer(raw, [Evidence.from_hit(hit) for hit in evidence_hits])

    async def _generate_answer_async(
        self, request: QueryRequest, evidence_hits: Sequence[SearchHit]
    ) -> Answer:
        if self.answerer is None:
            return _normalize_answer(None, [Evidence.from_hit(hit) for hit in evidence_hits])
        method = getattr(self.answerer, "answer", self.answerer)
        raw = await acall_compatible(
            method,
            request,
            query=request.query,
            options={"evidence": evidence_hits, "hits": evidence_hits},
        )
        return _normalize_answer(raw, [Evidence.from_hit(hit) for hit in evidence_hits])

    def _finish(
        self,
        request: QueryRequest,
        results: Sequence[SearchHit],
        fusion_meta: Mapping[str, Any],
        started: float,
        warnings: list[str],
    ) -> QueryResponse:
        evidence_hits = (
            list(results[: request.evidence_limit or request.limit])
            if request.include_evidence
            else []
        )
        evidence = tuple(Evidence.from_hit(hit) for hit in evidence_hits)
        answer = self._generate_answer(request, evidence_hits)
        meta = {
            "mode": request.mode,
            "limit": request.limit,
            "result_count": len(results),
            "evidence_count": len(evidence),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            **dict(fusion_meta),
        }
        return QueryResponse(
            request=request,
            answer=answer,
            evidence=evidence,
            results=tuple(results),
            meta=meta,
            warnings=tuple(warnings),
        )

    async def _finish_async(
        self,
        request: QueryRequest,
        results: Sequence[SearchHit],
        fusion_meta: Mapping[str, Any],
        started: float,
        warnings: list[str],
    ) -> QueryResponse:
        evidence_hits = (
            list(results[: request.evidence_limit or request.limit])
            if request.include_evidence
            else []
        )
        evidence = tuple(Evidence.from_hit(hit) for hit in evidence_hits)
        answer = await self._generate_answer_async(request, evidence_hits)
        meta = {
            "mode": request.mode,
            "limit": request.limit,
            "result_count": len(results),
            "evidence_count": len(evidence),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            **dict(fusion_meta),
        }
        return QueryResponse(
            request=request,
            answer=answer,
            evidence=evidence,
            results=tuple(results),
            meta=meta,
            warnings=tuple(warnings),
        )

    def _query_sync(self, request: QueryRequest) -> QueryResponse:
        started = time.perf_counter()
        backends = self._active_backends(request)
        warnings: list[str] = []
        if request.mode in {"hybrid", "semantic"} and self.semantic is None:
            warnings.append("semantic retrieval unavailable: no semantic backend is configured")
        if request.mode in {"hybrid", "fts"} and self.fts is None:
            warnings.append("fts retrieval unavailable: no FTS backend is configured")
        if not backends:
            warnings.append("no retrieval backend is configured for the requested mode")
        results: list[tuple[str, list[SearchHit]]] = []
        for source, backend in backends:
            try:
                results.append(self._search_one(source, backend, request))
            except Exception as error:
                warnings.append(f"{source} retrieval failed: {error}")
        fused, fusion_meta = self._fuse(results, request)
        return self._finish(request, fused, fusion_meta, started, warnings)

    async def _query_async(self, request: QueryRequest) -> QueryResponse:
        started = time.perf_counter()
        backends = self._active_backends(request)
        warnings: list[str] = []
        if request.mode in {"hybrid", "semantic"} and self.semantic is None:
            warnings.append("semantic retrieval unavailable: no semantic backend is configured")
        if request.mode in {"hybrid", "fts"} and self.fts is None:
            warnings.append("fts retrieval unavailable: no FTS backend is configured")
        if not backends:
            warnings.append("no retrieval backend is configured for the requested mode")
        raw_results = list(
            await asyncio.gather(
                *(self._search_one_async(source, backend, request) for source, backend in backends),
                return_exceptions=True,
            )
        )
        results: list[tuple[str, list[SearchHit]]] = []
        for (source, _), result in zip(backends, raw_results):
            if isinstance(result, Exception):
                warnings.append(f"{source} retrieval failed: {result}")
            else:
                results.append(result)
        fused, fusion_meta = self._fuse(results, request)
        return await self._finish_async(request, fused, fusion_meta, started, warnings)


__all__ = ["QueryOrchestrator"]
