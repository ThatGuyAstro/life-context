"""Retention policy helpers and the public purge function."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .models import RetentionPolicy, RetentionTier, ensure_utc

if TYPE_CHECKING:
    from .database import SQLiteStorage


def expiration_for(
    tier: RetentionTier | str,
    created_at: datetime,
    policy: RetentionPolicy | None = None,
) -> datetime | None:
    """Calculate the expiration timestamp for a retention tier."""

    return (policy or RetentionPolicy()).expires_at(tier, ensure_utc(created_at))


def purge_expired(
    storage: SQLiteStorage,
    *,
    as_of: datetime | str | None = None,
    dry_run: bool = False,
    before: datetime | str | None = None,
    date: datetime | str | None = None,
    source: str | Iterable[str] | None = None,
    project: str | Iterable[str] | None = None,
    retention_tiers: Any = None,
    retention_tier: Any = None,
):
    """Purge eligible observations/artifacts through a storage instance."""

    return storage.purge_expired(
        as_of=as_of,
        dry_run=dry_run,
        before=before,
        date=date,
        source=source,
        project=project,
        retention_tiers=retention_tiers,
        retention_tier=retention_tier,
    )


__all__ = ["RetentionPolicy", "RetentionTier", "expiration_for", "purge_expired"]
