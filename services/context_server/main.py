"""Uvicorn-compatible entrypoint.

Run with ``uvicorn services.context_server.main:app`` or
``python -m services.context_server`` from the repository root.
"""

from __future__ import annotations

import uvicorn

from .application import app
from .config import get_settings


def main() -> None:
    """Start the application using the process configuration."""

    settings = get_settings()
    uvicorn.run(
        "services.context_server.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=settings.reload,
        log_config=None,
    )


if __name__ == "__main__":
    main()


__all__ = ["app", "main"]
