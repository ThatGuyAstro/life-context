"""Best-effort sqlite-vec loading with an explicit unavailable state."""

from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import SqliteVecStatus, timestamp, utc_now


def _record_status(
    connection: sqlite3.Connection,
    status: SqliteVecStatus,
) -> SqliteVecStatus:
    connection.execute(
        """
        INSERT INTO storage_features(feature, state, reason, checked_at)
        VALUES ('sqlite_vec', ?, ?, ?)
        ON CONFLICT(feature) DO UPDATE SET
            state = excluded.state,
            reason = excluded.reason,
            checked_at = excluded.checked_at
        """,
        (status.state, status.reason, timestamp(utc_now())),
    )
    return status


def load_sqlite_vec(
    connection: sqlite3.Connection,
    extension_path: str | Path | None = None,
    *,
    enabled: bool = True,
) -> SqliteVecStatus:
    """Try to load sqlite-vec and never make it a required dependency.

    The preferred path is the official ``sqlite-vec`` Python package, whose
    ``loadable_path()`` points at the platform extension.  A caller can pass a
    path explicitly for a packaged DLL/SO.  Both a missing package and a load
    error are persisted in ``storage_features`` so operators can distinguish
    an unavailable vector index from a silently skipped one.
    """

    if not enabled:
        return _record_status(
            connection,
            SqliteVecStatus(
                available=False,
                state="disabled",
                reason="sqlite-vec loading disabled by configuration",
            ),
        )

    extension: str | None = str(extension_path) if extension_path is not None else None
    module: Any = None
    if extension_path is None:
        try:
            module = importlib.import_module("sqlite_vec")
        except ModuleNotFoundError:
            return _record_status(
                connection,
                SqliteVecStatus(
                    available=False,
                    state="unavailable",
                    reason="sqlite_vec package is not installed",
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive import boundary
            return _record_status(
                connection,
                SqliteVecStatus(
                    available=False,
                    state="unavailable",
                    reason=f"sqlite_vec import failed: {exc}",
                ),
            )
        try:
            extension = str(module.loadable_path())
        except Exception as exc:
            return _record_status(
                connection,
                SqliteVecStatus(
                    available=False,
                    state="unavailable",
                    reason=f"sqlite_vec.loadable_path() failed: {exc}",
                ),
            )

    if not hasattr(connection, "enable_load_extension"):
        return _record_status(
            connection,
            SqliteVecStatus(
                available=False,
                state="unavailable",
                reason="Python SQLite was built without extension loading support",
                extension=extension,
            ),
        )

    try:
        connection.enable_load_extension(True)
        connection.load_extension(extension or "sqlite_vec")
    except Exception as exc:
        return _record_status(
            connection,
            SqliteVecStatus(
                available=False,
                state="unavailable",
                reason=f"sqlite-vec extension could not be loaded: {exc}",
                extension=extension,
            ),
        )
    finally:
        try:
            connection.enable_load_extension(False)
        except Exception:
            pass

    version: str | None = None
    try:
        row = connection.execute("SELECT vec_version() AS version").fetchone()
        if row is not None:
            version = str(row[0])
    except sqlite3.Error:
        # Some older builds load successfully but expose no vec_version().
        version = None
    return _record_status(
        connection,
        SqliteVecStatus(
            available=True,
            state="available",
            reason="",
            extension=extension,
            version=version,
        ),
    )


def status_from_connection(connection: sqlite3.Connection) -> SqliteVecStatus:
    try:
        row = connection.execute(
            "SELECT state, reason FROM storage_features WHERE feature = 'sqlite_vec'"
        ).fetchone()
    except sqlite3.Error as exc:
        return SqliteVecStatus(
            False,
            "unavailable",
            f"sqlite-vec status could not be read: {exc}",
        )
    if row is None:
        return SqliteVecStatus(False, "unavailable", "sqlite-vec status has not been checked")
    return SqliteVecStatus(
        available=row[0] == "available",
        state=str(row[0]),
        reason=str(row[1] or ""),
    )


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VectorFeatureUnavailable(RuntimeError):
    """Raised when a vector index is requested without sqlite-vec."""


def create_vector_index(
    connection: sqlite3.Connection,
    table_name: str,
    dimensions: int,
) -> None:
    """Create a sqlite-vec ``vec0`` table after validating availability."""

    status = status_from_connection(connection)
    if not status.available:
        raise VectorFeatureUnavailable(status.reason or "sqlite-vec is unavailable")
    if not _IDENTIFIER.fullmatch(table_name):
        raise ValueError("vector table name must be a simple SQLite identifier")
    if dimensions <= 0:
        raise ValueError("vector dimensions must be positive")
    connection.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0(embedding float[{int(dimensions)}])"
    )


__all__ = [
    "VectorFeatureUnavailable",
    "create_vector_index",
    "load_sqlite_vec",
    "status_from_connection",
]
