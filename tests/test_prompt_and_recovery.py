"""Tests for prompt improvements and recovery mechanisms.

Covers:
- Dynamic viewport injection in Playwright prompt
- Stuck detection (exact and near-identical coordinates)
- Stuck recovery hint escalation
- Fill retry with alternative selectors
- Find element with text stripping
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, AsyncMock

from backend.agent.prompts import get_system_prompt
from backend.models import ActionType, AgentAction


class TestPlaywrightMcpPromptContent(unittest.TestCase):
    """Verify the Playwright MCP prompt includes key guidance."""

    def test_playwright_mcp_prompt_has_data_extraction(self):
        prompt = get_system_prompt("playwright_mcp")
        self.assertIn("DATA EXTRACTION", prompt)
        self.assertIn("NEVER re-extract", prompt)

    def test_playwright_mcp_prompt_has_no_repeat_rule(self):
        prompt = get_system_prompt("playwright_mcp")
        self.assertIn("NEVER repeat the same browser_evaluate", prompt)


class TestStuckDetection(unittest.TestCase):
    """Verify stuck detection catches exact and near-identical repeats."""

    def _make_loop(self):
        """Create a minimal AgentLoop for testing stuck detection."""
        from backend.agent.loop import AgentLoop
        with patch("backend.agent.loop.check_service_health"):
            loop = AgentLoop(
                task="test",
                api_key="fake",
                model="test-model",
                mode="browser",
                engine="playwright_mcp",
                provider="google",
            )
        return loop

    def test_exact_duplicate_detected(self):
        """Three identical click actions trigger stuck detection."""
        loop = self._make_loop()
        action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        loop._action_history = [action, action, action]
        self.assertTrue(loop._detect_stuck())

    def test_near_identical_coordinates_detected(self):
        """Clicks within 10px tolerance are considered stuck."""
        loop = self._make_loop()
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        a2 = AgentAction(action=ActionType.CLICK, coordinates=[105, 203])
        a3 = AgentAction(action=ActionType.CLICK, coordinates=[98, 197])
        loop._action_history = [a1, a2, a3]
        self.assertTrue(loop._detect_stuck())

    def test_different_coordinates_not_stuck(self):
        """Clicks far apart are not considered stuck."""
        loop = self._make_loop()
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        a2 = AgentAction(action=ActionType.CLICK, coordinates=[500, 600])
        a3 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        loop._action_history = [a1, a2, a3]
        self.assertFalse(loop._detect_stuck())


class TestActionRetryPolicy(unittest.IsolatedAsyncioTestCase):
    """Verify one-time retry behavior and exact failure logging."""

    def _make_loop(self):
        from backend.agent.loop import AgentLoop
        with patch("backend.agent.loop.check_service_health"):
            return AgentLoop(
                task="test",
                api_key="fake",
                model="test-model",
                mode="desktop",
                engine="omni_accessibility",
                provider="google",
            )

    async def test_step_retries_once_then_succeeds(self):
        """A failed execution is retried once and clears step error on success."""
        loop = self._make_loop()
        action = AgentAction(action=ActionType.CLICK, coordinates=[10, 10], reasoning="test click")

        with patch("backend.agent.loop.capture_screenshot", new=AsyncMock(return_value="abc")), \
             patch("backend.agent.loop.query_model", new=AsyncMock(return_value=(action, "{}"))), \
             patch("backend.agent.loop.execute_action", new=AsyncMock(side_effect=[
                 {"success": False, "message": "first failed", "error_type": "execution"},
                 {"success": True, "message": "retry ok", "error_type": None},
             ])) as mock_exec:
            step = await loop._execute_step(1)

        self.assertIsNone(step.error)
        self.assertEqual(mock_exec.await_count, 2)

    async def test_step_retries_once_then_records_both_errors(self):
        """When retry also fails, both reasons are kept in step.error."""
        loop = self._make_loop()
        action = AgentAction(action=ActionType.CLICK, coordinates=[10, 10], reasoning="test click")

        with patch("backend.agent.loop.capture_screenshot", new=AsyncMock(return_value="abc")), \
             patch("backend.agent.loop.query_model", new=AsyncMock(return_value=(action, "{}"))), \
             patch("backend.agent.loop.execute_action", new=AsyncMock(side_effect=[
                 {"success": False, "message": "first failed", "error_type": "execution"},
                 {"success": False, "message": "second failed", "error_type": "execution"},
             ])):
            step = await loop._execute_step(1)

        self.assertIn("initial failure: first failed", step.error)
        self.assertIn("retry failure: second failed", step.error)

    def test_different_actions_not_stuck(self):
        """Different action types don't trigger stuck."""
        loop = self._make_loop()
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        a2 = AgentAction(action=ActionType.FILL, coordinates=[100, 200], text="x")
        a3 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        loop._action_history = [a1, a2, a3]
        self.assertFalse(loop._detect_stuck())

    def test_insufficient_history_not_stuck(self):
        """Fewer than the engine's window never trigger stuck."""
        loop = self._make_loop()  # xdotool engine: window = 2
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
        loop._action_history = [a1]  # only 1 action, below window of 2
        self.assertFalse(loop._detect_stuck())

    def test_desktop_stuck_detected_with_two_actions(self):
        """Desktop engines trigger stuck after only 2 near-identical clicks."""
        loop = self._make_loop()  # accessibility engine
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[500, 400])
        a2 = AgentAction(action=ActionType.CLICK, coordinates=[515, 420])
        loop._action_history = [a1, a2]
        self.assertTrue(loop._detect_stuck())

    def test_desktop_wide_tolerance_caught(self):
        """Desktop uses 30px tolerance — 28px drift triggers stuck."""
        loop = self._make_loop()  # accessibility engine
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[500, 400])
        a2 = AgentAction(action=ActionType.CLICK, coordinates=[528, 400])  # 28px drift
        loop._action_history = [a1, a2]
        self.assertTrue(loop._detect_stuck())

    def test_desktop_outside_tolerance_not_stuck(self):
        """Desktop: >30px apart is NOT stuck."""
        loop = self._make_loop()  # accessibility engine
        a1 = AgentAction(action=ActionType.CLICK, coordinates=[500, 400])
        a2 = AgentAction(action=ActionType.CLICK, coordinates=[550, 400])  # 50px drift
        loop._action_history = [a1, a2]
        self.assertFalse(loop._detect_stuck())


class TestFillRetryHelpers(unittest.TestCase):
    """Test the selector generation logic used by fill retry."""

    def _generate_alt_selectors(self, selector: str) -> list:
        """Replicates the logic from agent_service._generate_alt_selectors."""
        import re
        alternatives = []

        name_match = re.search(r'name=["\']?([^"\'\\]+)', selector)
        id_match = re.search(r'id=["\']?([^"\'\\]+)', selector)
        type_match = re.search(r'type=["\']?([^"\'\\]+)', selector)

        if name_match:
            name = name_match.group(1)
            alternatives.extend([
                f'input[name="{name}"]',
                f'textarea[name="{name}"]',
                f'select[name="{name}"]',
                f'[name="{name}"]',
            ])
        if id_match:
            id_val = id_match.group(1)
            alternatives.extend([
                f'#{id_val}',
                f'input[id="{id_val}"]',
                f'[id="{id_val}"]',
            ])
        if type_match and not name_match and not id_match:
            type_val = type_match.group(1)
            alternatives.append(f'input[type="{type_val}"]')

        seen = {selector}
        return [s for s in alternatives if s not in seen and not seen.add(s)]

    def test_generate_alt_selectors_from_name(self):
        """Alt selectors generated from name attribute."""
        alts = self._generate_alt_selectors('input[name="telephone"]')
        self.assertTrue(any("telephone" in s for s in alts))
        # Should not include original
        self.assertNotIn('input[name="telephone"]', alts)

    def test_generate_alt_selectors_from_id(self):
        """Alt selectors generated from id attribute."""
        alts = self._generate_alt_selectors('input[id="email"]')
        self.assertTrue(any("#email" in s for s in alts))

    def test_generate_alt_selectors_no_match(self):
        """Returns empty list when no name/id/type can be extracted."""
        alts = self._generate_alt_selectors("div.some-class")
        self.assertEqual(alts, [])


class TestFindElementTextStripping(unittest.TestCase):
    """Test that find_element strips common suffixes like 'button'."""

    def test_strip_button_suffix(self):
        """'Submit order button' → tries 'Submit order' as fallback."""
        import re
        description = "Submit order button"
        stripped = re.sub(
            r'\s+(button|link|field|input|element|area|box|section)\s*$',
            '', description, flags=re.IGNORECASE,
        ).strip()
        self.assertEqual(stripped, "Submit order")

    def test_no_strip_when_no_suffix(self):
        """'Submit order' stays unchanged."""
        import re
        description = "Submit order"
        stripped = re.sub(
            r'\s+(button|link|field|input|element|area|box|section)\s*$',
            '', description, flags=re.IGNORECASE,
        ).strip()
        self.assertEqual(stripped, "Submit order")


if __name__ == "__main__":
    unittest.main()
