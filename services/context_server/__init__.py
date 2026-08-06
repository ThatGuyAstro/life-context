"""Foundation package for the context server.

The package intentionally keeps storage, inference, and query concerns out of
the foundation layer.  Those modules can be added later and mounted through
the ``routers`` argument to :func:`create_app`.
"""

from .app import app, create_app
from .config import Settings, get_settings, load_settings

__all__ = [
    "Settings",
    "app",
    "create_app",
    "get_settings",
    "load_settings",
]

__version__ = "0.1.0"
