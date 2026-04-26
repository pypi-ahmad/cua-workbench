"""Focused tests for WebSocket Origin validation and token authentication.

Covers both ``/ws`` (event stream) and ``/vnc/websockify`` (noVNC proxy):

  1) Token issuance endpoint returns a non-empty token + TTL.
  2) WS connection without a token is rejected.
  3) WS connection with an invalid token is rejected.
  4) WS connection with a foreign Origin is rejected.
  5) WS connection with a same-origin Origin + valid token succeeds (/ws).
  6) Tokens are single-use (second connect fails).
  7) Token helpers reject empty/None/expired values.
  8) Origin helper accepts only the dev allowlist.
"""

from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api import server as srv


TESTCLIENT_OWNER = "testclient"
SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"


def _seed_owned_session(session_id: str, *, owner: str = TESTCLIENT_OWNER) -> None:
    srv._active_loops[session_id] = object()
    srv._session_owners[session_id] = owner


def _client() -> TestClient:
    # raise_server_exceptions=False so handler-internal failures don't
    # mask the WS handshake assertions we're making.
    return TestClient(srv.app, raise_server_exceptions=False)


class _WsStateCase(unittest.TestCase):
    def setUp(self):
        srv._ws_tokens.clear()
        srv._ws_clients.clear()
        srv._ws_pending_tokens.clear()
        srv._active_loops.clear()
        srv._session_owners.clear()

    def tearDown(self):
        srv._ws_tokens.clear()
        srv._ws_clients.clear()
        srv._ws_pending_tokens.clear()
        srv._active_loops.clear()
        srv._session_owners.clear()


class TestWsTokenIssuance(_WsStateCase):
    def test_issue_endpoint_returns_token_and_ttl(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            resp = c.post("/api/session/ws-token", json={"session_id": SESSION_A})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIn("token", body)
            self.assertIn("ttl_seconds", body)
            self.assertTrue(isinstance(body["token"], str) and len(body["token"]) >= 32)
            self.assertGreater(body["ttl_seconds"], 0)
            self.assertEqual(srv._ws_tokens[body["token"]].session_id, SESSION_A)


class TestWsTokenHelpers(_WsStateCase):

    def test_consume_rejects_none(self):
        self.assertFalse(srv._consume_ws_token(None))

    def test_consume_rejects_empty(self):
        self.assertFalse(srv._consume_ws_token(""))

    def test_consume_rejects_unknown(self):
        self.assertFalse(srv._consume_ws_token("not-a-real-token"))

    def test_issue_then_consume_succeeds_once(self):
        token = srv._issue_ws_token(SESSION_A)
        self.assertTrue(srv._consume_ws_token(token))
        # Second consumption fails — single-use.
        self.assertFalse(srv._consume_ws_token(token))

    def test_expired_token_rejected(self):
        token = srv._issue_ws_token(SESSION_A)
        # Force expiration by rewriting issuance timestamp.
        record = srv._ws_tokens[token]
        srv._ws_tokens[token] = type(record)(
            issued_at=time.monotonic() - (srv._WS_TOKEN_TTL_SECONDS + 5),
            session_id=record.session_id,
        )
        self.assertFalse(srv._consume_ws_token(token))


class TestOriginHelper(unittest.TestCase):
    def test_allows_dev_localhost_5173(self):
        self.assertTrue(srv._is_allowed_ws_origin("http://localhost:5173"))

    def test_allows_dev_127_0_0_1_5173(self):
        self.assertTrue(srv._is_allowed_ws_origin("http://127.0.0.1:5173"))

    def test_allows_missing_origin_for_non_browser_clients(self):
        # F-052 was the missing token-to-session binding, not the empty-Origin
        # allowance itself. curl/tests/native clients still omit Origin, but
        # they now need a session-bound token and cannot subscribe elsewhere.
        self.assertTrue(srv._is_allowed_ws_origin(None))
        self.assertTrue(srv._is_allowed_ws_origin(""))

    def test_rejects_foreign_origin(self):
        self.assertFalse(srv._is_allowed_ws_origin("https://evil.example"))

    def test_rejects_wrong_port(self):
        self.assertFalse(srv._is_allowed_ws_origin("http://localhost:9999"))

    def test_rejects_https_local(self):
        # Only HTTP dev origins are allowed by current allowlist.
        self.assertFalse(srv._is_allowed_ws_origin("https://localhost:5173"))


class TestWsRejection(_WsStateCase):
    """Connections without a valid token must be rejected before accept."""

    def test_ws_no_token_rejected(self):
        with _client() as c, self.assertRaises(WebSocketDisconnect):
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()

    def test_ws_bogus_token_rejected(self):
        with _client() as c, self.assertRaises(WebSocketDisconnect):
            with c.websocket_connect("/ws?token=not-real") as ws:
                ws.receive_text()

    def test_ws_foreign_origin_rejected(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with self.assertRaises(WebSocketDisconnect):
                with c.websocket_connect(
                    f"/ws?token={token}",
                    headers={"origin": "https://evil.example"},
                ) as ws:
                    ws.receive_text()

    def test_vnc_ws_no_token_rejected(self):
        with _client() as c, self.assertRaises(WebSocketDisconnect):
            with c.websocket_connect("/vnc/websockify") as ws:
                ws.receive_text()

    def test_vnc_ws_foreign_origin_rejected(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with self.assertRaises(WebSocketDisconnect):
                with c.websocket_connect(
                    f"/vnc/websockify?token={token}",
                    headers={"origin": "https://attacker.test"},
                ) as ws:
                    ws.receive_text()


class TestWsAcceptance(_WsStateCase):
    """Same-origin + valid token must connect successfully on /ws."""

    def test_ws_valid_token_no_origin_accepts(self):
        # Non-browser client (no Origin) + valid token → accepted.
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with c.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_text('{"type":"ping"}')
                msg = ws.receive_text()
                self.assertIn("pong", msg)

    def test_ws_valid_token_dev_origin_accepts(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with c.websocket_connect(
                f"/ws?token={token}",
                headers={"origin": "http://localhost:5173"},
            ) as ws:
                ws.send_text('{"type":"ping"}')
                msg = ws.receive_text()
                self.assertIn("pong", msg)

    def test_ws_subscribe_accepts_bound_session_and_consumes_token(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with c.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_json({"type": "subscribe", "session_id": SESSION_A})
                ws.send_json({"type": "ping"})
                self.assertEqual(ws.receive_json()["event"], "pong")
                self.assertNotIn(token, srv._ws_tokens)
                self.assertIn(SESSION_A, next(iter(srv._ws_clients.values())))

    def test_ws_subscribe_mismatch_closes_1008(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with c.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_json({"type": "subscribe", "session_id": SESSION_B})
                with self.assertRaises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                self.assertEqual(exc_info.exception.code, 1008)

    def test_ws_token_is_single_use(self):
        _seed_owned_session(SESSION_A)
        with _client() as c:
            token = c.post("/api/session/ws-token", json={"session_id": SESSION_A}).json()["token"]
            with c.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_json({"type": "subscribe", "session_id": SESSION_A})
                ws.send_json({"type": "ping"})
                ws.receive_json()
            # Reusing the same token must fail.
            with self.assertRaises(WebSocketDisconnect):
                with c.websocket_connect(f"/ws?token={token}") as ws:
                    ws.receive_text()


if __name__ == "__main__":
    unittest.main()
