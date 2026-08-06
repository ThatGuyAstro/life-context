"""Event-stream primitives used by the API's SSE/WebSocket adapters."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import jsonable


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: Any = None
    id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "data": jsonable(self.data),
            "timestamp": self.timestamp.isoformat(),
        }


def event_to_sse(event: Event | Any) -> str:
    """Encode an event using the SSE wire format."""

    if not isinstance(event, Event):
        event = Event(type="message", data=event)
    payload = json.dumps(jsonable(event.data), ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event.type}", f"data: {payload}"]
    if event.id is not None:
        lines.insert(0, f"id: {event.id}")
    return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class _Subscriber:
    topic: str | None
    queue: asyncio.Queue[Event]
    loop: asyncio.AbstractEventLoop


class EventStreamStub:
    """Small in-memory event source for local wiring and route smoke tests.

    It is deliberately a stub, not a replacement for a durable event bus.
    ``publish`` is useful in tests; production callers can inject their own
    stream implementing the same subscription surface.
    """

    def __init__(
        self,
        initial: Iterable[Event | Any] | None = None,
        *,
        subscriber_queue_size: int = 256,
    ) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        self._subscriber_queue_size = int(subscriber_queue_size)
        self._events: list[Event] = []
        self._waiters: list[_Subscriber] = []
        self._lock = threading.Lock()
        for event in initial or ():
            self.publish(event)

    def publish(self, event: Event | Any, *, event_type: str = "message") -> Event:
        normalized = event if isinstance(event, Event) else Event(type=event_type, data=event)
        with self._lock:
            self._events.append(normalized)
            waiters = tuple(self._waiters)
        for waiter in waiters:
            if waiter.topic is not None and waiter.topic != normalized.type:
                continue
            try:
                waiter.loop.call_soon_threadsafe(self._enqueue, waiter, normalized)
            except RuntimeError:
                self._remove_waiter(waiter)
        return normalized

    def _enqueue(self, waiter: _Subscriber, event: Event) -> None:
        with self._lock:
            if waiter not in self._waiters:
                return
        try:
            waiter.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A live stream is a notification channel, not a durable queue.
            # Drop a new event while bounding memory for a slow client.  This
            # also preserves an item already handed to a waiting consumer.
            return

    def _remove_waiter(self, waiter: _Subscriber) -> None:
        with self._lock:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass

    def snapshot(self, topic: str | None = None) -> tuple[Event, ...]:
        with self._lock:
            if topic is None:
                return tuple(self._events)
            return tuple(event for event in self._events if event.type == topic)

    def subscribe(self, topic: str | None = None) -> tuple[Event, ...]:
        return self.snapshot(topic)

    async def astream(
        self, topic: str | None = None, *, include_existing: bool = True
    ) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        waiter = _Subscriber(topic=topic, queue=queue, loop=loop)
        with self._lock:
            existing = tuple(
                event
                for event in self._events
                if include_existing and (topic is None or event.type == topic)
            )
            # Register before replaying history so a publish between the first
            # replayed event and the live wait cannot be lost.
            self._waiters.append(waiter)
        try:
            for event in existing:
                yield event
            while True:
                event = await queue.get()
                yield event
        finally:
            self._remove_waiter(waiter)


__all__ = ["Event", "EventStreamStub", "event_to_sse"]
