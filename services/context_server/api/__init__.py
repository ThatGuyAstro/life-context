"""HTTP API assembly for the context server foundation."""

from .router import default_routers, register_routers

__all__ = ["default_routers", "register_routers"]
