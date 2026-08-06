"""Small sync/async and dependency-invocation helpers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def _run_coroutine(coroutine: Awaitable[Any]) -> Any:
    """Resolve an awaitable from sync code, including inside a live loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # A synchronous caller can be invoked from an async test or host.  Running
    # a short coroutine in a worker avoids the illegal nested-loop error while
    # keeping the public sync API useful.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="context-query") as pool:
        return pool.submit(asyncio.run, coroutine).result()


def resolve(value: Any) -> Any:
    return _run_coroutine(value) if inspect.isawaitable(value) else value


async def aresolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def get_callable(target: Any, names: tuple[str, ...]) -> Callable[..., Any] | None:
    if target is None:
        return None
    if callable(target) and not any(hasattr(target, name) for name in names):
        return target
    for name in names:
        candidate = getattr(target, name, None)
        if callable(candidate):
            return candidate
    return None


def call_compatible(
    function: Callable[..., Any],
    request: Any,
    *,
    query: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> Any:
    """Call common backend shapes without coupling to a concrete adapter.

    Preferred shape is ``fn(request)``.  The fallback shapes support small
    adapters that expose ``fn(query, limit=..., filters=...)`` or
    ``fn(options)``.  A TypeError raised *inside* a correctly-shaped adapter
    is not hidden when signature inspection gives us enough information.
    """

    options = dict(options or {})
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        parameters = list(signature.parameters.values())
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        first_name = positional[0].name.lower() if positional else ""
        request_names = {"request", "req", "query_request", "spec", "params", "payload"}
        query_names = {"query", "q", "text", "query_text", "term"}
        option_names = {"options", "filters", "criteria"}
        if not positional:
            return function()
        if first_name in request_names or first_name not in query_names:
            required_after_first = [
                parameter
                for parameter in positional[1:]
                if parameter.default is inspect.Parameter.empty
            ]
            evidence_parameter = signature.parameters.get("evidence")
            if evidence_parameter is not None and "evidence" in options:
                if evidence_parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                    return function(request, evidence=options["evidence"])
                return function(request, options["evidence"])
            if required_after_first and "evidence" in options:
                return function(request, options["evidence"])
            return function(request)
        if first_name in option_names:
            return function(options)
        if query is not None:
            accepted = {parameter.name for parameter in parameters}
            kwargs = {}
            for key, value in options.items():
                if key in accepted:
                    kwargs[key] = value
            if "limit" in accepted and "limit" not in kwargs:
                kwargs["limit"] = options.get("limit")
            if "filters" in accepted and "filters" not in kwargs:
                kwargs["filters"] = options.get("filters", {})
            try:
                return function(query, **kwargs)
            except TypeError:
                # A one-argument query function is a common and safe fallback.
                if len(positional) == 1:
                    return function(query)
                raise
    try:
        return function(request)
    except TypeError:
        if query is not None:
            return function(query)
        return function(options)


async def acall_compatible(
    function: Callable[..., Any],
    request: Any,
    *,
    query: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> Any:
    return await aresolve(call_compatible(function, request, query=query, options=options))


def first_callable(target: Any, names: tuple[str, ...]) -> Callable[..., Any] | None:
    return get_callable(target, names)


__all__ = [
    "acall_compatible",
    "aresolve",
    "call_compatible",
    "first_callable",
    "get_callable",
    "resolve",
]
