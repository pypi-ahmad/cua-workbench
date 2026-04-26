from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.api import server as srv


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self._response


class TestApiErrorResponses(unittest.TestCase):
    def setUp(self):
        srv._active_loops.clear()
        srv._active_tasks.clear()
        srv._session_owners.clear()
        srv._key_validate_limiter._buckets.clear()
        self.client = TestClient(srv.app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        srv._active_loops.clear()
        srv._active_tasks.clear()
        srv._session_owners.clear()
        srv._key_validate_limiter._buckets.clear()

    def test_stop_rejects_invalid_uuid_with_400(self):
        resp = self.client.post("/api/agent/stop/not-a-uuid")

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "Invalid session_id")

    def test_status_missing_session_is_404(self):
        resp = self.client.get("/api/agent/status/00000000-0000-0000-0000-000000000000")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "Session not found")

    def test_validate_invalid_google_key_is_422(self):
        fake_client = _FakeAsyncClient(_FakeResponse(403))

        with mock.patch.object(srv.httpx, "AsyncClient", return_value=fake_client):
            resp = self.client.post(
                "/api/keys/validate",
                json={"provider": "google", "api_key": "AIzaSyNotRealKey"},
            )

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "Invalid Google API key")