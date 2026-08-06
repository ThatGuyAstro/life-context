"""Public value objects used by the context-server storage layer.

The storage package deliberately keeps its records as small immutable
dataclasses.  Callers can pass ordinary dictionaries into the write methods,
while reads return typed records with decoded metadata/payload values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping


class RetentionTier(str, Enum):
    """Retention class for data and artifacts.

    ``raw`` is short-lived capture material, ``keyframe`` is a longer-lived
    selected frame, and ``derived`` is durable metadata or model output.
    """

    RAW = "raw"
    KEYFRAME = "keyframe"
    DERIVED = "derived"

    @classmethod
    def coerce(cls, value: "RetentionTier | str") -> "RetentionTier":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown retention tier: {value!r}") from exc


@dataclass(frozen=True)
class RetentionPolicy:
    """Default retention windows.

    Expiration is calculated from the record/artifact creation time supplied
    by the caller.  A derived item has no expiration timestamp.
    """

    raw: timedelta = timedelta(hours=24)
    keyframe: timedelta = timedelta(days=30)
    derived: timedelta | None = None

    def expires_at(
        self,
        tier: RetentionTier | str,
        created_at: datetime,
    ) -> datetime | None:
        normalized = RetentionTier.coerce(tier)
        duration = {
            RetentionTier.RAW: self.raw,
            RetentionTier.KEYFRAME: self.keyframe,
            RetentionTier.DERIVED: self.derived,
        }[normalized]
        if duration is None:
            return None
        return ensure_utc(created_at) + duration


@dataclass(frozen=True)
class SourceRecord:
    id: str
    kind: str
    name: str | None
    uri: str | None
    external_id: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: utc_now())
    updated_at: datetime = field(default_factory=lambda: utc_now())


@dataclass(frozen=True)
class CaptureRecord:
    id: str
    source_id: str | None
    kind: str
    external_id: str | None
    captured_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=lambda: utc_now())
    updated_at: datetime = field(default_factory=lambda: utc_now())


@dataclass(frozen=True)
class ObservationRecord:
    id: str
    capture_id: str | None
    source_id: str | None
    kind: str
    observed_at: datetime
    text_content: str
    payload: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    retention_tier: RetentionTier = RetentionTier.DERIVED
    expires_at: datetime | None = None
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=lambda: utc_now())
    updated_at: datetime = field(default_factory=lambda: utc_now())


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: str
    status: str
    payload: Any
    idempotency_key: str
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_until: datetime | None
    locked_by: str | None
    last_error: str | None
    result: Any = None
    created_at: datetime = field(default_factory=lambda: utc_now())
    updated_at: datetime = field(default_factory=lambda: utc_now())
    completed_at: datetime | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    digest: str
    size_bytes: int
    media_type: str
    relative_path: str
    retention_tier: RetentionTier
    created_at: datetime
    expires_at: datetime | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    last_accessed_at: datetime | None = None


@dataclass(frozen=True)
class SqliteVecStatus:
    """The explicit state of the optional sqlite-vec feature."""

    available: bool
    state: str
    reason: str
    extension: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class PurgeResult:
    as_of: datetime
    dry_run: bool
    eligible_observations: int = 0
    eligible_artifacts: int = 0
    expired_observations: int = 0
    deleted_observations: int = 0
    expired_artifacts: int = 0
    deleted_artifacts: int = 0
    skipped_referenced_artifacts: int = 0
    missing_artifact_files: int = 0
    file_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactRecoveryResult:
    removed_staging_files: int = 0
    missing_records: tuple[str, ...] = ()
    corrupt_records: tuple[str, ...] = ()
    orphan_files: tuple[str, ...] = ()
    removed_orphan_files: int = 0


@dataclass(frozen=True)
class JobRecoveryResult:
    recovered_jobs: int = 0
    job_ids: tuple[str, ...] = ()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | str) -> datetime:
    """Return an aware UTC datetime for a datetime or ISO-8601 value."""

    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime or ISO timestamp, got {type(value)!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def timestamp(value: datetime | str) -> str:
    """Serialize timestamps in one stable UTC representation for SQLite."""

    return ensure_utc(value).isoformat(timespec="microseconds")


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return ensure_utc(value)
