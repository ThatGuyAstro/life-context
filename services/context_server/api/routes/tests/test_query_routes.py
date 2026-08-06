from __future__ import annotations

import unittest

from services.context_server.api.routes.query import (
    RouteServices,
    handle_config,
    handle_observation,
    handle_pause,
    handle_purge,
    handle_query,
    handle_start,
    handle_status,
    handle_timeline,
    timeline_route,
)


class Query:
    async def aquery(self, request):
        return {"query": request.query, "answer": {"text": "ok"}, "evidence": []}


class Timeline:
    def timeline(self, options):
        return {"items": [], "options": dict(options)}


class Observations:
    def observe(self, observation):
        return {"stored": observation}


class Lifecycle:
    def __init__(self):
        self.running = False

    def start(self, options):
        self.running = True
        return {"running": self.running, "options": options}

    def pause(self):
        self.running = False
        return {"running": self.running}

    def status(self):
        return {"running": self.running}


class Purge:
    def purge(self, options):
        return {"purged": options}


class Config:
    def config(self, operation):
        return {"config": operation}


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.services = RouteServices(
            query=Query(),
            timeline=Timeline(),
            observation=Observations(),
            lifecycle=Lifecycle(),
            status=None,
            purge=Purge(),
            config=Config(),
        )

    async def test_query_timeline_observation_and_status(self):
        query = await handle_query({"query": "hello"}, self.services)
        timeline = await handle_timeline({"limit": "2"}, self.services)
        observation = await handle_observation({"text": "hello"}, self.services)
        status = await handle_status(self.services)
        self.assertEqual(query["query"], "hello")
        self.assertEqual(timeline["options"], {"limit": "2"})
        self.assertEqual(observation["stored"], {"text": "hello"})
        self.assertEqual(status, {"running": False})

    async def test_lifecycle_purge_config_and_direct_fastapi_route_shape(self):
        self.assertEqual((await handle_start({"mode": "live"}, self.services))["running"], True)
        self.assertEqual((await handle_pause(self.services))["running"], False)
        self.assertEqual((await handle_purge({"before": "2026-01-01"}, self.services))["purged"]["before"], "2026-01-01")
        self.assertEqual((await handle_config({"operation": "get"}, self.services))["config"]["operation"], "get")
        # With FastAPI installed this endpoint normally receives Request; the
        # mapping branch keeps it directly callable in contract tests too.
        result = await timeline_route({"limit": "1"}, self.services)
        self.assertEqual(result["options"], {"limit": "1"})
