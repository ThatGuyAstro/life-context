"""Structural interfaces for query and context-server dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .models import QueryRequest, SearchHit


class FtsSearcher(Protocol):
    def search(self, request: QueryRequest) -> Sequence[SearchHit] | Any:
        """Return lexical/full-text hits for a query request."""


class SemanticSearcher(Protocol):
    def search(self, request: QueryRequest) -> Sequence[SearchHit] | Any:
        """Return embedding/vector hits for a query request."""


class AnswerGenerator(Protocol):
    def answer(self, request: QueryRequest, evidence: Sequence[SearchHit]) -> Any:
        """Generate an answer from the request and selected evidence."""


class TimelineProvider(Protocol):
    def timeline(self, options: Mapping[str, Any]) -> Any:
        """Read timeline entries."""


class ObservationStore(Protocol):
    def observe(self, observation: Mapping[str, Any]) -> Any:
        """Persist an observation through a storage-owned implementation."""


class StatusProvider(Protocol):
    def status(self) -> Any:
        """Return service/index health and lifecycle state."""


class LifecycleController(Protocol):
    def start(self, options: Mapping[str, Any] | None = None) -> Any:
        """Start or resume context processing."""

    def pause(self) -> Any:
        """Pause context processing."""


class PurgeStore(Protocol):
    def purge(self, options: Mapping[str, Any]) -> Any:
        """Delete context data according to an explicit policy."""


class ConfigProvider(Protocol):
    def config(self, operation: Mapping[str, Any] | None = None) -> Any:
        """Read or update context configuration."""


class EventStream(Protocol):
    def subscribe(self, topic: str | None = None) -> Iterable[Any] | AsyncIterator[Any]:
        """Return events from a server-owned event source."""


@dataclass(slots=True)
class QueryDependencies:
    """Optional retrieval/generation dependencies for ``QueryOrchestrator``."""

    fts: FtsSearcher | Any | None = None
    semantic: SemanticSearcher | Any | None = None
    answerer: AnswerGenerator | Any | None = None


@dataclass(slots=True)
class RouteServices:
    """Dependency bundle consumed by API route handlers.

    Values are intentionally typed as ``Any`` at the boundary.  Concrete
    storage, model, and lifecycle implementations belong to the application
    that assembles the router, not this package.
    """

    query_service: Any = None
    timeline_provider: Any = None
    observation_store: Any = None
    status_provider: Any = None
    lifecycle: Any = None
    purge_store: Any = None
    config_provider: Any = None
    event_stream: Any = None

    def __init__(
        self,
        query: Any = None,
        timeline: Any = None,
        observation: Any = None,
        status: Any = None,
        lifecycle: Any = None,
        purge: Any = None,
        config: Any = None,
        events: Any = None,
        **aliases: Any,
    ) -> None:
        self.query_service = aliases.pop("query_service", query)
        self.timeline_provider = aliases.pop("timeline_provider", timeline)
        self.observation_store = aliases.pop("observation_store", observation)
        self.status_provider = aliases.pop("status_provider", status)
        self.lifecycle = lifecycle
        self.purge_store = aliases.pop("purge_store", purge)
        self.config_provider = aliases.pop("config_provider", config)
        self.event_stream = aliases.pop("event_stream", events)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise TypeError(f"unexpected route service(s): {unknown}")


__all__ = [
    "AnswerGenerator",
    "ConfigProvider",
    "EventStream",
    "FtsSearcher",
    "LifecycleController",
    "ObservationStore",
    "PurgeStore",
    "QueryDependencies",
    "RouteServices",
    "SemanticSearcher",
    "StatusProvider",
    "TimelineProvider",
]
