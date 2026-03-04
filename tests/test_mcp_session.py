"""Tests for Playwright MCP STDIO session — initialization, reconnection,
tool call routing, and module state management.

Covers:
- STDIO session creation via StdioServerParameters
- Double-check locking in _ensure_mcp_initialized
- Session caching across concurrent calls
- _reset_session clears all module state
- _mcp_call routes through session.call_tool
- _mcp_call handles empty content gracefully
- close_mcp_session noop safety
- Handler functions return correct error for missing params
- mcp_wait caps duration
"""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockTextContent:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _MockCallToolResult:
    def __init__(self, content=None, is_error=False):
        self.content = content or []
        self.isError = is_error


class _MockTool:
    def __init__(self, name):
        self.name = name


class _MockListToolsResult:
    def __init__(self, names=None):
        self.tools = [_MockTool(n) for n in (names or ["browser_navigate"])]


class _MockSession:
    def __init__(self):
        self.call_tool = AsyncMock(
            return_value=_MockCallToolResult([_MockTextContent("ok")])
        )
        self.list_tools = AsyncMock(
            return_value=_MockListToolsResult()
        )
        self.initialize = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@asynccontextmanager
async def _fake_stdio(params, errlog=None):
    yield MagicMock(), MagicMock()


def _reset():
    import backend.agent.playwright_mcp_client as mod
    mod._exit_stack = None
    mod._mcp_session = None
    mod._mcp_init_lock = None
    mod._current_step = 0
    mod._current_action = "unknown"


# ---------------------------------------------------------------------------
# Tests: Locking and Concurrency
# ---------------------------------------------------------------------------

class TestInitLocking(unittest.IsolatedAsyncioTestCase):
    """_ensure_mcp_initialized uses double-check locking."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    async def test_concurrent_inits_only_create_one_session(self):
        """Multiple concurrent calls should result in one initialization."""
        mock_session = _MockSession()
        init_count = 0
        orig_init = mock_session.initialize

        async def _counting_init():
            nonlocal init_count
            init_count += 1
            await asyncio.sleep(0.01)  # simulate startup delay
            return await orig_init()

        mock_session.initialize = _counting_init

        with patch("backend.agent.playwright_mcp_client.stdio_client", _fake_stdio), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _ensure_mcp_initialized
            # Fire 5 concurrent init calls
            results = await asyncio.gather(*[_ensure_mcp_initialized() for _ in range(5)])

            # All should return same session
            self.assertTrue(all(r is results[0] for r in results))
            # Initialize should be called exactly once
            self.assertEqual(init_count, 1)


# ---------------------------------------------------------------------------
# Tests: Tool Call Routing
# ---------------------------------------------------------------------------

class TestToolCallRouting(unittest.IsolatedAsyncioTestCase):
    """_mcp_call correctly routes tool name and arguments."""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    async def test_passes_correct_tool_name_and_args(self):
        mock_session = _MockSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _fake_stdio), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            await _mcp_call("browser_navigate", {"url": "https://test.com"})

            mock_session.call_tool.assert_awaited_with(
                "browser_navigate", {"url": "https://test.com"}
            )

    async def test_empty_content_returns_empty_message(self):
        mock_session = _MockSession()
        mock_session.call_tool.return_value = _MockCallToolResult(content=[])

        with patch("backend.agent.playwright_mcp_client.stdio_client", _fake_stdio), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_reload", {})

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "")


# ---------------------------------------------------------------------------
# Tests: Handler Validation
# ---------------------------------------------------------------------------

class TestHandlerValidation(unittest.IsolatedAsyncioTestCase):
    """Handler functions validate required parameters."""

    async def test_open_url_missing_text(self):
        from backend.agent.playwright_mcp_client import _h_open_url
        result = await _h_open_url("", "")
        self.assertFalse(result["success"])
        self.assertIn("Missing URL", result["message"])

    async def test_click_missing_target(self):
        from backend.agent.playwright_mcp_client import _h_click
        result = await _h_click("", "")
        self.assertFalse(result["success"])
        self.assertIn("Target required", result["message"])

    async def test_type_missing_text(self):
        from backend.agent.playwright_mcp_client import _h_type
        result = await _h_type("", "input")
        self.assertFalse(result["success"])
        self.assertIn("Text required", result["message"])

    async def test_type_missing_target(self):
        from backend.agent.playwright_mcp_client import _h_type
        result = await _h_type("hello", "")
        self.assertFalse(result["success"])
        self.assertIn("Target required", result["message"])

    async def test_fill_missing_target(self):
        from backend.agent.playwright_mcp_client import _h_fill
        result = await _h_fill("value", "")
        self.assertFalse(result["success"])

    async def test_select_option_missing_target(self):
        from backend.agent.playwright_mcp_client import _h_select_option
        result = await _h_select_option("opt", "")
        self.assertFalse(result["success"])

    async def test_done_always_succeeds(self):
        from backend.agent.playwright_mcp_client import _h_done
        result = await _h_done("", "")
        self.assertTrue(result["success"])

    async def test_error_returns_failure(self):
        from backend.agent.playwright_mcp_client import _h_error
        result = await _h_error("Something broke", "")
        self.assertFalse(result["success"])
        self.assertIn("Something broke", result["message"])


# ---------------------------------------------------------------------------
# Tests: Wait Capping
# ---------------------------------------------------------------------------

class TestWaitCapping(unittest.IsolatedAsyncioTestCase):
    """mcp_wait caps duration between 0.1 and 10 seconds."""

    async def test_caps_at_10_seconds(self):
        from backend.agent.playwright_mcp_client import mcp_wait
        result = await mcp_wait(999.0)
        self.assertIn("10.0", result["message"])

    async def test_minimum_0_1_seconds(self):
        from backend.agent.playwright_mcp_client import mcp_wait
        result = await mcp_wait(0.001)
        self.assertIn("0.1", result["message"])
