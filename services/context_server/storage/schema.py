"""Compatibility-facing schema exports.

The implementation lives in :mod:`migrations`; this module gives callers a
stable place to import the schema version and migration runner from.
"""

from .migrations import MIGRATIONS, SCHEMA_VERSION, apply_migrations, current_schema_version

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "apply_migrations", "current_schema_version"]

