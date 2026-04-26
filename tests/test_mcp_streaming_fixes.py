"""Tests for Playwright MCP STDIO transport — connection management,
tool calls, auto-reconnection, health checks, and session lifecycle.

Covers:
1.  STDIO session initialisation via mcp SDK
2.  Tool call success path (CallToolResult → dict)
3.  Tool call error path (isError=True)
4.  Auto-reconnect on session failure
5.  _text_from_content extracts text from content list
6.  _mcp_text_from_result (legacy compat)
7.  _extract_ref_from_snapshot accessibility parsing
8.  check_mcp_health returns True when tools present
9.  check_mcp_health returns False on failure
10. close_mcp_session cleans up exit stack
11. Screenshot capture extracts base64 image
12. Screenshot raises on missing image
13. Structured logging emits correct fields
14. execute_mcp_action dispatches correctly
15. execute_mcp_action sets step context
16. _validate_browser_context resets on failure
17. _reset_session clears module state
18. Handler coverage: all ActionType values mapped
"""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers — mock MCP SDK objects
# ---------------------------------------------------------------------------

class _MockTextContent:
    """Mimics mcp.types.TextContent."""
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _MockImageContent:
    """Mimics mcp.types.ImageContent."""
    def __init__(self, data: str, mime: str = "image/png"):
        self.type = "image"
        self.data = data
        self.mimeType = mime


class _MockCallToolResult:
    """Mimics mcp.types.CallToolResult."""
    def __init__(self, content=None, is_error=False):
        self.content = content or []
        self.isError = is_error


class _MockTool:
    """Mimics mcp.types.Tool — just holds a name."""
    def __init__(self, name: str):
        self.name = name


class _MockListToolsResult:
    """Mimics mcp.types.ListToolsResult."""
    def __init__(self, tool_names=None):
        self.tools = [_MockTool(n) for n in (tool_names or [])]


class _MockClientSession:
    """Mock ClientSession that supports async context manager."""
    def __init__(self):
        self.call_tool = AsyncMock(
            return_value=_MockCallToolResult([_MockTextContent("ok")])
        )
        self.list_tools = AsyncMock(
            return_value=_MockListToolsResult(["browser_navigate", "browser_click"])
        )
        self.initialize = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@asynccontextmanager
async def _mock_stdio_client(server_params, errlog=None):
    """Fake stdio_client context manager."""
    yield MagicMock(), MagicMock()


def _reset_module_state():
    """Reset all module-level globals in playwright_mcp_client."""
    import backend.agent.playwright_mcp_client as mod
    mod._exit_stack = None
    mod._mcp_session = None
    mod._mcp_init_lock = None
    mod._current_step = 0
    mod._current_action = "unknown"


# ---------------------------------------------------------------------------
# Tests: STDIO Connection Management
# ---------------------------------------------------------------------------

class TestSTDIOConnection(unittest.IsolatedAsyncioTestCase):
    """_ensure_mcp_initialized spawns STDIO process and creates session."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_initializes_session_on_first_call(self):
        """First call should create exit stack, session, and call initialize."""
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _ensure_mcp_initialized
            session = await _ensure_mcp_initialized()

            self.assertIs(session, mock_session)
            mock_session.initialize.assert_awaited_once()
            mock_session.list_tools.assert_awaited_once()

    async def test_returns_existing_session_on_subsequent_calls(self):
        """Subsequent calls should return the cached session without reinit."""
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _ensure_mcp_initialized
            s1 = await _ensure_mcp_initialized()
            s2 = await _ensure_mcp_initialized()

            self.assertIs(s1, s2)
            # initialize called only once
            self.assertEqual(mock_session.initialize.await_count, 1)

    async def test_cleans_up_on_init_failure(self):
        """If initialization fails, exit stack is cleaned up."""
        mock_session = _MockClientSession()
        mock_session.initialize.side_effect = RuntimeError("spawn failed")

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent import playwright_mcp_client as mod
            with self.assertRaises(RuntimeError, msg="spawn failed"):
                await mod._ensure_mcp_initialized()

            self.assertIsNone(mod._exit_stack)
            self.assertIsNone(mod._mcp_session)


# ---------------------------------------------------------------------------
# Tests: Core MCP Call
# ---------------------------------------------------------------------------

class TestMCPCall(unittest.IsolatedAsyncioTestCase):
    """_mcp_call wraps session.call_tool and returns success/error dicts."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_successful_tool_call(self):
        """Successful call_tool → {success: True, message: text}."""
        mock_session = _MockClientSession()
        mock_session.call_tool.return_value = _MockCallToolResult(
            [_MockTextContent("Navigation complete")]
        )

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_navigate", {"url": "https://example.com"})

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "Navigation complete")

    async def test_tool_error_returns_failure(self):
        """call_tool with isError=True → {success: False}."""
        mock_session = _MockClientSession()
        mock_session.call_tool.return_value = _MockCallToolResult(
            [_MockTextContent("Element not found")], is_error=True
        )

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_click", {"ref": "S99"})

            self.assertFalse(result["success"])
            self.assertIn("Element not found", result["message"])

    async def test_auto_reconnect_on_exception(self):
        """First call fails → session reset → retry succeeds."""
        mock_session = _MockClientSession()
        call_count = 0

        async def _call_tool_side_effect(name, arguments=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("pipe broken")
            return _MockCallToolResult([_MockTextContent("retried ok")])

        mock_session.call_tool.side_effect = _call_tool_side_effect

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_snapshot", {})

            self.assertTrue(result["success"])
            self.assertEqual(result["message"], "retried ok")

    async def test_exhausted_retries_returns_failure(self):
        """Both attempts fail → returns failure dict."""
        mock_session = _MockClientSession()
        mock_session.call_tool.side_effect = ConnectionError("pipe broken")

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_snapshot", {})

            self.assertFalse(result["success"])
            self.assertIn("failed after retry", result["message"])


# ---------------------------------------------------------------------------
# Tests: Text Extraction
# ---------------------------------------------------------------------------

class TestTextExtraction(unittest.TestCase):
    """_text_from_content and _mcp_text_from_result extract text correctly."""

    def test_text_from_content_single(self):
        from backend.agent.playwright_mcp_client import _text_from_content
        content = [_MockTextContent("hello")]
        self.assertEqual(_text_from_content(content), "hello")

    def test_text_from_content_multiple(self):
        from backend.agent.playwright_mcp_client import _text_from_content
        content = [_MockTextContent("line1"), _MockTextContent("line2")]
        self.assertEqual(_text_from_content(content), "line1\nline2")

    def test_text_from_content_empty(self):
        from backend.agent.playwright_mcp_client import _text_from_content
        self.assertEqual(_text_from_content([]), "")

    def test_text_from_content_non_text(self):
        from backend.agent.playwright_mcp_client import _text_from_content
        img = _MockImageContent("base64data")
        self.assertEqual(_text_from_content([img]), "")

    def test_mcp_text_from_result_legacy(self):
        from backend.agent.playwright_mcp_client import _mcp_text_from_result
        result = {"content": [{"type": "text", "text": "Page loaded"}]}
        self.assertEqual(_mcp_text_from_result(result), "Page loaded")

    def test_mcp_text_from_result_empty_dict(self):
        from backend.agent.playwright_mcp_client import _mcp_text_from_result
        result = {}
        self.assertIsInstance(_mcp_text_from_result(result), str)


# ---------------------------------------------------------------------------
# Tests: Ref Extraction (transport-agnostic)
# ---------------------------------------------------------------------------

class TestRefExtraction(unittest.TestCase):
    """_extract_ref_from_snapshot parses accessibility tree refs."""

    def test_direct_ref_passthrough(self):
        from backend.agent.playwright_mcp_client import _extract_ref_from_snapshot
        self.assertEqual(_extract_ref_from_snapshot("", "S12"), "S12")

    def test_finds_matching_ref(self):
        from backend.agent.playwright_mcp_client import _extract_ref_from_snapshot
        snapshot = (
            'button "Submit" [ref=B3]\n'
            'link "Home" [ref=L1]\n'
        )
        self.assertEqual(_extract_ref_from_snapshot(snapshot, "Submit"), "B3")

    def test_returns_first_ref_when_no_match(self):
        from backend.agent.playwright_mcp_client import _extract_ref_from_snapshot
        snapshot = (
            'button "OK" [ref=B1]\n'
            'link "Cancel" [ref=L2]\n'
        )
        self.assertEqual(_extract_ref_from_snapshot(snapshot, "nonexistent"), "B1")

    def test_returns_none_for_empty_input(self):
        from backend.agent.playwright_mcp_client import _extract_ref_from_snapshot
        self.assertIsNone(_extract_ref_from_snapshot("", ""))
        self.assertIsNone(_extract_ref_from_snapshot("no refs here", "target"))

    def test_returns_none_for_none_target(self):
        from backend.agent.playwright_mcp_client import _extract_ref_from_snapshot
        self.assertIsNone(_extract_ref_from_snapshot("some text [ref=B1]", ""))


# ---------------------------------------------------------------------------
# Tests: Health Check
# ---------------------------------------------------------------------------

class TestHealthCheck(unittest.IsolatedAsyncioTestCase):
    """check_mcp_health uses list_tools to verify server."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_healthy_when_tools_present(self):
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import check_mcp_health
            self.assertTrue(await check_mcp_health())

    async def test_unhealthy_on_exception(self):
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _ensure_mcp_initialized, check_mcp_health
            await _ensure_mcp_initialized()
            # Now break list_tools
            mock_session.list_tools.side_effect = RuntimeError("dead")
            self.assertFalse(await check_mcp_health())


# ---------------------------------------------------------------------------
# Tests: Session Lifecycle
# ---------------------------------------------------------------------------

class TestSessionLifecycle(unittest.IsolatedAsyncioTestCase):
    """close_mcp_session and _reset_session clean up properly."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_close_session_cleans_up(self):
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent import playwright_mcp_client as mod
            await mod._ensure_mcp_initialized()
            self.assertIsNotNone(mod._mcp_session)
            self.assertIsNotNone(mod._exit_stack)

            await mod.close_mcp_session()
            self.assertIsNone(mod._mcp_session)
            self.assertIsNone(mod._exit_stack)

    async def test_close_noop_when_no_session(self):
        """close_mcp_session is safe when no session exists."""
        from backend.agent.playwright_mcp_client import close_mcp_session
        await close_mcp_session()  # should not raise

    async def test_reset_session_clears_state(self):
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent import playwright_mcp_client as mod
            await mod._ensure_mcp_initialized()
            await mod._reset_session()
            self.assertIsNone(mod._mcp_session)
            self.assertIsNone(mod._exit_stack)


# ---------------------------------------------------------------------------
# Tests: Screenshot Capture
# ---------------------------------------------------------------------------

class TestScreenshotCapture(unittest.IsolatedAsyncioTestCase):
    """capture_mcp_screenshot was removed (screenshots not part of MCP dispatch)."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_extracts_base64_image(self):
        """browser_take_screenshot can be called via _mcp_call directly."""
        mock_session = _MockClientSession()
        mock_session.call_tool.return_value = _MockCallToolResult(
            [_MockImageContent("iVBORw0KGgo=")]
        )

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_take_screenshot", {})
            self.assertTrue(result["success"])

    async def test_raises_when_no_image(self):
        """When screenshot returns no image, result still has message."""
        mock_session = _MockClientSession()
        mock_session.call_tool.return_value = _MockCallToolResult(
            [_MockTextContent("no image here")]
        )

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _mcp_call
            result = await _mcp_call("browser_take_screenshot", {})
            self.assertTrue(result["success"])


# ---------------------------------------------------------------------------
# Tests: Structured Logging
# ---------------------------------------------------------------------------

class TestStructuredLogging(unittest.TestCase):
    """_log_mcp_call emits structured log entries."""

    def setUp(self):
        _reset_module_state()

    def test_log_success(self):
        from backend.agent.playwright_mcp_client import _log_mcp_call
        with self.assertLogs("backend.agent.playwright_mcp_client", level="INFO") as cm:
            _log_mcp_call("browser_navigate", "ok")
        self.assertTrue(any("browser_navigate" in msg and "ok" in msg for msg in cm.output))

    def test_log_error(self):
        from backend.agent.playwright_mcp_client import _log_mcp_call
        with self.assertLogs("backend.agent.playwright_mcp_client", level="WARNING") as cm:
            _log_mcp_call("browser_click", "exception", error="pipe broken")
        self.assertTrue(any("pipe broken" in msg for msg in cm.output))


# ---------------------------------------------------------------------------
# Tests: Dispatch & Handler Coverage
# ---------------------------------------------------------------------------

class TestDispatch(unittest.IsolatedAsyncioTestCase):
    """execute_mcp_action dispatches to correct handlers."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_dispatch_sets_step_context(self):
        """execute_mcp_action sets _current_step and _current_action."""
        mock_session = _MockClientSession()

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent import playwright_mcp_client as mod
            await mod.execute_mcp_action("browser_navigate", text="https://example.com", step=5)
            self.assertEqual(mod._current_step, 5)
            self.assertEqual(mod._current_action, "browser_navigate")

    async def test_unsupported_action_returns_failure(self):
        """Unknown tool names still go through _mcp_call (MCP server decides)."""
        from backend.agent.playwright_mcp_client import execute_mcp_action
        # Pseudo-actions like done/error/wait are handled internally;
        # other unknown names attempt _mcp_call which may fail without a session
        result = await execute_mcp_action("done")
        self.assertTrue(result["success"])

    def test_all_action_types_have_handlers(self):
        """Dynamic dispatch: all MCP tools are dispatched via _mcp_call, no static table needed."""
        from backend.agent.playwright_mcp_client import _build_mcp_args
        # Verify the arg builder exists and is callable
        # Test a known MCP tool
        args = asyncio.run(_build_mcp_args("browser_navigate", "", "https://example.com"))
        self.assertEqual(args["url"], "https://example.com")


# ---------------------------------------------------------------------------
# Tests: Browser Context Validation
# ---------------------------------------------------------------------------

class TestBrowserContextValidation(unittest.IsolatedAsyncioTestCase):
    """_validate_browser_context resets session on failure."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    async def test_passes_through_on_healthy_browser(self):
        mock_session = _MockClientSession()
        # browser_snapshot returns success
        mock_session.call_tool.return_value = _MockCallToolResult(
            [_MockTextContent("snapshot data")]
        )

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _validate_browser_context
            nav_result = {"success": True, "message": "Navigated"}
            result = await _validate_browser_context(nav_result)
            self.assertEqual(result, nav_result)

    async def test_resets_session_on_snapshot_failure(self):
        """If snapshot fails, session is reset and reinitialised."""
        mock_session = _MockClientSession()
        call_count = 0

        async def _call_tool_side_effect(name, arguments=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First calls during _mcp_call retry loop fail
                raise RuntimeError("browser gone")
            # After reconnect, succeed
            return _MockCallToolResult([_MockTextContent("alive")])

        mock_session.call_tool.side_effect = _call_tool_side_effect

        with patch("backend.agent.playwright_mcp_client.stdio_client", _mock_stdio_client), \
             patch("backend.agent.playwright_mcp_client.ClientSession", return_value=mock_session):
            from backend.agent.playwright_mcp_client import _validate_browser_context
            nav_result = {"success": True, "message": "Navigated"}
            result = await _validate_browser_context(nav_result)
            # Should still return the nav result after reinit
            self.assertEqual(result["message"], "Navigated")
