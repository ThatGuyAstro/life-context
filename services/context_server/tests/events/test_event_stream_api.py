from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.context_server.api.routes import query
from services.context_server.app import create_app
from services.context_server.config import Settings
from services.context_server.query.interfaces import RouteServices
from services.context_server.query.stream import Event, EventStreamStub, event_to_sse

EVENT_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class SnapshotEventStream:
    """Finite event source for response-oriented TestClient assertions."""

    def __init__(self, events: Iterable[Event]) -> None:
        self.events = tuple(events)
        self.requested_topics: list[str | None] = []

    def subscribe(self, topic: str | None = None) -> tuple[Event, ...]:
        self.requested_topics.append(topic)
        if topic is None:
            return self.events
        return tuple(event for event in self.events if event.type == topic)


class DisconnectingEventStream:
    """Async source that attempts one send after the client has disconnected."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()

    async def astream(self, topic: str | None = None):
        del topic
        try:
            yield Event(type="ready", data={"service": "context-server"}, timestamp=EVENT_TIME)
            self.started.set()
            await asyncio.to_thread(self.release.wait, 5)
            yield Event(
                type="context.observation",
                id="context-after-disconnect",
                data={"text": "should not escape the route"},
                timestamp=EVENT_TIME,
            )
        finally:
            self.finished.set()


class BlockingEventStream:
    """Live source used to verify that client close cancels the source task."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.active_tasks: set[asyncio.Task[Any]] = set()

    async def astream(self, topic: str | None = None):
        del topic
        task = asyncio.current_task()
        if task is not None:
            self.active_tasks.add(task)
        try:
            self.started.set()
            yield Event(type="ready", data={"service": "context-server"}, timestamp=EVENT_TIME)
            await asyncio.Event().wait()
        finally:
            if task is not None:
                self.active_tasks.discard(task)
            self.finished.set()


@pytest.fixture
def app_factory(tmp_path):
    def build(stream: Any, **settings_overrides: Any) -> FastAPI:
        app = create_app(
            Settings(
                data_dir=tmp_path,
                api_prefix="/v1",
                docs_enabled=False,
                **settings_overrides,
            ),
            routers=(query.router,),
            include_default_routers=False,
        )
        app.state.context_services = RouteServices(events=stream)
        return app

    return build


def event_set() -> tuple[Event, ...]:
    return (
        Event(type="ready", data={"service": "context-server"}, timestamp=EVENT_TIME),
        Event(
            type="context.observation",
            id="context-1",
            data={"text": "captured context", "source": "test"},
            timestamp=EVENT_TIME,
        ),
        Event(
            type="context.inference",
            id="inference-1",
            data={"summary": "inferred context", "confidence": 0.9},
            timestamp=EVENT_TIME,
        ),
        Event(type="capture.started", data={"mode": "test"}, timestamp=EVENT_TIME),
        Event(type="capture.paused", data={}, timestamp=EVENT_TIME),
    )


def parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            name, value = line.split(": ", 1)
            fields[name] = json.loads(value) if name == "data" else value
        events.append(fields)
    return events


def test_sse_stream_emits_ready_and_context_events(app_factory) -> None:
    stream = SnapshotEventStream(event_set())

    with TestClient(app_factory(stream)) as client:
        response = client.get("/v1/events/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert parse_sse(response.text) == [
        {"event": "ready", "data": {"service": "context-server"}},
        {
            "id": "context-1",
            "event": "context.observation",
            "data": {"text": "captured context", "source": "test"},
        },
        {
            "id": "inference-1",
            "event": "context.inference",
            "data": {"summary": "inferred context", "confidence": 0.9},
        },
        {"event": "capture.started", "data": {"mode": "test"}},
        {"event": "capture.paused", "data": {}},
    ]
    assert stream.requested_topics == [None]


def test_sse_stream_filters_by_topic(app_factory) -> None:
    stream = SnapshotEventStream(event_set())

    with TestClient(app_factory(stream)) as client:
        response = client.get("/v1/events/stream?topic=context.observation")

    assert response.status_code == 200
    assert parse_sse(response.text) == [
        {
            "id": "context-1",
            "event": "context.observation",
            "data": {"text": "captured context", "source": "test"},
        }
    ]
    assert stream.requested_topics == ["context.observation"]


def test_websocket_stream_emits_ready_and_context_events(app_factory) -> None:
    stream = SnapshotEventStream(event_set())

    with TestClient(app_factory(stream)) as client:
        with client.websocket_connect("/v1/events/ws") as websocket:
            messages = [websocket.receive_json() for _ in range(len(event_set()))]

    assert messages == [event.to_dict() for event in event_set()]
    assert stream.requested_topics == [None]


def test_websocket_stream_filters_by_topic(app_factory) -> None:
    stream = SnapshotEventStream(event_set())

    with TestClient(app_factory(stream)) as client:
        with client.websocket_connect("/v1/events/ws?topic=context.observation") as websocket:
            message = websocket.receive_json()

    assert message == event_set()[1].to_dict()
    assert stream.requested_topics == ["context.observation"]


def test_websocket_client_disconnect_is_handled(app_factory) -> None:
    stream = DisconnectingEventStream()

    with TestClient(app_factory(stream)) as client:
        with client.websocket_connect("/v1/events/ws") as websocket:
            assert websocket.receive_json() == event_set()[0].to_dict()
            assert stream.started.wait(timeout=1)
            websocket.close()
            stream.release.set()

    assert stream.finished.wait(timeout=1)


def test_event_stream_registers_before_replaying_history() -> None:
    async def scenario() -> None:
        stream = EventStreamStub(initial=(Event(type="ready", data={"service": "context-server"}),))
        events = stream.astream()
        try:
            assert (await anext(events)).type == "ready"
            published = stream.publish(Event(type="capture.started", data={"mode": "race-test"}))
            assert await asyncio.wait_for(anext(events), timeout=1) == published
        finally:
            await events.aclose()

        assert stream._waiters == []

    asyncio.run(scenario())


def test_event_stream_filters_and_bounds_slow_subscriber_queue() -> None:
    async def scenario() -> None:
        stream = EventStreamStub(subscriber_queue_size=2)
        events = stream.astream("context.inference", include_existing=False)
        first_event = asyncio.create_task(anext(events))
        await asyncio.sleep(0)

        assert len(stream._waiters) == 1
        stream.publish(Event(type="capture.started", data={"mode": "unrelated"}))
        await asyncio.sleep(0)
        assert stream._waiters[0].queue.qsize() == 0

        for index in range(4):
            stream.publish(Event(type="context.inference", data={"index": index}))
        await asyncio.sleep(0)

        assert stream._waiters[0].queue.qsize() <= 2
        assert (await asyncio.wait_for(first_event, timeout=1)).data == {"index": 0}
        assert (await asyncio.wait_for(anext(events), timeout=1)).data == {"index": 1}
        await events.aclose()

        assert stream._waiters == []

    asyncio.run(scenario())


def test_event_stream_accepts_events_published_from_worker_thread() -> None:
    async def scenario() -> None:
        stream = EventStreamStub()
        events = stream.astream("context.inference", include_existing=False)
        pending = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        worker = threading.Thread(
            target=stream.publish,
            args=(Event(type="context.inference", data={"source": "worker"}),),
        )
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        try:
            assert (await asyncio.wait_for(pending, timeout=1)).data == {"source": "worker"}
        finally:
            await events.aclose()

    asyncio.run(scenario())


def test_sse_body_close_closes_underlying_stream(app_factory) -> None:
    async def scenario() -> None:
        stream = BlockingEventStream()
        response = await query.events_route(
            SimpleNamespace(query_params={}), RouteServices(events=stream)
        )
        body = response.body_iterator
        assert await anext(body) == event_to_sse(
            Event(type="ready", data={"service": "context-server"})
        )
        assert stream.started.wait(timeout=1)
        await body.aclose()

        assert stream.finished.wait(timeout=1)
        assert stream.active_tasks == set()

    asyncio.run(scenario())


def test_websocket_close_cancels_stream_without_waiting_for_another_event(app_factory) -> None:
    stream = BlockingEventStream()

    with TestClient(app_factory(stream)) as client:
        with client.websocket_connect("/v1/events/ws") as websocket:
            assert websocket.receive_json() == event_set()[0].to_dict()
            assert stream.started.wait(timeout=1)
            websocket.close()

        assert stream.finished.wait(timeout=1)

    assert stream.active_tasks == set()


def test_remote_websocket_requires_bearer_auth_and_allows_wss_with_token(app_factory) -> None:
    stream = SnapshotEventStream(event_set())
    app = app_factory(
        stream,
        host="remote.example.test",
        auth_token="remote-event-token",
        require_https_remote=True,
    )

    with TestClient(app, base_url="https://remote.example.test") as client:
        response = client.get(
            "/v1/events/stream",
            headers={
                "Accept": "text/event-stream",
                "Authorization": "Bearer remote-event-token",
            },
        )
        assert response.status_code == 200
        assert "event: ready" in response.text

    with TestClient(app, base_url="https://remote.example.test") as client:
        for headers in ({}, {"Authorization": "Bearer incorrect-token"}):
            with pytest.raises(Exception) as error:
                with client.websocket_connect(
                    "wss://remote.example.test/v1/events/ws", headers=headers
                ):
                    pass
            assert getattr(error.value, "status_code", None) == 401

        with client.websocket_connect(
            "wss://remote.example.test/v1/events/ws",
            headers={"Authorization": "Bearer remote-event-token"},
        ) as websocket:
            assert websocket.receive_json() == event_set()[0].to_dict()

    with TestClient(app, base_url="http://remote.example.test") as client:
        with pytest.raises(Exception) as error:
            with client.websocket_connect(
                "ws://remote.example.test/v1/events/ws",
                headers={"Authorization": "Bearer remote-event-token"},
            ):
                pass
        assert getattr(error.value, "status_code", None) == 400
