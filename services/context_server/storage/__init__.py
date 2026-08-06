"""Context-server storage primitives.

This package has no dependency on the HTTP/server layer.  It can be imported
from a worker, a route adapter, or tests without creating routes or global
connections.
"""

from .artifacts import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactReferenced,
    ArtifactStore,
)
from .database import (
    ArtifactConflict,
    ContextStore,
    IdempotencyConflict,
    JobStateError,
    SQLiteStorage,
    Storage,
    VectorFeatureUnavailable,
)
from .migrations import MIGRATIONS, SCHEMA_VERSION, apply_migrations, current_schema_version
from .models import (
    ArtifactRecord,
    ArtifactRecoveryResult,
    CaptureRecord,
    JobRecord,
    JobRecoveryResult,
    ObservationRecord,
    PurgeResult,
    RetentionPolicy,
    RetentionTier,
    SourceRecord,
    SqliteVecStatus,
)
from .paths import (
    InvalidDigest,
    artifact_path,
    content_addressed_path,
    digest_bytes,
    digest_file,
    digest_stream,
    normalize_digest,
)
from .retention import expiration_for, purge_expired
from .vector import create_vector_index, load_sqlite_vec, status_from_connection

__all__ = [
    "ArtifactConflict",
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactRecord",
    "ArtifactRecoveryResult",
    "ArtifactReferenced",
    "ArtifactStore",
    "CaptureRecord",
    "ContextStore",
    "IdempotencyConflict",
    "InvalidDigest",
    "JobRecord",
    "JobRecoveryResult",
    "JobStateError",
    "MIGRATIONS",
    "ObservationRecord",
    "PurgeResult",
    "RetentionPolicy",
    "RetentionTier",
    "SCHEMA_VERSION",
    "SQLiteStorage",
    "SourceRecord",
    "SqliteVecStatus",
    "Storage",
    "VectorFeatureUnavailable",
    "apply_migrations",
    "artifact_path",
    "content_addressed_path",
    "current_schema_version",
    "create_vector_index",
    "digest_bytes",
    "digest_file",
    "digest_stream",
    "expiration_for",
    "normalize_digest",
    "purge_expired",
    "load_sqlite_vec",
    "status_from_connection",
]
