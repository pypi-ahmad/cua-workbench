"""Tests for execution_target field, routing, and guards.

Covers:
- StartTaskRequest accepts execution_target ("local" / "docker")
- MCP docker strategy dispatches execute_mcp_action_docker
- Accessibility local dispatches execute_accessibility_action directly
- Computer-use + local returns 400 (server guard)
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import StartTaskRequest


# ── Model tests ────────────────────────────────────────────────────────────────

class TestStartTaskRequestExecutionTarget:
    """execution_target field on StartTaskRequest."""

    def test_default_is_local(self):
        req = StartTaskRequest(
            task="test task",
            mode="browser",
            engine="playwright_mcp",
            provider="google",
        )
        assert req.execution_target == "local"

    def test_accepts_docker(self):
        req = StartTaskRequest(
            task="test task",
            mode="desktop",
            engine="computer_use",
            provider="google",
            execution_target="docker",
        )
        assert req.execution_target == "docker"

    def test_accepts_local_explicit(self):
        req = StartTaskRequest(
            task="test task",
            mode="browser",
            engine="playwright_mcp",
            provider="google",
            execution_target="local",
        )
        assert req.execution_target == "local"

    def test_field_max_length(self):
        """execution_target has max_length=20, so a long string should be rejected."""
        with pytest.raises(Exception):
            StartTaskRequest(
                task="test",
                mode="browser",
                engine="playwright_mcp",
                provider="google",
                execution_target="x" * 21,
            )


# ── MCP docker routing tests ──────────────────────────────────────────────────

class TestMCPDockerRouting(unittest.IsolatedAsyncioTestCase):
    """When execution_target='docker', execute_action should call execute_mcp_action_docker."""

    async def test_mcp_docker_dispatches_docker_function(self):
        """playwright_mcp + execution_target=docker → execute_mcp_action_docker."""
        mock_result = {"success": True, "engine": "playwright_mcp"}

        with patch("backend.agent.playwright_mcp_client.execute_mcp_action_docker", new_callable=AsyncMock, return_value=mock_result) as mock_docker, \
             patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock) as mock_local:
            from backend.agent.executor import execute_action
            result = await execute_action(
                action={"action": "browser_click", "target": "button"},
                engine="playwright_mcp",
                step=1,
                execution_target="docker",
            )
            mock_docker.assert_called_once()
            mock_local.assert_not_called()
            assert result.get("engine") == "playwright_mcp"

    async def test_mcp_local_dispatches_local_function(self):
        """playwright_mcp + execution_target=local → execute_mcp_action (STDIO)."""
        mock_result = {"success": True, "engine": "playwright_mcp"}

        with patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock, return_value=mock_result) as mock_local, \
             patch("backend.agent.playwright_mcp_client.execute_mcp_action_docker", new_callable=AsyncMock) as mock_docker:
            from backend.agent.executor import execute_action
            await execute_action(
                action={"action": "browser_click", "target": "button"},
                engine="playwright_mcp",
                step=1,
                execution_target="local",
            )
            mock_local.assert_called_once()
            mock_docker.assert_not_called()


# ── Accessibility local routing tests ──────────────────────────────────────────

class TestAccessibilityLocalRouting(unittest.IsolatedAsyncioTestCase):
    """When execution_target='local', omni_accessibility dispatches to local provider."""

    async def test_a11y_local_calls_execute_accessibility_action(self):
        """omni_accessibility + local → execute_accessibility_action (platform-native)."""
        mock_result = {"success": True, "message": "clicked"}

        with patch(
            "backend.engines.accessibility_engine.execute_accessibility_action",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_a11y:
            from backend.agent.executor import execute_action
            result = await execute_action(
                action={"action": "click", "target": "OK button"},
                engine="omni_accessibility",
                step=1,
                execution_target="local",
            )
            mock_a11y.assert_called_once()
            assert result.get("engine") == "omni_accessibility"
            assert result.get("success") is True

    async def test_a11y_docker_routes_through_agent_service(self):
        """omni_accessibility + docker → HTTP call to agent_service inside container."""
        mock_result = {"success": True}

        with patch(
            "backend.agent.executor._send_with_retry",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_send:
            from backend.agent.executor import execute_action
            result = await execute_action(
                action={"action": "click", "target": "OK button"},
                engine="omni_accessibility",
                step=1,
                execution_target="docker",
            )
            mock_send.assert_called_once()
            payload = mock_send.call_args[0][0]
            assert payload["mode"] == "omni_accessibility"
            assert result.get("engine") == "omni_accessibility"


# ── CU local guard (server-level 400) ─────────────────────────────────────────

class TestCULocalGuard:
    """computer_use + execution_target='local' should be blocked at the API level."""

    def test_cu_local_returns_400(self):
        """POST /api/agent/start with engine=computer_use, execution_target=local → 400."""
        from fastapi.testclient import TestClient
        from backend.api.server import app

        client = TestClient(app)
        resp = client.post("/api/agent/start", json={
            "task": "do something",
            "api_key": "test-key-12345678",
            "model": "gemini-3-flash-preview",
            "max_steps": 5,
            "mode": "desktop",
            "engine": "computer_use",
            "provider": "google",
            "execution_target": "local",
        })
        assert resp.status_code == 400
        data = resp.json()
        # Error message uses user-facing wording.  Assert on the invariant
        # tokens ("Computer Use" / "Docker") rather than an exact string.
        err = data["error"].lower()
        assert "computer use" in err or "computer_use" in err
        assert "docker" in err

    def test_cu_docker_not_blocked(self):
        """POST /api/agent/start with engine=computer_use, execution_target=docker
        should NOT be blocked by the guard (may fail later for other reasons)."""
        from fastapi.testclient import TestClient
        from backend.api.server import app

        client = TestClient(app)
        resp = client.post("/api/agent/start", json={
            "task": "do something",
            "api_key": "test-key-12345678",
            "model": "gemini-3-flash-preview",
            "max_steps": 5,
            "mode": "desktop",
            "engine": "computer_use",
            "provider": "google",
            "execution_target": "docker",
        })
        # Should NOT be 400 — the guard only blocks local
        assert resp.status_code != 400
