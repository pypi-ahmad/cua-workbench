"""Tests for the unified ComputerUseEngine.

Covers:
- Coordinate normalization (Gemini 0-999 → pixels)
- PlaywrightExecutor action dispatch
- DesktopExecutor action dispatch
- ClaudeCUClient action mapping
- ComputerUseEngine facade construction
- Safety decision handling
"""

from __future__ import annotations

import asyncio
import base64
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.computer_use_engine import (
    CUActionResult,
    CUTurnRecord,
    ClaudeCUClient,
    ComputerUseEngine,
    DEFAULT_SCREEN_HEIGHT,
    DEFAULT_SCREEN_WIDTH,
    DesktopExecutor,
    Environment,
    GeminiCUClient,
    GEMINI_NORMALIZED_MAX,
    PlaywrightExecutor,
    Provider,
    SafetyDecision,
    denormalize_x,
    denormalize_y,
)


# ── Coordinate helpers ─────────────────────────────────────────────────────────

class TestDenormalize:
    """Gemini CU uses normalized 0-999 coords → pixel mapping."""

    def test_denormalize_x_origin(self):
        assert denormalize_x(0) == 0

    def test_denormalize_y_origin(self):
        assert denormalize_y(0) == 0

    def test_denormalize_x_max(self):
        result = denormalize_x(999, DEFAULT_SCREEN_WIDTH)
        assert result == int(999 / GEMINI_NORMALIZED_MAX * DEFAULT_SCREEN_WIDTH)

    def test_denormalize_y_max(self):
        result = denormalize_y(999, DEFAULT_SCREEN_HEIGHT)
        assert result == int(999 / GEMINI_NORMALIZED_MAX * DEFAULT_SCREEN_HEIGHT)

    def test_denormalize_midpoint(self):
        assert denormalize_x(500, 1440) == int(500 / 1000 * 1440)
        assert denormalize_y(500, 900) == int(500 / 1000 * 900)

    def test_denormalize_custom_screen(self):
        assert denormalize_x(500, 1920) == int(500 / 1000 * 1920)
        assert denormalize_y(500, 1080) == int(500 / 1000 * 1080)


# ── PlaywrightExecutor ─────────────────────────────────────────────────────────

class TestPlaywrightExecutor(unittest.IsolatedAsyncioTestCase):
    """PlaywrightExecutor translates CU actions → Playwright page calls."""

    def _make_executor(self, normalize: bool = True) -> PlaywrightExecutor:
        page = AsyncMock()
        page.url = "https://example.com"
        page.mouse = AsyncMock()
        page.keyboard = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"\x89PNG_FAKE")
        return PlaywrightExecutor(
            page=page,
            screen_width=1440,
            screen_height=900,
            normalize_coords=normalize,
        )

    async def test_click_at_normalized(self):
        ex = self._make_executor(normalize=True)
        result = await ex.execute("click_at", {"x": 500, "y": 500})
        assert result.success
        assert result.name == "click_at"
        # Verify denormalized coords were used
        ex.page.mouse.click.assert_awaited_once()
        px, py = ex.page.mouse.click.call_args[0]
        assert px == denormalize_x(500, 1440)
        assert py == denormalize_y(500, 900)

    async def test_click_at_real_pixels(self):
        ex = self._make_executor(normalize=False)
        result = await ex.execute("click_at", {"x": 720, "y": 450})
        assert result.success
        ex.page.mouse.click.assert_awaited_once_with(720, 450)

    async def test_hover_at(self):
        ex = self._make_executor(normalize=False)
        result = await ex.execute("hover_at", {"x": 100, "y": 200})
        assert result.success
        ex.page.mouse.move.assert_awaited_once_with(100, 200)

    async def test_type_text_at(self):
        ex = self._make_executor(normalize=False)
        result = await ex.execute("type_text_at", {
            "x": 100, "y": 200,
            "text": "hello world",
            "press_enter": False,
            "clear_before_typing": False,
        })
        assert result.success
        ex.page.keyboard.type.assert_awaited_once_with("hello world")

    async def test_key_combination(self):
        ex = self._make_executor()
        result = await ex.execute("key_combination", {"keys": "Control+C"})
        assert result.success
        ex.page.keyboard.press.assert_awaited_once_with("Control+C")

    async def test_navigate(self):
        ex = self._make_executor()
        result = await ex.execute("navigate", {"url": "https://google.com"})
        assert result.success
        ex.page.goto.assert_awaited_once_with("https://google.com")

    async def test_go_back(self):
        ex = self._make_executor()
        result = await ex.execute("go_back", {})
        assert result.success
        ex.page.go_back.assert_awaited_once()

    async def test_go_forward(self):
        ex = self._make_executor()
        result = await ex.execute("go_forward", {})
        assert result.success
        ex.page.go_forward.assert_awaited_once()

    async def test_scroll_document(self):
        ex = self._make_executor()
        result = await ex.execute("scroll_document", {"direction": "down"})
        assert result.success
        ex.page.mouse.wheel.assert_awaited_once()

    async def test_drag_and_drop(self):
        ex = self._make_executor(normalize=False)
        result = await ex.execute("drag_and_drop", {
            "x": 100, "y": 100,
            "destination_x": 500, "destination_y": 500,
        })
        assert result.success
        ex.page.mouse.down.assert_awaited_once()
        ex.page.mouse.up.assert_awaited_once()

    async def test_unimplemented_action(self):
        ex = self._make_executor()
        result = await ex.execute("nonexistent_action", {})
        assert not result.success
        assert "Unimplemented" in result.error

    async def test_capture_screenshot(self):
        ex = self._make_executor()
        data = await ex.capture_screenshot()
        assert data == b"\x89PNG_FAKE"

    def test_get_current_url(self):
        ex = self._make_executor()
        assert ex.get_current_url() == "https://example.com"

    async def test_wait_5_seconds(self):
        """wait_5_seconds should succeed (we don't actually wait 5s in tests)."""
        ex = self._make_executor()
        with patch("backend.engines.computer_use_engine.asyncio.sleep", new_callable=AsyncMock):
            result = await ex.execute("wait_5_seconds", {})
        assert result.success

    async def test_safety_decision_popped(self):
        """Safety decision in args should be extracted, not passed to handler."""
        ex = self._make_executor(normalize=False)
        result = await ex.execute("click_at", {
            "x": 100, "y": 200,
            "safety_decision": {
                "decision": "require_confirmation",
                "explanation": "Sensitive action",
            },
        })
        assert result.success
        assert result.safety_decision == SafetyDecision.REQUIRE_CONFIRMATION
        assert result.safety_explanation == "Sensitive action"


# ── DesktopExecutor ────────────────────────────────────────────────────────────

class TestDesktopExecutor(unittest.IsolatedAsyncioTestCase):
    """DesktopExecutor translates CU actions → agent_service HTTP calls."""

    def _make_executor(self, normalize: bool = True) -> DesktopExecutor:
        return DesktopExecutor(
            screen_width=1440,
            screen_height=900,
            normalize_coords=normalize,
            agent_service_url="http://127.0.0.1:9222",
            container_name="test-container",
        )

    async def test_click_at(self):
        ex = self._make_executor(normalize=False)
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "clicked"},
        ) as mock_post:
            result = await ex.execute("click_at", {"x": 720, "y": 450})
        assert result.success
        mock_post.assert_awaited_once_with({
            "action": "click", "coordinates": [720, 450], "mode": "desktop",
        })

    async def test_type_text_at(self):
        ex = self._make_executor(normalize=False)
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ) as mock_post:
            result = await ex.execute("type_text_at", {
                "x": 100, "y": 200,
                "text": "hello",
                "press_enter": True,
                "clear_before_typing": True,
            })
        assert result.success
        # Should call: click, hotkey(ctrl+a), key(BackSpace), type, key(Return)
        assert mock_post.call_count == 5

    async def test_key_combination(self):
        ex = self._make_executor()
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ) as mock_post:
            result = await ex.execute("key_combination", {"keys": "Control+C"})
        assert result.success
        mock_post.assert_awaited_once_with({
            "action": "key", "text": "ctrl+c", "mode": "desktop",
        })

    async def test_unimplemented_action(self):
        ex = self._make_executor()
        result = await ex.execute("nonexistent_desktop_action", {})
        assert not result.success
        assert "Unimplemented" in result.error

    async def test_scroll_document(self):
        ex = self._make_executor()
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ):
            result = await ex.execute("scroll_document", {"direction": "up"})
        assert result.success

    async def test_capture_screenshot_via_service(self):
        ex = self._make_executor()
        fake_b64 = base64.b64encode(b"\x89PNG_DESKTOP").decode()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"screenshot": fake_b64, "method": "desktop"})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch.object(ex, "_get_client", new_callable=AsyncMock, return_value=mock_client):
            data = await ex.capture_screenshot()
        assert data == b"\x89PNG_DESKTOP"

    async def test_double_click(self):
        """double_click should send a single 'double_click' action (no extra left click)."""
        ex = self._make_executor(normalize=False)
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "double-clicked"},
        ) as mock_post:
            result = await ex.execute("double_click", {"x": 100, "y": 200})
        assert result.success
        mock_post.assert_awaited_once_with({
            "action": "double_click", "coordinates": [100, 200], "mode": "desktop",
        })

    async def test_right_click(self):
        """right_click should send a single 'right_click' action (no extra left click)."""
        ex = self._make_executor(normalize=False)
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "right-clicked"},
        ) as mock_post:
            result = await ex.execute("right_click", {"x": 300, "y": 400})
        assert result.success
        mock_post.assert_awaited_once_with({
            "action": "right_click", "coordinates": [300, 400], "mode": "desktop",
        })

    async def test_triple_click(self):
        """triple_click should send double_click + click (no redundant extra click)."""
        ex = self._make_executor(normalize=False)
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ) as mock_post:
            result = await ex.execute("triple_click", {"x": 50, "y": 60})
        assert result.success
        assert mock_post.call_count == 2
        calls = mock_post.call_args_list
        assert calls[0].args[0]["action"] == "double_click"
        assert calls[1].args[0]["action"] == "click"

    async def test_type_at_cursor(self):
        """type_at_cursor should type without clicking/moving focus."""
        ex = self._make_executor()
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "typed"},
        ) as mock_post:
            result = await ex.execute("type_at_cursor", {
                "text": "hello", "press_enter": False,
            })
        assert result.success
        # Should only call type (no click, no hotkey, no key)
        mock_post.assert_awaited_once_with({
            "action": "type", "text": "hello", "mode": "desktop",
        })

    async def test_type_at_cursor_with_enter(self):
        """type_at_cursor with press_enter should type then press Return."""
        ex = self._make_executor()
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ) as mock_post:
            result = await ex.execute("type_at_cursor", {
                "text": "query", "press_enter": True,
            })
        assert result.success
        assert mock_post.call_count == 2
        calls = mock_post.call_args_list
        assert calls[0].args[0]["action"] == "type"
        assert calls[1].args[0] == {
            "action": "key", "text": "Return", "mode": "desktop",
        }

    def test_get_current_url(self):
        ex = self._make_executor()
        assert ex.get_current_url() == ""


# ── ComputerUseEngine facade ──────────────────────────────────────────────────

class TestComputerUseEngine:
    """ComputerUseEngine selects provider client + executor."""

    def test_gemini_browser_requires_page(self):
        engine = ComputerUseEngine(
            provider=Provider.GEMINI,
            api_key="fake-key",
            environment=Environment.BROWSER,
        )
        with pytest.raises(ValueError, match="requires a Playwright page"):
            engine._build_executor(page=None)

    def test_gemini_desktop_no_page_needed(self):
        engine = ComputerUseEngine(
            provider=Provider.GEMINI,
            api_key="fake-key",
            environment=Environment.DESKTOP,
        )
        executor = engine._build_executor(page=None)
        assert isinstance(executor, DesktopExecutor)
        assert executor._normalize is True  # Gemini = normalize

    def test_claude_browser_builds_playwright_executor(self):
        page = AsyncMock()
        engine = ComputerUseEngine(
            provider=Provider.CLAUDE,
            api_key="fake-key",
            environment=Environment.BROWSER,
            tool_version="computer_20251124",
            beta_flag=["computer-use-2025-11-24"],
        )
        executor = engine._build_executor(page=page)
        assert isinstance(executor, PlaywrightExecutor)
        assert executor._normalize is False  # Claude = real pixels

    def test_claude_desktop_builds_desktop_executor(self):
        engine = ComputerUseEngine(
            provider=Provider.CLAUDE,
            api_key="fake-key",
            environment=Environment.DESKTOP,
            tool_version="computer_20251124",
            beta_flag=["computer-use-2025-11-24"],
        )
        executor = engine._build_executor(page=None)
        assert isinstance(executor, DesktopExecutor)
        assert executor._normalize is False  # Claude = real pixels

    def test_unsupported_provider(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            ComputerUseEngine(provider="openai", api_key="fake")

    def test_custom_model(self):
        engine = ComputerUseEngine(
            provider=Provider.GEMINI,
            api_key="fake-key",
            model="gemini-custom-model",
            environment=Environment.BROWSER,
        )
        assert engine._client._model == "gemini-custom-model"

    def test_custom_screen_dimensions(self):
        engine = ComputerUseEngine(
            provider=Provider.CLAUDE,
            api_key="fake-key",
            environment=Environment.DESKTOP,
            screen_width=1920,
            screen_height=1080,
            tool_version="computer_20251124",
            beta_flag=["computer-use-2025-11-24"],
        )
        assert engine.screen_width == 1920
        assert engine.screen_height == 1080
        executor = engine._build_executor()
        assert executor.screen_width == 1920
        assert executor.screen_height == 1080


# ── CUActionResult / CUTurnRecord ────────────────────────────────────────────

class TestDataClasses:
    """CUActionResult and CUTurnRecord dataclass behavior."""

    def test_action_result_defaults(self):
        r = CUActionResult(name="click_at")
        assert r.success is True
        assert r.error is None
        assert r.safety_decision is None

    def test_action_result_error(self):
        r = CUActionResult(name="click_at", success=False, error="Element not found")
        assert not r.success
        assert r.error == "Element not found"

    def test_turn_record(self):
        r = CUTurnRecord(
            turn=1,
            model_text="Clicking the button",
            actions=[CUActionResult(name="click_at")],
            screenshot_b64="base64data",
        )
        assert r.turn == 1
        assert len(r.actions) == 1
        assert r.screenshot_b64 == "base64data"


# ── Integration: Engine registration ──────────────────────────────────────────

class TestEngineRegistration:
    """Verify computer_use is registered across the system."""

    def test_automation_engine_enum(self):
        from backend.models import AutomationEngine
        assert AutomationEngine.COMPUTER_USE.value == "computer_use"

    def test_all_engines_includes_computer_use(self):
        from backend.engine_capabilities import ALL_ENGINES
        assert "computer_use" in ALL_ENGINES

    def test_supported_engines_includes_computer_use(self):
        from backend.tools.router import SUPPORTED_ENGINES
        assert "computer_use" in SUPPORTED_ENGINES

    def test_validate_engine_accepts_computer_use(self):
        from backend.tools.router import validate_engine
        assert validate_engine("computer_use") == "computer_use"

    def test_system_prompt_exists(self):
        from backend.agent.prompts import get_system_prompt
        prompt = get_system_prompt("computer_use")
        assert "computer_use" in prompt.lower() or "computer-using agent" in prompt.lower()

    def test_executor_rejects_computer_use(self):
        """execute_action should reject computer_use engine (it has its own loop)."""
        from backend.agent.executor import execute_action
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                execute_action({"action": "click"}, engine="computer_use")
            )
        finally:
            loop.close()
        assert not result["success"]
        assert "wrong_dispatch_path" in str(result)


# ── Claude action mapping ────────────────────────────────────────────────────

class TestClaudeActionMapping(unittest.IsolatedAsyncioTestCase):
    """ClaudeCUClient._execute_claude_action maps Claude → CU executor actions."""

    async def test_screenshot_action(self):
        """Claude screenshot action returns immediately (no executor call)."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        result = await client._execute_claude_action({"action": "screenshot"}, executor)
        assert result.name == "screenshot"
        assert result.success

    async def test_click_maps_to_click_at(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value=CUActionResult(name="click_at"))
        result = await client._execute_claude_action(
            {"action": "click", "coordinate": [100, 200]}, executor
        )
        executor.execute.assert_awaited_once_with("click_at", {"x": 100, "y": 200})
        assert result.name == "click_at"

    async def test_scroll_maps_to_scroll_at(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value=CUActionResult(name="scroll_at"))
        result = await client._execute_claude_action(
            {"action": "scroll", "coordinate": [500, 400], "direction": "down", "amount": 3},
            executor,
        )
        executor.execute.assert_awaited_once()
        call_args = executor.execute.call_args
        assert call_args[0][0] == "scroll_at"
        assert call_args[0][1]["direction"] == "down"

    async def test_key_maps_to_key_combination(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value=CUActionResult(name="key_combination"))
        result = await client._execute_claude_action(
            {"action": "key", "key": "Return"}, executor
        )
        executor.execute.assert_awaited_once()
        call_args = executor.execute.call_args
        assert call_args[0][0] == "key_combination"
        assert call_args[0][1]["keys"] == "Enter"  # Return → Enter mapping

    async def test_mouse_move_maps_to_hover_at(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        executor.execute = AsyncMock(return_value=CUActionResult(name="hover_at"))
        result = await client._execute_claude_action(
            {"action": "mouse_move", "coordinate": [300, 400]}, executor
        )
        executor.execute.assert_awaited_once()
        assert executor.execute.call_args[0][0] == "hover_at"

    async def test_unknown_action_returns_error(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        executor = AsyncMock()
        result = await client._execute_claude_action(
            {"action": "unknown_action"}, executor
        )
        assert not result.success
        assert "Unknown Claude action" in result.error

    async def test_type_desktop_uses_type_at_cursor(self):
        """Claude 'type' on desktop should use type_at_cursor (no coordinate click)."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        # Desktop executor has no .page attribute
        executor = MagicMock(spec=DesktopExecutor)
        del executor.page  # Ensure no page attribute
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.execute = AsyncMock(return_value=CUActionResult(name="type_at_cursor"))
        result = await client._execute_claude_action(
            {"action": "type", "text": "hello world"}, executor
        )
        executor.execute.assert_awaited_once()
        call_args = executor.execute.call_args
        assert call_args[0][0] == "type_at_cursor"
        assert call_args[0][1]["text"] == "hello world"
        assert call_args[0][1]["press_enter"] is False

    async def test_special_click_desktop_no_extra_left_click(self):
        """double_click/right_click desktop should NOT perform an extra left click."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        # Desktop executor has no .page attribute
        executor = MagicMock(spec=DesktopExecutor)
        del executor.page  # Ensure no page attribute
        executor.execute = AsyncMock(return_value=CUActionResult(
            name="double_click", extra={"pixel_x": 100, "pixel_y": 200},
        ))
        result = await client._execute_claude_action(
            {"action": "double_click", "coordinate": [100, 200]}, executor
        )
        # Should call executor.execute exactly once with "double_click"
        # (previously it would call click_at + _post_action = 2 actions)
        executor.execute.assert_awaited_once()
        call_args = executor.execute.call_args
        assert call_args[0][0] == "double_click"
        assert call_args[0][1] == {"x": 100, "y": 200}


# ── A1: DesktopExecutor success=false propagation ─────────────────────────────

class TestDesktopExecutorSuccessFalsePropagation(unittest.IsolatedAsyncioTestCase):
    """DesktopExecutor must surface {success: false} from agent_service."""

    async def test_post_action_returns_success_false(self):
        """If _post_action returns {success: False, message: ...}, result must be failure."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=False,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": False, "message": "no display"},
        ):
            result = await ex.execute("click_at", {"x": 100, "y": 200})
        assert result.success is False
        assert "no display" in result.error

    async def test_post_action_returns_success_true(self):
        """If _post_action returns {success: True}, result should be success."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=False,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True, "message": "ok"},
        ):
            result = await ex.execute("click_at", {"x": 100, "y": 200})
        assert result.success is True

    async def test_post_action_returns_no_success_field(self):
        """If _post_action returns dict without 'success', treat as success."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=False,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"message": "done"},
        ):
            result = await ex.execute("click_at", {"x": 100, "y": 200})
        assert result.success is True


# ── A2: Key combination normalization ─────────────────────────────────────────

class TestDesktopKeyNormalization(unittest.IsolatedAsyncioTestCase):
    """DesktopExecutor _act_key_combination normalizes single letters."""

    async def test_control_l_becomes_ctrl_l(self):
        """'Control+L' should become 'ctrl+l' in the payload."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=True,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_post:
            result = await ex.execute("key_combination", {"keys": "Control+L"})
        assert result.success
        payload = mock_post.call_args[0][0]
        assert payload["text"] == "ctrl+l"

    async def test_shift_x_lowercased(self):
        """'Shift+X' should lower-case the single letter: 'shift+x'."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=True,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_post:
            await ex.execute("key_combination", {"keys": "Shift+X"})
        payload = mock_post.call_args[0][0]
        assert payload["text"] == "shift+x"

    async def test_special_keys_preserved(self):
        """Special tokens like Return, BackSpace should NOT be lowered."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=True,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        with patch.object(
            ex, "_post_action", new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_post:
            await ex.execute("key_combination", {"keys": "Alt+F4"})
        payload = mock_post.call_args[0][0]
        assert payload["text"] == "alt+F4"


# ── A3: httpx client aclose ──────────────────────────────────────────────────

class TestDesktopExecutorAclose(unittest.IsolatedAsyncioTestCase):
    """DesktopExecutor.aclose() must close the httpx client."""

    async def test_aclose_closes_client(self):
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=True,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        mock_client = AsyncMock()
        mock_client.is_closed = False
        ex._client = mock_client
        await ex.aclose()
        mock_client.aclose.assert_awaited_once()
        assert ex._client is None

    async def test_aclose_noop_when_no_client(self):
        """aclose() should be safe to call when no client exists."""
        ex = DesktopExecutor(
            screen_width=1440, screen_height=900, normalize_coords=True,
            agent_service_url="http://127.0.0.1:9222", container_name="test",
        )
        # Should not raise
        await ex.aclose()


# ── Claude tool version configuration (allowlist-driven) ─────────────────────

class TestClaudeToolVersionConfiguration:
    """ClaudeCUClient._build_tools returns the explicitly configured tool type."""

    def _make_client(self, model: str, tool_version: str, beta_flag: str) -> ClaudeCUClient:
        """Construct a ClaudeCUClient without hitting Anthropic import."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        client._client = MagicMock()
        client._model = model
        client._system_prompt = ""
        client._tool_version = tool_version
        client._beta_flags = [beta_flag]
        client._beta_flag = beta_flag
        return client

    def test_explicit_legacy_tool_version_supported(self):
        c = self._make_client(
            "claude-sonnet-4-20250514",
            "computer_20250124",
            "computer-use-2025-01-24",
        )
        assert c._tool_version == "computer_20250124"
        assert c._beta_flag == "computer-use-2025-01-24"
        tools = c._build_tools(1024, 768)
        assert tools[0]["type"] == "computer_20250124"
        assert "display_number" not in tools[0]

    def test_sonnet_46_uses_allowlist_tool_version(self):
        c = self._make_client(
            "claude-sonnet-4-6",
            "computer_20251124",
            "computer-use-2025-11-24",
        )
        assert c._tool_version == "computer_20251124"
        assert c._beta_flag == "computer-use-2025-11-24"

    def test_opus_46_uses_allowlist_tool_version(self):
        c = self._make_client(
            "claude-opus-4-6",
            "computer_20251124",
            "computer-use-2025-11-24",
        )
        assert c._tool_version == "computer_20251124"
        assert c._beta_flag == "computer-use-2025-11-24"

    def test_explicit_metadata_supported_for_non_allowlisted_models(self):
        c = self._make_client(
            "claude-opus-4-5",
            "computer_20251124",
            "computer-use-2025-11-24",
        )
        assert c._tool_version == "computer_20251124"
        assert c._beta_flag == "computer-use-2025-11-24"

    def test_explicit_legacy_metadata_supported_for_other_models(self):
        c = self._make_client(
            "claude-haiku-4-5-20250101",
            "computer_20250124",
            "computer-use-2025-01-24",
        )
        assert c._tool_version == "computer_20250124"

    def test_build_tools_no_display_number(self):
        """_build_tools must NOT include display_number (Fix 5)."""
        c = self._make_client(
            "claude-sonnet-4-20250514",
            "computer_20250124",
            "computer-use-2025-01-24",
        )
        tools = c._build_tools(1920, 1080)
        assert "display_number" not in tools[0]
        assert tools[0]["display_width_px"] == 1920
        assert tools[0]["display_height_px"] == 1080


# ── Claude beta API call (Fix 1) ─────────────────────────────────────────────

class TestClaudeBetaApiCall(unittest.IsolatedAsyncioTestCase):
    """ClaudeCUClient must use client.beta.messages.create with betas."""

    async def test_beta_messages_create_called_with_betas(self):
        """run_loop must call beta.messages.create, not messages.create."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        client._model = "claude-sonnet-4-20250514"
        client._system_prompt = ""
        client._tool_version = "computer_20250124"
        client._beta_flags = ["computer-use-2025-01-24"]
        client._beta_flag = "computer-use-2025-01-24"

        # Mock the beta messages endpoint to return a terminal response
        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done"
        mock_response.content = [text_block]
        mock_response.stop_reason = "end_turn"

        mock_beta_create = MagicMock(return_value=mock_response)
        client._client = MagicMock()
        client._client.beta.messages.create = mock_beta_create

        # Mock executor
        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n")

        result = await client.run_loop("test task", executor, turn_limit=1)

        # Verify beta.messages.create was called (not messages.create)
        mock_beta_create.assert_called_once()
        call_kwargs = mock_beta_create.call_args
        # Check betas parameter
        assert call_kwargs.kwargs.get("betas") == ["computer-use-2025-01-24"]

    async def test_46_model_uses_new_beta_flag(self):
        """Claude 4.6 model should use computer-use-2025-11-24 beta."""
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        client._model = "claude-sonnet-4-6"
        client._system_prompt = ""
        client._tool_version = "computer_20251124"
        client._beta_flags = ["computer-use-2025-11-24"]
        client._beta_flag = "computer-use-2025-11-24"

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done"
        mock_response.content = [text_block]
        mock_response.stop_reason = "end_turn"

        mock_beta_create = MagicMock(return_value=mock_response)
        client._client = MagicMock()
        client._client.beta.messages.create = mock_beta_create

        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n")

        await client.run_loop("test", executor, turn_limit=1)

        call_kwargs = mock_beta_create.call_args
        assert call_kwargs.kwargs.get("betas") == ["computer-use-2025-11-24"]


# ── Claude thinking enabled (Fix 4) ──────────────────────────────────────────

class TestClaudeThinkingEnabled(unittest.IsolatedAsyncioTestCase):
    """ClaudeCUClient must pass thinking config to API call."""

    async def test_thinking_param_present(self):
        client = ClaudeCUClient.__new__(ClaudeCUClient)
        client._anthropic = MagicMock()
        client._model = "claude-sonnet-4-20250514"
        client._system_prompt = ""
        client._tool_version = "computer_20250124"
        client._beta_flags = ["computer-use-2025-01-24"]
        client._beta_flag = "computer-use-2025-01-24"

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done"
        mock_response.content = [text_block]
        mock_response.stop_reason = "end_turn"

        mock_beta_create = MagicMock(return_value=mock_response)
        client._client = MagicMock()
        client._client.beta.messages.create = mock_beta_create

        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n")

        await client.run_loop("test", executor, turn_limit=1)

        call_kwargs = mock_beta_create.call_args
        thinking = call_kwargs.kwargs.get("thinking")
        assert thinking is not None
        assert thinking["type"] == "enabled"
        assert thinking["budget_tokens"] == 4096


# ── FunctionResponse screenshot inline (not separate Part) ─────────────────────

class TestGeminiFunctionResponseScreenshot(unittest.IsolatedAsyncioTestCase):
    """Screenshot must be embedded inside FunctionResponse.parts, NOT as a
    separate Part.from_bytes().  See:
    https://ai.google.dev/gemini-api/docs/computer-use
    """

    async def test_screenshot_inside_function_response_not_separate_part(self):
        """Verify screenshot bytes go into FunctionResponseBlob, not Part.from_bytes."""
        client = GeminiCUClient.__new__(GeminiCUClient)
        client._model = "gemini-3-flash-preview"

        mock_types = MagicMock()
        client._types = mock_types
        client._genai = MagicMock()
        client._client = MagicMock()
        client._environment = Environment.BROWSER
        client._excluded = []
        client._system_instruction = None

        # Build a response with a single function call
        fc = MagicMock()
        fc.name = "click_at"
        fc.args = {"x": 500, "y": 300}

        candidate = MagicMock()
        fc_part = MagicMock()
        fc_part.function_call = fc
        fc_part.text = None
        text_part = MagicMock()
        text_part.function_call = None
        text_part.text = "clicking"
        candidate.content.parts = [text_part, fc_part]

        response = MagicMock()
        response.candidates = [candidate]

        # Second call returns done (no function calls)
        done_part = MagicMock()
        done_part.function_call = None
        done_part.text = "Done"
        done_candidate = MagicMock()
        done_candidate.content.parts = [done_part]
        done_response = MagicMock()
        done_response.candidates = [done_candidate]

        client._client.models.generate_content = MagicMock(
            side_effect=[response, done_response]
        )

        # Executor stubs
        screenshot_bytes = b"\x89PNG\r\n" + b"\x00" * 120
        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.execute = AsyncMock(return_value=CUActionResult(name="click_at"))
        executor.capture_screenshot = AsyncMock(return_value=screenshot_bytes)
        executor.get_current_url = MagicMock(return_value="http://test.com")

        client._build_config = MagicMock()

        # Track calls to types to verify the screenshot embedding pattern
        fr_blob_calls = []
        fr_part_calls = []
        fr_calls = []

        def _track_blob(**kwargs):
            fr_blob_calls.append(kwargs)
            return MagicMock()

        def _track_fr_part(**kwargs):
            fr_part_calls.append(kwargs)
            return MagicMock()

        def _track_fr(**kwargs):
            fr_calls.append(kwargs)
            return MagicMock()

        mock_types.FunctionResponseBlob = _track_blob
        mock_types.FunctionResponsePart = _track_fr_part
        mock_types.FunctionResponse = _track_fr
        mock_types.Content = MagicMock()
        mock_types.Part = MagicMock()
        mock_types.Part.from_bytes = MagicMock(return_value=MagicMock())

        with patch("backend.engines.computer_use_engine.asyncio.to_thread",
                    side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            await client.run_loop("click btn", executor, turn_limit=2)

        # 1. FunctionResponseBlob must have been called with the screenshot
        assert len(fr_blob_calls) >= 1, "FunctionResponseBlob was never called"
        assert fr_blob_calls[0]["mime_type"] == "image/png"
        assert fr_blob_calls[0]["data"] == screenshot_bytes

        # 2. FunctionResponsePart must wrap the blob via inline_data
        assert len(fr_part_calls) >= 1, "FunctionResponsePart was never called"
        assert "inline_data" in fr_part_calls[0]

        # 3. FunctionResponse must have parts= with the inline screenshot
        assert len(fr_calls) >= 1, "FunctionResponse was never called"
        assert "parts" in fr_calls[0], "FunctionResponse missing 'parts' kwarg"
        assert fr_calls[0]["name"] == "click_at"

        # 4. Part.from_bytes must NOT have been called for the screenshot
        #    (it's only called for the initial screenshot in the first message)
        from_bytes_calls = mock_types.Part.from_bytes.call_count
        assert from_bytes_calls <= 1, (
            f"Part.from_bytes called {from_bytes_calls} times — "
            "screenshot should be inside FunctionResponse.parts, not as separate Part"
        )

    async def test_no_screenshot_omits_parts_from_function_response(self):
        """When screenshot is empty/too small, FunctionResponse has no parts."""
        client = GeminiCUClient.__new__(GeminiCUClient)
        client._model = "gemini-3-flash-preview"

        mock_types = MagicMock()
        client._types = mock_types
        client._genai = MagicMock()
        client._client = MagicMock()
        client._environment = Environment.BROWSER
        client._excluded = []
        client._system_instruction = None

        fc = MagicMock()
        fc.name = "click_at"
        fc.args = {"x": 100, "y": 200}

        candidate = MagicMock()
        fc_part = MagicMock()
        fc_part.function_call = fc
        fc_part.text = None
        candidate.content.parts = [fc_part]

        response = MagicMock()
        response.candidates = [candidate]

        done_part = MagicMock()
        done_part.function_call = None
        done_part.text = "Done"
        done_candidate = MagicMock()
        done_candidate.content.parts = [done_part]
        done_response = MagicMock()
        done_response.candidates = [done_candidate]

        client._client.models.generate_content = MagicMock(
            side_effect=[response, done_response]
        )

        # First call: valid bytes for initial screenshot; second call: empty
        valid_screenshot = b"\x89PNG\r\n" + b"\x00" * 120
        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.execute = AsyncMock(return_value=CUActionResult(name="click_at"))
        executor.capture_screenshot = AsyncMock(side_effect=[valid_screenshot, b""])
        executor.get_current_url = MagicMock(return_value="http://test.com")

        client._build_config = MagicMock()

        fr_calls = []

        def _track_fr(**kwargs):
            fr_calls.append(kwargs)
            return MagicMock()

        mock_types.FunctionResponse = _track_fr
        mock_types.Content = MagicMock()
        mock_types.Part = MagicMock()
        mock_types.Part.from_bytes = MagicMock(return_value=MagicMock())

        with patch("backend.engines.computer_use_engine.asyncio.to_thread",
                    side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            await client.run_loop("click", executor, turn_limit=2)

        # FunctionResponse should NOT have 'parts' key when screenshot is empty
        assert len(fr_calls) >= 1
        assert "parts" not in fr_calls[0], (
            "FunctionResponse should not have 'parts' when screenshot is empty/small"
        )


# ── Safety pop before executor (Fix 3) ────────────────────────────────────────

class TestGeminiSafetyPopBeforeExecutor(unittest.IsolatedAsyncioTestCase):
    """GeminiCUClient extracts safety_decision before calling executor."""

    async def test_safety_confirmed_stamps_result(self):
        """When user confirms, result.safety_decision must be set."""
        client = GeminiCUClient.__new__(GeminiCUClient)
        client._model = "gemini-3-flash-preview"

        mock_types = MagicMock()
        client._types = mock_types
        client._genai = MagicMock()
        client._client = MagicMock()
        client._environment = Environment.BROWSER
        client._excluded = []
        client._system_instruction = None

        # Build a fake response with a function call that includes safety_decision
        fc = MagicMock()
        fc.name = "click_at"
        fc.args = {
            "x": 100, "y": 200,
            "safety_decision": {"decision": "require_confirmation", "explanation": "Risky click"}
        }

        candidate = MagicMock()
        text_part = MagicMock()
        text_part.function_call = None
        text_part.text = "clicking"
        fc_part = MagicMock()
        fc_part.function_call = fc
        fc_part.text = None
        candidate.content.parts = [text_part, fc_part]

        response = MagicMock()
        response.candidates = [candidate]

        # Second call returns no function calls (done)
        done_candidate = MagicMock()
        done_text = MagicMock()
        done_text.function_call = None
        done_text.text = "Completed"
        done_candidate.content.parts = [done_text]
        done_response = MagicMock()
        done_response.candidates = [done_candidate]

        client._client.models.generate_content = MagicMock(
            side_effect=[response, done_response]
        )

        # Mock executor that records args
        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        capture_result = CUActionResult(name="click_at")
        executor.execute = AsyncMock(return_value=capture_result)
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n" + b"\x00" * 120)
        executor.get_current_url = MagicMock(return_value="http://test.com")

        # User confirms safety
        confirmed_safety = True

        # Mock _build_config
        client._build_config = MagicMock()

        # Patch asyncio.to_thread to call synchronously
        with patch("backend.engines.computer_use_engine.asyncio.to_thread",
                    side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            # Mock types for FunctionResponse construction
            mock_types.Part = MagicMock()
            mock_types.Part.from_bytes = MagicMock(return_value=MagicMock())
            mock_types.Content = MagicMock()

            async def _confirm(explanation):
                return confirmed_safety

            result = await client.run_loop(
                "click the button",
                executor,
                turn_limit=2,
                on_safety=_confirm,
            )

        # The executor should NOT have received safety_decision in args
        execute_call_args = executor.execute.call_args[0][1]
        assert "safety_decision" not in execute_call_args

        # The capture_result.safety_decision should have been stamped
        assert capture_result.safety_decision == SafetyDecision.REQUIRE_CONFIRMATION

    async def test_safety_denied_terminates(self):
        """When user denies safety, the loop terminates immediately."""
        client = GeminiCUClient.__new__(GeminiCUClient)
        client._model = "gemini-3-flash-preview"

        mock_types = MagicMock()
        client._types = mock_types
        client._genai = MagicMock()
        client._client = MagicMock()
        client._environment = Environment.BROWSER
        client._excluded = []
        client._system_instruction = None

        fc = MagicMock()
        fc.name = "navigate"
        fc.args = {
            "url": "http://evil.com",
            "safety_decision": {"decision": "require_confirmation", "explanation": "Dangerous URL"}
        }

        candidate = MagicMock()
        fc_part = MagicMock()
        fc_part.function_call = fc
        fc_part.text = None
        candidate.content.parts = [fc_part]

        response = MagicMock()
        response.candidates = [candidate]

        client._client.models.generate_content = MagicMock(return_value=response)

        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n" + b"\x00" * 120)
        executor.get_current_url = MagicMock(return_value="")

        client._build_config = MagicMock()

        with patch("backend.engines.computer_use_engine.asyncio.to_thread",
                    side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            mock_types.Part = MagicMock()
            mock_types.Part.from_bytes = MagicMock(return_value=MagicMock())
            mock_types.Content = MagicMock()

            async def _deny(explanation):
                return False

            final = await client.run_loop(
                "go to evil.com",
                executor,
                turn_limit=2,
                on_safety=_deny,  # DENY
            )

        assert "terminated" in final.lower()
        # executor.execute should NOT have been called (safety denied before execution)
        executor.execute.assert_not_awaited()

    async def test_on_safety_is_awaited(self):
        """Regression for F-001: on_safety must be awaited as a coroutine.

        Previously the call site asyncio.iscoroutinefunction(on_safety) check
        did not detect bound methods / partials whose underlying coroutine
        was wrapped, and silently auto-approved the unawaited bool-coroutine
        as truthy. Now the signature is typed ``Callable[[str], Awaitable[bool]]``
        and the call is unconditionally awaited.
        """
        client = GeminiCUClient.__new__(GeminiCUClient)
        client._model = "gemini-3-flash-preview"

        mock_types = MagicMock()
        client._types = mock_types
        client._genai = MagicMock()
        client._client = MagicMock()
        client._environment = Environment.BROWSER
        client._excluded = []
        client._system_instruction = None

        fc = MagicMock()
        fc.name = "navigate"
        fc.args = {
            "url": "http://evil.com",
            "safety_decision": {"decision": "require_confirmation", "explanation": "danger"},
        }

        candidate = MagicMock()
        fc_part = MagicMock()
        fc_part.function_call = fc
        fc_part.text = None
        candidate.content.parts = [fc_part]

        response = MagicMock()
        response.candidates = [candidate]

        client._client.models.generate_content = MagicMock(return_value=response)

        executor = AsyncMock()
        executor.screen_width = 1440
        executor.screen_height = 900
        executor.capture_screenshot = AsyncMock(return_value=b"\x89PNG\r\n" + b"\x00" * 120)
        executor.get_current_url = MagicMock(return_value="")

        client._build_config = MagicMock()

        on_safety = AsyncMock(return_value=False)

        with patch(
            "backend.engines.computer_use_engine.asyncio.to_thread",
            side_effect=lambda fn, *a, **kw: fn(*a, **kw),
        ):
            mock_types.Part = MagicMock()
            mock_types.Part.from_bytes = MagicMock(return_value=MagicMock())
            mock_types.Content = MagicMock()

            final = await client.run_loop(
                "go to evil.com",
                executor,
                turn_limit=2,
                on_safety=on_safety,
            )

        assert on_safety.await_count == 1, (
            f"on_safety must be awaited exactly once; got {on_safety.await_count}"
        )
        on_safety.assert_awaited_with("danger")
        assert "terminated" in final.lower()
        executor.execute.assert_not_awaited()
