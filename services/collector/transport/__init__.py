"""Durable, source-independent collector transport."""

from .client import BatchIngestClient, ClientResponse, JsonBatchClient
from .config import TransportConfig
from .errors import (
    HttpTransportError,
    ReplayError,
    RetryableTransportError,
    SpoolCorruptionError,
    SpoolError,
    TransportError,
)
from .metrics import TransportMetrics
from .retry import RetryPolicy
from .spool import LocalSpool, Spool, SpoolBatch, StoredArtifact
from .transport import CollectorTransport, ReplayReport, Transport

__all__ = [
    "ClientResponse",
    "BatchIngestClient",
    "CollectorTransport",
    "HttpTransportError",
    "JsonBatchClient",
    "LocalSpool",
    "ReplayError",
    "ReplayReport",
    "RetryPolicy",
    "RetryableTransportError",
    "SpoolBatch",
    "Spool",
    "SpoolCorruptionError",
    "SpoolError",
    "StoredArtifact",
    "TransportConfig",
    "Transport",
    "TransportError",
    "TransportMetrics",
]
