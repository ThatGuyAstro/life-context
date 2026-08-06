"""Compatibility imports for optional sqlite-vec integration."""

from .vector import (
    VectorFeatureUnavailable,
    create_vector_index,
    load_sqlite_vec,
    status_from_connection,
)

__all__ = [
    "VectorFeatureUnavailable",
    "create_vector_index",
    "load_sqlite_vec",
    "status_from_connection",
]

