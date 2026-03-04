"""Tests for strict engine isolation.

Validates that:
- Engine selection is user-controlled only (no backend overrides)
- Router is pure dispatch (no intelligent routing)
- No fallback or cross-engine switching
- Engine validation rejects invalid values
- StartTaskRequest requires explicit engine (no default)
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ActionType, AgentAction, StartTaskRequest
from backend.tools.router import (
    SUPPORTED_ENGINES,
    InvalidEngineError,
    validate_engine,
)
from backend.agent.executor import execute_action


# ── Router tests ──────────────────────────────────────────────────────────────


class TestValidateEngine:
    """validate_engine must accept only SUPPORTED_ENGINES, nothing else."""

    @pytest.mark.parametrize("engine", sorted(SUPPORTED_ENGINES))
    def test_valid_engine_passes(self, engine: str):
        assert validate_engine(engine) == engine

    @pytest.mark.parametrize("bad", [
        "", "Playwright", "PLAYWRIGHT", "magic", "hybrid",
        "playwright_mcp_v2", "xdotool2", " xdotool", "playwright ",
    ])
    def test_invalid_engine_raises(self, bad: str):
        with pytest.raises(InvalidEngineError):
            validate_engine(bad)

    def test_supported_engines_set_is_complete(self):
        expected = {"playwright_mcp", "omni_accessibility", "computer_use"}
        assert SUPPORTED_ENGINES == expected


# ── Router no longer has select_engine ────────────────────────────────────────


class TestNoIntelligentRouting:
    """The router module must NOT export any select_engine function."""

    def test_select_engine_removed(self):
        import backend.tools.router as router_mod
        assert not hasattr(router_mod, "select_engine"), \
            "select_engine still exists — intelligent routing has not been removed"


# ── Executor engine dispatch isolation ────────────────────────────────────────


class TestExecutorEngineIsolation:
    """execute_action must use ONLY the engine it receives — no override."""

    def test_invalid_engine_rejected(self):
        action = AgentAction(action=ActionType.CLICK, coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="nonexistent"))
        assert not result["success"]
        assert "Unsupported engine" in result["message"]

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_mcp_stays_mcp(self, mock_mcp):
        """A playwright_mcp session must NOT fall back to playwright on failure."""
        mock_mcp.return_value = {"success": False, "message": "MCP error"}
        action = AgentAction(action=ActionType.CLICK, target="#submit-btn", coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="playwright_mcp", mode="browser"))
        # Must NOT succeed via fallback — the failure propagates
        assert not result["success"]
        assert result["engine"] == "playwright_mcp"
        mock_mcp.assert_called_once()

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_accessibility_stays_accessibility(self, mock_send):
        """An accessibility session must NOT fall back to xdotool on failure."""
        mock_send.return_value = {"success": False, "message": "AT-SPI error"}
        action = AgentAction(action=ActionType.CLICK, coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="omni_accessibility", mode="desktop"))
        assert not result["success"]
        assert result["engine"] == "omni_accessibility"
        mock_send.assert_called_once()




# ── No cross-engine fallback in executor ──────────────────────────────────────


class TestNoCrossEngineFallback:
    """Executor must never call a different engine's dispatch path."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_accessibility_never_calls_mcp(self, mock_mcp, mock_send):
        mock_send.return_value = {"success": False, "message": "AT-SPI error"}
        action = AgentAction(action=ActionType.CLICK, coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="omni_accessibility", mode="desktop"))
        mock_mcp.assert_not_called()


# ── Model validation ──────────────────────────────────────────────────────────


class TestStartTaskRequestEngineRequired:
    """StartTaskRequest must require engine — no silent default."""

    def test_missing_engine_rejected(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            StartTaskRequest(
                task="test", api_key="12345678",
                provider="google", mode="browser",
            )

    def test_explicit_engine_accepted(self):
        req = StartTaskRequest(
            task="test", api_key="12345678",
            engine="playwright_mcp", mode="browser", provider="google",
        )
        assert req.engine == "playwright_mcp"

    @pytest.mark.parametrize("engine", sorted(SUPPORTED_ENGINES))
    def test_all_engines_accepted(self, engine: str):
        req = StartTaskRequest(
            task="test", api_key="12345678",
            engine=engine, mode="browser", provider="google",
        )
        assert req.engine == engine


# ── Terminal actions bypass dispatch cleanly ──────────────────────────────────


class TestTerminalActions:
    # computer_use has its own internal loop and does not dispatch through execute_action()
    _DISPATCH_ENGINES = sorted(SUPPORTED_ENGINES - {"computer_use"})

    @pytest.mark.parametrize("engine", _DISPATCH_ENGINES)
    def test_done_action_returns_success(self, engine: str):
        action = AgentAction(action=ActionType.DONE, reasoning="finished")
        result = asyncio.run(execute_action(action, engine=engine))
        assert result["success"]
        assert result["message"] == "Task completed"

    @pytest.mark.parametrize("engine", _DISPATCH_ENGINES)
    def test_error_action_returns_failure(self, engine: str):
        action = AgentAction(action=ActionType.ERROR, reasoning="stuck")
        result = asyncio.run(execute_action(action, engine=engine))
        assert not result["success"]
        assert "stuck" in result["message"]


# ── Response shape includes engine tag ────────────────────────────────────────


class TestResponseShape:
    """Every result must include the engine tag for debugging."""

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_mcp_result_has_engine_tag(self, mock_mcp):
        mock_mcp.return_value = {"success": True, "message": "OK"}
        action = AgentAction(action=ActionType.CLICK, target="#submit-btn", coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="playwright_mcp"))
        assert result.get("engine") == "playwright_mcp"

    @patch("backend.engines.accessibility_engine.execute_accessibility_action", new_callable=AsyncMock)
    def test_a11y_result_has_engine_tag(self, mock_a11y):
        mock_a11y.return_value = {"success": True, "message": "OK"}
        action = AgentAction(action=ActionType.CLICK, coordinates=[100, 100])
        result = asyncio.run(execute_action(action, engine="omni_accessibility"))
        assert result.get("engine") == "omni_accessibility"
