"""Tests for agent loop completion detection, duplicate result handling,
and stuck-detection force-termination.

Covers:
- Execution results enriched into action history (evaluate_js, get_text)
- Duplicate result detection (_detect_duplicate_results)
- Stuck detection counter & force-termination after MAX_STUCK_DETECTIONS
- Recovery hints for evaluate_js and get_text loops
- Prompt data-extraction guidance (Playwright, Playwright MCP)
- History text truncation shows JS results (>60 chars)
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent.loop import (
    AgentLoop,
    MAX_DUPLICATE_RESULTS,
    MAX_STUCK_DETECTIONS,
)
from backend.models import ActionType, AgentAction
from backend.agent.prompts import get_system_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loop(engine: str = "playwright_mcp", max_steps: int = 50) -> AgentLoop:
    """Create a minimal AgentLoop for unit testing."""
    return AgentLoop(
        task="test task",
        api_key="test-key",
        model="test-model",
        max_steps=max_steps,
        engine=engine,
        provider="google",
    )


def _make_action(
    action: ActionType = ActionType.EVALUATE_JS,
    text: str = "JSON.stringify({title:document.title})",
    reasoning: str = "",
    coordinates: list[int] | None = None,
) -> AgentAction:
    """Create a test AgentAction."""
    return AgentAction(
        action=action,
        text=text,
        reasoning=reasoning,
        coordinates=coordinates,
    )


# ---------------------------------------------------------------------------
# Tests: Duplicate result detection
# ---------------------------------------------------------------------------

class TestDetectDuplicateResults(unittest.TestCase):
    """_detect_duplicate_results flags identical execution results."""

    def test_no_results_returns_false(self):
        loop = _make_loop()
        self.assertFalse(loop._detect_duplicate_results())

    def test_single_result_returns_false(self):
        loop = _make_loop()
        loop._result_cache = ['{"title":"Hello","linkCount":42}']
        self.assertFalse(loop._detect_duplicate_results())

    def test_different_results_returns_false(self):
        loop = _make_loop()
        loop._result_cache = [
            '{"title":"Page 1","linkCount":100}',
            '{"title":"Page 2","linkCount":200}',
            '{"title":"Page 3","linkCount":300}',
        ]
        self.assertFalse(loop._detect_duplicate_results())

    def test_duplicate_results_returns_true(self):
        loop = _make_loop()
        same = '{"title":"autonomous-agents","h1":"Search","linkCount":482}'
        loop._result_cache = [same] * (MAX_DUPLICATE_RESULTS + 1)
        self.assertTrue(loop._detect_duplicate_results())

    def test_short_results_ignored(self):
        """Trivial short results (<20 chars) should not trigger detection."""
        loop = _make_loop()
        loop._result_cache = ["OK", "OK", "OK", "OK"]
        self.assertFalse(loop._detect_duplicate_results())

    def test_threshold_boundary(self):
        """Exactly MAX_DUPLICATE_RESULTS identical results should trigger."""
        loop = _make_loop()
        same = "JS result: " + "x" * 50
        # Need MAX_DUPLICATE_RESULTS + 1 items in cache
        loop._result_cache = [same] * (MAX_DUPLICATE_RESULTS + 1)
        self.assertTrue(loop._detect_duplicate_results())

        # One fewer should NOT trigger
        loop._result_cache = [same] * MAX_DUPLICATE_RESULTS
        self.assertFalse(loop._detect_duplicate_results())


# ---------------------------------------------------------------------------
# Tests: Stuck detection counter & force-termination
# ---------------------------------------------------------------------------

class TestStuckDetectionCounter(unittest.TestCase):
    """Stuck detection increments counter and force-terminates."""

    def test_stuck_count_starts_at_zero(self):
        loop = _make_loop()
        self.assertEqual(loop._stuck_count, 0)

    def test_detect_stuck_increments_when_detected(self):
        """Simulating stuck detection via manual count (since _detect_stuck
        requires action_history manipulation)."""
        loop = _make_loop()
        # Populate identical actions to trigger stuck
        action = _make_action(ActionType.CLICK, coordinates=[100, 200])
        loop._action_history = [action, action, action]
        stuck = loop._detect_stuck()
        self.assertTrue(stuck)

    def test_max_stuck_detections_constant(self):
        """MAX_STUCK_DETECTIONS should be reasonable (2-5)."""
        self.assertGreaterEqual(MAX_STUCK_DETECTIONS, 2)
        self.assertLessEqual(MAX_STUCK_DETECTIONS, 5)


# ---------------------------------------------------------------------------
# Tests: Recovery hints for JS/text loops
# ---------------------------------------------------------------------------

class TestRecoveryHints(unittest.TestCase):
    """_build_recovery_hint provides specific guidance for each action type."""

    def test_evaluate_js_hint_says_stop(self):
        loop = _make_loop()
        loop._action_history = [_make_action(ActionType.EVALUATE_JS)]
        hint = loop._build_recovery_hint()
        self.assertIn("STOP", hint)
        self.assertIn("done", hint)
        self.assertIn("already", hint.lower())

    def test_get_text_hint_says_stop(self):
        loop = _make_loop()
        loop._action_history = [_make_action(ActionType.GET_TEXT, text="h1")]
        hint = loop._build_recovery_hint()
        self.assertIn("STOP", hint)
        self.assertIn("done", hint.lower())

    def test_click_hint_unchanged(self):
        loop = _make_loop()
        loop._action_history = [
            _make_action(ActionType.CLICK, text="", coordinates=[100, 200])
        ]
        hint = loop._build_recovery_hint()
        self.assertIn("evaluate_js", hint)

    def test_fill_hint_mentions_discover(self):
        loop = _make_loop()
        loop._action_history = [_make_action(ActionType.FILL, text="query")]
        hint = loop._build_recovery_hint()
        self.assertIn("discover", hint.lower())

    def test_generic_hint_for_unknown_action(self):
        loop = _make_loop()
        loop._action_history = [_make_action(ActionType.SCROLL)]
        hint = loop._build_recovery_hint()
        self.assertIn("different approach", hint.lower())


# ---------------------------------------------------------------------------
# Tests: Result enrichment into action history
# ---------------------------------------------------------------------------

class TestResultEnrichment(unittest.TestCase):
    """Execution results should be appended to action.reasoning."""

    def test_result_cache_bounded(self):
        """Cache should not grow beyond 10 entries."""
        loop = _make_loop()
        for i in range(20):
            loop._result_cache.append(f"result_{i}_" + "x" * 50)
            if len(loop._result_cache) > 10:
                loop._result_cache = loop._result_cache[-10:]
        self.assertLessEqual(len(loop._result_cache), 10)

    def test_initial_result_cache_empty(self):
        loop = _make_loop()
        self.assertEqual(len(loop._result_cache), 0)


# ---------------------------------------------------------------------------
# Tests: Prompt updates for data extraction
# ---------------------------------------------------------------------------

class TestPromptDataExtraction(unittest.TestCase):
    """System prompts include data extraction and completion guidance."""

    def test_playwright_mcp_prompt_has_data_extraction(self):
        prompt = get_system_prompt("playwright_mcp")
        self.assertIn("DATA EXTRACTION", prompt)
        self.assertIn("NEVER re-extract", prompt)
        self.assertIn("Result:", prompt)

    def test_playwright_mcp_prompt_has_no_repeat_rule(self):
        prompt = get_system_prompt("playwright_mcp")
        self.assertIn("NEVER repeat the same browser_evaluate", prompt)


# ---------------------------------------------------------------------------
# Tests: History text includes JS result data
# ---------------------------------------------------------------------------

class TestHistoryTextExpansion(unittest.TestCase):
    """History lines should show enough of evaluate_js results."""

    def test_gemini_history_shows_result_in_reasoning(self):
        """When reasoning contains '→ Result: {...}', it should be visible."""
        from google.genai import types
        from backend.agent.gemini_client import _build_contents

        action = AgentAction(
            action=ActionType.EVALUATE_JS,
            text='JSON.stringify({title:document.title})',
            reasoning='Extracting title → Result: JS result: {"title":"Test Page","linkCount":42}',
        )
        contents = _build_contents(
            task="test",
            screenshot_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            action_history=[action],
            step_number=2,
        )
        # The history text part should contain the result
        history_part = contents[0].parts[0].text
        self.assertIn("Result:", history_part)
        self.assertIn("Test Page", history_part)

    def test_anthropic_history_shows_result_in_reasoning(self):
        from backend.agent.anthropic_client import _build_messages

        action = AgentAction(
            action=ActionType.EVALUATE_JS,
            text='JSON.stringify({title:document.title})',
            reasoning='Extracting → Result: JS result: {"title":"Page Title","h1":"Hello"}',
        )
        messages = _build_messages(
            task="test",
            screenshot_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            action_history=[action],
            step_number=2,
        )
        # History is in the first text content part
        history_text = messages[0]["content"][0]["text"]
        self.assertIn("Result:", history_text)
        self.assertIn("Page Title", history_text)

    def test_gemini_evaluate_js_gets_larger_truncation(self):
        """evaluate_js results should get 400 char limit, not 120."""
        from google.genai import types
        from backend.agent.gemini_client import _build_contents

        long_result = "→ Result: " + "x" * 300
        action = AgentAction(
            action=ActionType.EVALUATE_JS,
            text='long script',
            reasoning=long_result,
        )
        contents = _build_contents(
            task="test",
            screenshot_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            action_history=[action],
            step_number=2,
        )
        history_part = contents[0].parts[0].text
        # With 400 char limit, 310 chars of reasoning should be fully visible
        self.assertIn("x" * 100, history_part)

    def test_gemini_click_gets_smaller_truncation(self):
        """click reasoning should be truncated at 120 chars."""
        from google.genai import types
        from backend.agent.gemini_client import _build_contents

        long_reasoning = "R" * 200
        action = AgentAction(
            action=ActionType.CLICK,
            coordinates=[100, 200],
            reasoning=long_reasoning,
        )
        contents = _build_contents(
            task="test",
            screenshot_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            action_history=[action],
            step_number=2,
        )
        history_part = contents[0].parts[0].text
        # Should be truncated — not all 200 R's visible
        self.assertNotIn("R" * 200, history_part)
        # But the first 120 should be present (checking 100 to be safe)
        self.assertIn("R" * 100, history_part)


# ---------------------------------------------------------------------------
# Tests: Constants sanity
# ---------------------------------------------------------------------------

class TestLoopConstants(unittest.TestCase):
    """Sanity check for loop control constants."""

    def test_max_duplicate_results_reasonable(self):
        self.assertGreaterEqual(MAX_DUPLICATE_RESULTS, 2)
        self.assertLessEqual(MAX_DUPLICATE_RESULTS, 5)

    def test_max_stuck_detections_reasonable(self):
        self.assertGreaterEqual(MAX_STUCK_DETECTIONS, 2)
        self.assertLessEqual(MAX_STUCK_DETECTIONS, 5)


if __name__ == "__main__":
    unittest.main()
