"""Lock-in tests for WebSocket session-scoped broadcast.

The contract under test (in ``backend.api.server``):

* ``_broadcast(event, data)``             — global event, every client.
* ``_broadcast(event, data, session_id=)`` — scoped event, ONLY clients
  whose subscription set contains *session_id*.

Previously the empty subscription set fell through to a fan-out that
leaked another tab's events (steps, logs, screenshots, finish, safety)
to unrelated viewers.  These tests pin the new behavior so it cannot
silently regress.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from backend.api import server as srv


class _FakeWS:
    """Minimal stand-in for ``starlette.WebSocket`` used by ``_broadcast``."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.sent.append(msg)


def _events(ws: _FakeWS) -> list[dict]:
    return [json.loads(m) for m in ws.sent]


class TestBroadcastScoping(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Save and clear the real registry so tests are isolated.
        self._saved = dict(srv._ws_clients)
        srv._ws_clients.clear()

    async def asyncTearDown(self) -> None:
        srv._ws_clients.clear()
        srv._ws_clients.update(self._saved)

    async def test_global_event_reaches_all_clients(self):
        a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
        srv._ws_clients[a] = set()
        srv._ws_clients[b] = {"s-1"}
        srv._ws_clients[c] = {"s-2"}

        await srv._broadcast("ping", {"t": 1})

        for ws in (a, b, c):
            self.assertEqual(len(ws.sent), 1)
            self.assertEqual(_events(ws)[0]["event"], "ping")

    async def test_scoped_event_only_to_subscribers(self):
        a = _FakeWS()  # subscribed to s-1
        b = _FakeWS()  # subscribed to s-2
        c = _FakeWS()  # subscribed to both
        srv._ws_clients[a] = {"s-1"}
        srv._ws_clients[b] = {"s-2"}
        srv._ws_clients[c] = {"s-1", "s-2"}

        await srv._broadcast("step", {"x": 1}, session_id="s-1")

        self.assertEqual(len(a.sent), 1)
        self.assertEqual(len(b.sent), 0, "tab subscribed to s-2 must NOT see s-1 event")
        self.assertEqual(len(c.sent), 1)

    async def test_scoped_event_skips_unsubscribed_client(self):
        # Regression lock for the audit finding: a client that has not
        # yet subscribed to ANY session must NOT receive scoped events.
        unsubscribed = _FakeWS()
        srv._ws_clients[unsubscribed] = set()

        await srv._broadcast("screenshot", {"b": "AAA"}, session_id="s-1")

        self.assertEqual(unsubscribed.sent, [],
                         "empty subscription must NOT receive scoped events")

    async def test_starter_client_still_receives_its_own_events(self):
        # UX preservation: the client that started a session and
        # subscribed to it continues to see all of its own events.
        starter = _FakeWS()
        srv._ws_clients[starter] = {"s-mine"}

        await srv._broadcast("log",        {"l": "x"}, session_id="s-mine")
        await srv._broadcast("step",       {"n": 1},   session_id="s-mine")
        await srv._broadcast("screenshot", {"b": "z"}, session_id="s-mine")
        await srv._broadcast("agent_finished", {"status": "ok"}, session_id="s-mine")

        self.assertEqual(len(starter.sent), 4)
        self.assertEqual(
            [e["event"] for e in _events(starter)],
            ["log", "step", "screenshot", "agent_finished"],
        )

    async def test_unrelated_subscriber_does_not_receive_other_session(self):
        a = _FakeWS()
        b = _FakeWS()
        srv._ws_clients[a] = {"alpha"}
        srv._ws_clients[b] = {"beta"}

        await srv._broadcast("step", {"n": 1}, session_id="alpha")
        await srv._broadcast("step", {"n": 2}, session_id="beta")

        self.assertEqual(len(a.sent), 1)
        self.assertEqual(len(b.sent), 1)
        self.assertEqual(_events(a)[0].get("n"), 1)
        self.assertEqual(_events(b)[0].get("n"), 2)

    async def test_disconnected_client_is_pruned(self):
        class _DeadWS(_FakeWS):
            async def send_text(self, msg: str) -> None:  # noqa: ARG002
                raise ConnectionError("peer gone")

        live = _FakeWS()
        dead = _DeadWS()
        srv._ws_clients[live] = {"s-1"}
        srv._ws_clients[dead] = {"s-1"}

        await srv._broadcast("step", {"n": 1}, session_id="s-1")

        self.assertIn(live, srv._ws_clients)
        self.assertNotIn(dead, srv._ws_clients)
        self.assertEqual(len(live.sent), 1)


class TestNewWSConnectionStartsUnsubscribed(unittest.TestCase):
    """The handler must initialise new connections with an empty set."""

    def test_initial_subscription_is_empty(self):
        # Spec check: the documented contract is that fresh clients
        # carry an empty subscription set and receive no scoped events
        # until they explicitly send a ``subscribe`` message.
        import inspect
        src = inspect.getsource(srv.websocket_endpoint)
        self.assertIn("_ws_clients[ws] = set()", src)


if __name__ == "__main__":
    unittest.main()
