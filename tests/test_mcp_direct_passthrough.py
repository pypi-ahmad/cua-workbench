"""Smoke tests for the MCP direct passthrough path (tool_args).

Validates that when AgentAction.tool_args is provided, the executor
routes to execute_mcp_action_direct (bypassing _build_mcp_args / ref
resolution / JS fallback), and that the legacy flat path still works
when tool_args is None.
"""

import asyncio
import unittest
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models import ActionType, AgentAction


# ── AgentAction.tool_args field ──────────────────────────────────────────────

class TestAgentActionToolArgs(unittest.TestCase):
    """AgentAction accepts optional tool_args dict."""

    def test_tool_args_default_none(self):
        """tool_args defaults to None (backward compat)."""
        action = AgentAction(action=ActionType.BROWSER_CLICK, target="button")
        assert action.tool_args is None

    def test_tool_args_with_dict(self):
        """tool_args accepts a dict."""
        action = AgentAction(
            action=ActionType.BROWSER_CLICK,
            tool_args={"element": "Submit", "ref": "S12"},
        )
        assert action.tool_args == {"element": "Submit", "ref": "S12"}

    def test_tool_args_serialization(self):
        """tool_args round-trips through model_dump."""
        action = AgentAction(
            action=ActionType.BROWSER_NAVIGATE,
            tool_args={"url": "https://example.com"},
            reasoning="Navigate to example",
        )
        data = action.model_dump(exclude_none=True)
        assert data["tool_args"] == {"url": "https://example.com"}

    def test_tool_args_none_excluded(self):
        """tool_args=None is excluded from model_dump(exclude_none=True)."""
        action = AgentAction(action=ActionType.DONE, reasoning="done")
        data = action.model_dump(exclude_none=True)
        assert "tool_args" not in data


# ── execute_mcp_action_direct ────────────────────────────────────────────────

class TestExecuteMcpActionDirect(unittest.IsolatedAsyncioTestCase):
    """execute_mcp_action_direct passes tool_args verbatim to _mcp_call."""

    async def test_direct_passthrough_navigate(self):
        """browser_navigate with tool_args calls _mcp_call with exact args."""
        import backend.agent.playwright_mcp_client as mod

        mock_mcp_call = AsyncMock(return_value={"success": True, "message": "Navigated"})
        mock_validate = AsyncMock(side_effect=lambda r: r)

        with patch.object(mod, "_mcp_call", mock_mcp_call), \
             patch.object(mod, "_validate_browser_context", mock_validate):
            result = await mod.execute_mcp_action_direct(
                tool_name="browser_navigate",
                tool_args={"url": "https://example.com"},
                step=1,
            )

        assert result["success"] is True
        mock_mcp_call.assert_called_once_with("browser_navigate", {"url": "https://example.com"})
        # browser_navigate triggers _validate_browser_context
        mock_validate.assert_called_once()

    async def test_direct_passthrough_click(self):
        """browser_click with tool_args calls _mcp_call with exact args — no ref resolution."""
        import backend.agent.playwright_mcp_client as mod

        tool_args = {"element": "Submit button", "ref": "S42"}
        mock_mcp_call = AsyncMock(return_value={"success": True, "message": "Clicked"})

        with patch.object(mod, "_mcp_call", mock_mcp_call):
            result = await mod.execute_mcp_action_direct(
                tool_name="browser_click",
                tool_args=tool_args,
                step=2,
            )

        assert result["success"] is True
        mock_mcp_call.assert_called_once_with("browser_click", tool_args)

    async def test_direct_passthrough_type(self):
        """browser_type with tool_args — no _resolve_input_ref, no _self_heal_input."""
        import backend.agent.playwright_mcp_client as mod

        tool_args = {"element": "Search box", "ref": "e7", "text": "hello world", "submit": True}
        mock_mcp_call = AsyncMock(return_value={"success": True, "message": "Typed"})

        with patch.object(mod, "_mcp_call", mock_mcp_call):
            result = await mod.execute_mcp_action_direct(
                tool_name="browser_type",
                tool_args=tool_args,
                step=3,
            )

        assert result["success"] is True
        mock_mcp_call.assert_called_once_with("browser_type", tool_args)

    async def test_direct_pseudo_action_done(self):
        """Pseudo-action 'done' routes through legacy execute_mcp_action."""
        import backend.agent.playwright_mcp_client as mod

        result = await mod.execute_mcp_action_direct("done", {"text": "completed"}, step=5)
        assert result["success"] is True
        assert "completed" in result["message"].lower() or "Task completed" in result["message"]

    async def test_direct_pseudo_action_wait(self):
        """Pseudo-action 'wait' routes through legacy execute_mcp_action."""
        import backend.agent.playwright_mcp_client as mod

        result = await mod.execute_mcp_action_direct("wait", {"text": "0.1"}, step=6)
        assert result["success"] is True


# ── Executor dispatch with tool_args ─────────────────────────────────────────

class TestExecutorDispatchToolArgs(unittest.IsolatedAsyncioTestCase):
    """execute_action routes to direct path when tool_args is present."""

    async def test_executor_uses_direct_when_tool_args_present(self):
        """Executor dispatches to execute_mcp_action_direct when tool_args is set."""
        action = AgentAction(
            action=ActionType.BROWSER_NAVIGATE,
            tool_args={"url": "https://example.com"},
            reasoning="Go to example",
        )

        mock_direct = AsyncMock(return_value={"success": True, "message": "Navigated"})

        with patch("backend.agent.playwright_mcp_client.execute_mcp_action_direct", mock_direct):
            from backend.agent.executor import execute_action
            result = await execute_action(
                action=action,
                engine="playwright_mcp",
                execution_target="local",
                step=1,
            )

        assert result["success"] is True
        mock_direct.assert_called_once_with(
            tool_name="browser_navigate",
            tool_args={"url": "https://example.com"},
            step=1,
        )

    async def test_executor_uses_legacy_when_tool_args_absent(self):
        """Executor dispatches to legacy execute_mcp_action when tool_args is None."""
        action = AgentAction(
            action=ActionType.BROWSER_NAVIGATE,
            target="https://example.com",
            text="https://example.com",
        )

        mock_legacy = AsyncMock(return_value={"success": True, "message": "Navigated"})

        with patch("backend.agent.playwright_mcp_client.execute_mcp_action", mock_legacy):
            from backend.agent.executor import execute_action
            result = await execute_action(
                action=action,
                engine="playwright_mcp",
                execution_target="local",
                step=1,
            )

        assert result["success"] is True
        mock_legacy.assert_called_once()

    async def test_executor_docker_direct_path(self):
        """Docker executor dispatches to execute_mcp_action_direct_docker when tool_args is set."""
        action = AgentAction(
            action=ActionType.BROWSER_CLICK,
            tool_args={"element": "Login", "ref": "A5"},
            reasoning="Click login",
        )

        mock_direct_docker = AsyncMock(return_value={"success": True, "message": "Clicked"})

        with patch("backend.agent.playwright_mcp_client.execute_mcp_action_direct_docker", mock_direct_docker):
            from backend.agent.executor import execute_action
            result = await execute_action(
                action=action,
                engine="playwright_mcp",
                execution_target="docker",
                step=2,
            )

        assert result["success"] is True
        mock_direct_docker.assert_called_once_with(
            tool_name="browser_click",
            tool_args={"element": "Login", "ref": "A5"},
            step=2,
        )


# ── Response parsing (tool_args extraction) ──────────────────────────────────

class TestResponseParsingToolArgs(unittest.TestCase):
    """Gemini and Anthropic parsers extract tool_args from model JSON."""

    def test_gemini_parse_with_tool_args(self):
        """Gemini parser extracts tool_args from response JSON."""
        import json
        from backend.agent.gemini_client import _parse_action
        raw = json.dumps({
            "action": "browser_click",
            "tool_args": {"element": "OK", "ref": "S3"},
            "coordinates": [0, 0],
            "target": "",
            "text": "",
            "reasoning": "click OK",
        })
        action = _parse_action(raw)
        assert action.action == ActionType.BROWSER_CLICK
        assert action.tool_args == {"element": "OK", "ref": "S3"}

    def test_gemini_parse_without_tool_args(self):
        """Gemini parser leaves tool_args=None when not in response."""
        import json
        from backend.agent.gemini_client import _parse_action
        raw = json.dumps({
            "action": "browser_click",
            "target": "OK button",
            "coordinates": [0, 0],
            "text": "",
            "reasoning": "click OK",
        })
        action = _parse_action(raw)
        assert action.action == ActionType.BROWSER_CLICK
        assert action.tool_args is None
        assert action.target == "OK button"

    def test_anthropic_parse_with_tool_args(self):
        """Anthropic parser extracts tool_args from response JSON."""
        import json
        from backend.agent.anthropic_client import _parse_action
        raw = json.dumps({
            "action": "browser_navigate",
            "tool_args": {"url": "https://example.com"},
            "coordinates": [0, 0],
            "target": "",
            "text": "",
            "reasoning": "go",
        })
        action = _parse_action(raw)
        assert action.action == ActionType.BROWSER_NAVIGATE
        assert action.tool_args == {"url": "https://example.com"}

    def test_anthropic_parse_without_tool_args(self):
        """Anthropic parser leaves tool_args=None when not in response."""
        import json
        from backend.agent.anthropic_client import _parse_action
        raw = json.dumps({
            "action": "browser_navigate",
            "target": "",
            "coordinates": [0, 0],
            "text": "https://example.com",
            "reasoning": "go",
        })
        action = _parse_action(raw)
        assert action.action == ActionType.BROWSER_NAVIGATE
        assert action.tool_args is None

    def test_gemini_parse_invalid_tool_args_type(self):
        """Non-dict tool_args is ignored (set to None)."""
        import json
        from backend.agent.gemini_client import _parse_action
        raw = json.dumps({
            "action": "browser_click",
            "tool_args": "not a dict",
            "target": "btn",
            "coordinates": [0, 0],
            "text": "",
            "reasoning": "x",
        })
        action = _parse_action(raw)
        assert action.tool_args is None
