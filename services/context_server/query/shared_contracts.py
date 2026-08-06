"""Best-effort imports for contracts owned by the wider context server.

The repository may provide these contracts in a sibling module in a fuller
checkout.  Agent 14 must not edit that module, so this file only discovers
what is available and leaves the query package usable when the contracts are
not present (as is the case in the minimal checkout used for local tests).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CANDIDATE_MODULES = (
    "contracts",
    "packages.contracts.python.contracts",
    "services.context_server.contracts",
    "services.context_server.shared.contracts",
    "services.context_server.api.contracts",
)


def load_shared_contract(name: str) -> Any | None:
    """Return a shared contract class when one is available.

    Import failures are intentionally ignored: query code only relies on the
    structural protocols in this package and can be bootstrapped before the
    shared contract package is installed.
    """

    for module_name in _CANDIDATE_MODULES:
        try:
            module = import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        contract = getattr(module, name, None)
        if contract is not None:
            return contract
    return None


SharedQueryRequest = load_shared_contract("QueryRequest")
SharedQueryResponse = load_shared_contract("QueryResponse")
SharedSearchHit = load_shared_contract("SearchHit")
SharedObservation = load_shared_contract("ObservationV1")

__all__ = [
    "SharedQueryRequest",
    "SharedQueryResponse",
    "SharedSearchHit",
    "SharedObservation",
    "load_shared_contract",
]
