"""Foundation route modules.

The package deliberately does not import or reserve a query route.  Query
endpoints can be supplied later as an ordinary external ``APIRouter``.
"""

from . import capabilities, health, status

__all__ = ["capabilities", "health", "status"]
