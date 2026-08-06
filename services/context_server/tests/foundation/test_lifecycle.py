from __future__ import annotations

import asyncio

import pytest

from services.context_server.lifecycle import Lifecycle


def test_lifecycle_runs_hooks_and_reverses_shutdown_order() -> None:
    events: list[str] = []

    async def startup_async() -> None:
        await asyncio.sleep(0)
        events.append("startup-async")

    def startup_sync() -> None:
        events.append("startup-sync")

    def shutdown_first() -> None:
        events.append("shutdown-first")

    async def shutdown_second() -> None:
        await asyncio.sleep(0)
        events.append("shutdown-second")

    lifecycle = Lifecycle(
        startup_hooks=[startup_async, startup_sync],
        shutdown_hooks=[shutdown_first, shutdown_second],
    )

    async def exercise() -> None:
        await lifecycle.startup()
        assert lifecycle.state.ready is True
        await lifecycle.shutdown()

    asyncio.run(exercise())

    assert events == [
        "startup-async",
        "startup-sync",
        "shutdown-second",
        "shutdown-first",
    ]
    assert lifecycle.state.status == "stopped"
    assert lifecycle.state.ready is False


def test_startup_failure_is_visible_in_state() -> None:
    def fail() -> None:
        raise RuntimeError("dependency unavailable")

    lifecycle = Lifecycle(startup_hooks=[fail])

    with pytest.raises(RuntimeError, match="dependency unavailable"):
        asyncio.run(lifecycle.startup())

    assert lifecycle.state.status == "failed"
    assert lifecycle.state.ready is False
    assert lifecycle.state.last_error == "dependency unavailable"
