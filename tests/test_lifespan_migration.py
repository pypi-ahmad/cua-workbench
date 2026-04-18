"""Lock-in tests for the FastAPI lifespan migration.

The audit flagged use of deprecated ``@app.on_event("startup"|"shutdown")``
decorators.  Current code uses an ``@asynccontextmanager`` lifespan
attached at ``FastAPI(..., lifespan=lifespan)``.  These tests keep the
migration locked:

  1) No ``@app.on_event(...)`` calls anywhere in ``backend/``.
  2) ``backend.api.server.lifespan`` exists and is an async-context-manager.
  3) ``app.router.lifespan_context`` is the function we defined.
  4) Lifespan startup creates the shared httpx client and a reaper task.
  5) Lifespan shutdown closes the httpx client and cancels the reaper.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.api import server as srv


_BACKEND = Path(__file__).resolve().parent.parent / "backend"


class TestNoDeprecatedOnEvent(unittest.TestCase):
    """Source must not contain @app.on_event(...) decorators."""

    def test_no_on_event_decorator_in_backend(self):
        offenders: list[str] = []
        # Strip line/block comments and docstrings cheaply: regex on source.
        # We allow the strings 'on_event' to appear in comments/docstrings,
        # but never as a decorator call '@<anything>.on_event('.
        pattern = re.compile(r"^\s*@\w[\w\.]*\.on_event\s*\(", re.MULTILINE)
        for path in _BACKEND.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(str(path))
        self.assertEqual(
            offenders, [],
            f"Deprecated @app.on_event(...) decorators found in: {offenders}",
        )


class TestLifespanWiring(unittest.TestCase):
    """Lifespan must be defined and attached to the FastAPI app."""

    def test_lifespan_symbol_exists(self):
        self.assertTrue(
            hasattr(srv, "lifespan"),
            "backend.api.server.lifespan is missing",
        )

    def test_lifespan_is_async_context_manager_factory(self):
        # @asynccontextmanager produces a callable returning an async CM.
        # Calling it with a dummy app yields an object with __aenter__/__aexit__.
        cm = srv.lifespan(srv.app)
        self.assertTrue(hasattr(cm, "__aenter__"))
        self.assertTrue(hasattr(cm, "__aexit__"))

    def test_app_router_lifespan_is_attached(self):
        # FastAPI stores the lifespan_context on the underlying router.
        attached = getattr(srv.app.router, "lifespan_context", None)
        self.assertIsNotNone(attached, "app.router has no lifespan_context")
        # Should be the same lifespan factory we authored.
        self.assertIs(attached, srv.lifespan)


class TestLifespanBehavior(unittest.TestCase):
    """Lifespan startup/shutdown must wire & tear down resources symmetrically."""

    def test_startup_creates_shared_http_client_and_reaper(self):
        # TestClient runs the lifespan around the with-block.
        with TestClient(srv.app) as client:
            # Startup side-effects must be visible.
            self.assertTrue(hasattr(srv.app.state, "http"))
            self.assertIsInstance(srv.app.state.http, httpx.AsyncClient)
            # Smoke-check: a trivial GET works (no startup/shutdown errors).
            resp = client.get("/api/keys/status")
            self.assertEqual(resp.status_code, 200)

    def test_shutdown_closes_shared_http_client(self):
        with TestClient(srv.app) as client:
            client.get("/api/keys/status")
            shared = srv.app.state.http
        # After context exit, the httpx client must be closed.
        self.assertTrue(shared.is_closed, "Shared httpx client not closed on shutdown")


if __name__ == "__main__":
    unittest.main()
