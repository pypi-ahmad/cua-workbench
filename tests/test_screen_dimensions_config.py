"""Focused tests for config-driven screen dimensions in prompts and validation.

The audit flagged hard-coded ``1440x900`` references in places that should
honor the runtime ``SCREEN_WIDTH`` / ``SCREEN_HEIGHT`` config.  These
tests lock the substitution path:

  1) At default dimensions, the computer-use prompt mentions 1440x900.
  2) At a non-default size (1920x1080), the prompt reflects the new size
     and contains NO stale 1440x900 reference (browser or desktop).
  3) The legacy literal "1440x900" no longer appears in any *live*
     SYSTEM_PROMPT_* string (dead-code constants are exempt).
  4) ``executor.py`` uses ``config.screen_width/height`` for click bounds.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.agent import prompts


class TestComputerUsePromptViewport(unittest.TestCase):
    """SYSTEM_PROMPT_COMPUTER_USE must honor configured dimensions."""

    def test_default_dimensions_in_prompt(self):
        # Default config: 1440x900 → browser viewport 1340x820, desktop 1440x900.
        with patch.object(prompts, "__name__", prompts.__name__):
            text = prompts.get_system_prompt("computer_use")
        self.assertIn("1340x820 (browser)", text)
        self.assertIn("1440x900 (desktop)", text)
        # No leftover template placeholders.
        self.assertNotIn("{viewport_width}", text)
        self.assertNotIn("{viewport_height}", text)
        self.assertNotIn("{screen_width}", text)
        self.assertNotIn("{screen_height}", text)

    def test_non_default_dimensions_in_prompt(self):
        # 1920x1080 → browser viewport 1820x1000, desktop 1920x1080.
        from backend import config as cfg_module
        with patch.object(cfg_module.config, "screen_width", 1920), \
             patch.object(cfg_module.config, "screen_height", 1080):
            text = prompts.get_system_prompt("computer_use")
        self.assertIn("1820x1000 (browser)", text)
        self.assertIn("1920x1080 (desktop)", text)
        # No stale default sizes anywhere in the rendered prompt.
        self.assertNotIn("1340x820", text)
        self.assertNotIn("1440x900", text)
        # No leftover template placeholders.
        self.assertNotIn("{screen_width}", text)
        self.assertNotIn("{screen_height}", text)

    def test_small_dimensions_in_prompt(self):
        # 1280x720 → browser viewport 1180x640, desktop 1280x720.
        from backend import config as cfg_module
        with patch.object(cfg_module.config, "screen_width", 1280), \
             patch.object(cfg_module.config, "screen_height", 720):
            text = prompts.get_system_prompt("computer_use")
        self.assertIn("1180x640 (browser)", text)
        self.assertIn("1280x720 (desktop)", text)
        self.assertNotIn("1440x900", text)


class TestNoLiveHardcodedDimensions(unittest.TestCase):
    """No live SYSTEM_PROMPT_* string may contain a literal '1440x900'."""

    def test_live_system_prompts_have_no_hardcoded_dim(self):
        # Live prompt strings only — exclude legacy/dead constants.
        for attr_name in dir(prompts):
            if not attr_name.startswith("SYSTEM_PROMPT_"):
                continue
            value = getattr(prompts, attr_name)
            if not isinstance(value, str):
                continue
            self.assertNotIn(
                "1440x900", value,
                f"Live prompt {attr_name} contains hard-coded 1440x900",
            )
            self.assertNotIn(
                "1440×900", value,
                f"Live prompt {attr_name} contains hard-coded 1440×900",
            )


class TestExecutorClickBoundsConfigDriven(unittest.TestCase):
    """executor.validate_unified_action must use config.screen_width/height."""

    def test_click_at_default_within_bounds(self):
        from backend.agent.executor import validate_unified_action
        from backend.tools.unified_schema import UnifiedAction
        # Default 1440x900 — (1000, 500) is in bounds.
        action = UnifiedAction(
            action="click",
            engine="omni_accessibility",
            coordinates=[1000, 500],
            target="x",
        )
        self.assertIsNone(validate_unified_action(action))

    def test_click_at_default_out_of_bounds(self):
        from backend.agent.executor import validate_unified_action
        from backend.tools.unified_schema import UnifiedAction
        # Default 1440x900 — (1500, 1000) is OUT of bounds.
        action = UnifiedAction(
            action="click",
            engine="omni_accessibility",
            coordinates=[1500, 1000],
            target="x",
        )
        result = validate_unified_action(action)
        self.assertIsNotNone(result)
        self.assertIn("out of bounds", result["message"].lower())

    def test_click_bounds_widen_with_config(self):
        from backend.agent.executor import validate_unified_action
        from backend.tools.unified_schema import UnifiedAction
        from backend import config as cfg_module
        # At 1920x1080, (1500, 1000) must be ALLOWED.
        with patch.object(cfg_module.config, "screen_width", 1920), \
             patch.object(cfg_module.config, "screen_height", 1080):
            action = UnifiedAction(
                action="click",
                engine="omni_accessibility",
                coordinates=[1500, 1000],
                target="x",
            )
            self.assertIsNone(validate_unified_action(action))

    def test_click_bounds_narrow_with_config(self):
        from backend.agent.executor import validate_unified_action
        from backend.tools.unified_schema import UnifiedAction
        from backend import config as cfg_module
        # At 800x600, (1000, 500) must be REJECTED.
        with patch.object(cfg_module.config, "screen_width", 800), \
             patch.object(cfg_module.config, "screen_height", 600):
            action = UnifiedAction(
                action="click",
                engine="omni_accessibility",
                coordinates=[1000, 500],
                target="x",
            )
            result = validate_unified_action(action)
            self.assertIsNotNone(result)
            self.assertIn("800x600", result["message"])


if __name__ == "__main__":
    unittest.main()
