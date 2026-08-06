"""Query orchestration for the context server.

The package deliberately contains no storage or model implementation.  Those
concerns are supplied through the protocols in :mod:`interfaces` so the same
orchestrator can be used by an API process, a test double, or a future CLI
adapter.
"""

from .interfaces import (
    AnswerGenerator,
    ConfigProvider,
    EventStream,
    FtsSearcher,
    LifecycleController,
    ObservationStore,
    PurgeStore,
    SemanticSearcher,
    StatusProvider,
    TimelineProvider,
)
from .models import Answer, Evidence, QueryRequest, QueryResponse, SearchHit
from .orchestrator import QueryOrchestrator
from .stream import Event, EventStreamStub, event_to_sse

__all__ = [
    "Answer",
    "AnswerGenerator",
    "ConfigProvider",
    "Event",
    "EventStream",
    "EventStreamStub",
    "Evidence",
    "FtsSearcher",
    "LifecycleController",
    "ObservationStore",
    "PurgeStore",
    "QueryOrchestrator",
    "QueryRequest",
    "QueryResponse",
    "SearchHit",
    "SemanticSearcher",
    "StatusProvider",
    "TimelineProvider",
    "event_to_sse",
]
