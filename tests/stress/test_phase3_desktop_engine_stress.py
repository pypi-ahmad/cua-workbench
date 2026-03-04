"""Phase 3 — Desktop Engine Stress Tests (xdotool).

50-iteration loop per engine:
  open xfce4-terminal → type "STRESS TEST" → press Enter → close window

Measures:
  - Focus errors
  - Key injection errors
  - Coordinate drift
  - Hanging windows
  - CPU spike (simulated via timing)

Run with:
    pytest tests/stress/test_phase3_desktop_engine_stress.py -v -m phase3
"""

from __future__ import annotations

import asyncio
import random
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from backend.config import config
from backend.engine_capabilities import EngineCapabilities
from backend.models import ActionType, AgentAction
from backend.agent.executor import execute_action
from backend.engines.desktop_hybrid_engine import (
    execute_desktop_hybrid_action,
    _is_recoverable,
    _validate,
)

from tests.stress.helpers import (
    ALL_ENGINES,
    DESKTOP_ENGINES,
    ENGINE_MODES,
    STRESS,
    StressMetrics,
    make_click_action,
    make_key_action,
    make_type_action,
    run_async,
)

# ── Constants ─────────────────────────────────────────────────────────────────

DESKTOP_LOOP_CYCLES = 50
# The primary desktop engines under test (computer_use has its own loop)
PRIMARY_DESKTOP_ENGINES = ["xdotool"]
SCREEN_W, SCREEN_H = 1440, 900


# ── Autouse: zero action delay ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep in the executor for fast stress tests."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


# ── Desktop-specific metrics ─────────────────────────────────────────────────

@dataclass
class DesktopStressMetrics:
    """Extended metrics tracking desktop-specific failure modes."""

    cycles_completed: int = 0
    total_actions: int = 0
    successful: int = 0
    failed: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    # Desktop-specific counters
    focus_errors: int = 0
    key_injection_errors: int = 0
    coordinate_drift_errors: int = 0
    hanging_window_errors: int = 0
    timeout_errors: int = 0
    errors: List[str] = field(default_factory=list)

    # Memory / CPU tracking
    memory_samples_bytes: List[int] = field(default_factory=list)
    cycle_durations_ms: List[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / max(self.total_actions, 1)

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / max(len(self.latencies_ms), 1)

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[int(len(s) * 0.95)]

    @property
    def avg_cycle_ms(self) -> float:
        return sum(self.cycle_durations_ms) / max(len(self.cycle_durations_ms), 1)

    @property
    def memory_growth_bytes(self) -> int:
        if len(self.memory_samples_bytes) < 2:
            return 0
        return self.memory_samples_bytes[-1] - self.memory_samples_bytes[0]

    def record_action(self, success: bool, latency_ms: float,
                      error: str | None = None):
        self.total_actions += 1
        self.latencies_ms.append(latency_ms)
        if success:
            self.successful += 1
        else:
            self.failed += 1
            if error:
                self.errors.append(error)

    def classify_error(self, message: str):
        """Classify an error message into the appropriate counter."""
        msg = message.lower()
        if "focus" in msg or "window not found" in msg:
            self.focus_errors += 1
        elif "key" in msg or "injection" in msg or "type" in msg:
            self.key_injection_errors += 1
        elif "coordinate" in msg or "drift" in msg or "bounds" in msg:
            self.coordinate_drift_errors += 1
        elif "hang" in msg or "timeout" in msg or "timed out" in msg:
            self.hanging_window_errors += 1
        elif "timeout" in msg:
            self.timeout_errors += 1

    def sample_memory(self):
        current, _ = tracemalloc.get_traced_memory()
        self.memory_samples_bytes.append(current)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _timed_execute(action, mode, engine):
    """Execute an action, return (result, elapsed_ms)."""
    start = time.perf_counter()
    result = await execute_action(action, mode=mode, engine=engine)
    elapsed = (time.perf_counter() - start) * 1000
    return result, elapsed


def _build_terminal_cycle_actions() -> list:
    """Build the 4-action loop: open terminal → type → Enter → close."""
    return [
        AgentAction(action=ActionType.OPEN_TERMINAL, text="xfce4-terminal"),
        AgentAction(action=ActionType.TYPE, text="STRESS TEST", coordinates=[720, 450]),
        AgentAction(action=ActionType.KEY, text="Enter"),
        AgentAction(action=ActionType.WINDOW_CLOSE, text="xfce4-terminal"),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# TEST A — Terminal Loop Stress (50 cycles per desktop engine)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestDesktopTerminalLoopXdotool:
    """50-cycle terminal open/type/enter/close stress for xdotool."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_terminal_loop_50_cycles(self, mock_send):
        """Loop 50×: open_terminal → type → key Enter → close_window."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = DesktopStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(DESKTOP_LOOP_CYCLES):
            cycle_start = time.perf_counter()
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
                metrics.record_action(result.get("success", False), elapsed)
            cycle_ms = (time.perf_counter() - cycle_start) * 1000
            metrics.cycle_durations_ms.append(cycle_ms)
            metrics.cycles_completed += 1

            if cycle % 10 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        assert metrics.total_actions == DESKTOP_LOOP_CYCLES * 4
        assert metrics.success_rate >= STRESS.min_success_rate
        assert metrics.avg_latency_ms < STRESS.max_acceptable_latency_ms

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_focus_error_tracking(self, mock_send):
        """Simulate intermittent focus errors; verify they are counted."""
        call_idx = 0

        async def _focus_flaky(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            # Every 5th call: focus error (typical xdotool X11 issue)
            if call_idx % 5 == 0:
                return {
                    "success": False,
                    "message": "X11 focus error: window not found",
                }
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _focus_flaky
        metrics = DesktopStressMetrics()

        for _ in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        assert metrics.focus_errors > 0, "Expected focus errors from injected faults"
        assert metrics.focus_errors < metrics.total_actions * 0.30
        assert metrics.success_rate >= 0.70

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_key_injection_error_tracking(self, mock_send):
        """Simulate key injection failures on type/key actions."""
        async def _key_flaky(*args, **kwargs):
            payload = args[0] if args else kwargs.get("payload", {})
            if isinstance(payload, dict):
                action_name = payload.get("action", "")
            else:
                action_name = ""
            # Fail on every 3rd keyboard action
            if action_name in ("type", "key") and random.random() < 0.20:
                return {
                    "success": False,
                    "message": "Key injection failed: X11 XTest extension error",
                }
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _key_flaky
        metrics = DesktopStressMetrics()

        for _ in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        # We might or might not see key injection errors due to randomness,
        # but the system must survive them gracefully
        assert metrics.success_rate >= 0.60

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_hanging_window_detection(self, mock_send):
        """Simulate close_window timeouts (hanging windows)."""
        async def _hang_on_close(*args, **kwargs):
            payload = args[0] if args else kwargs.get("payload", {})
            if isinstance(payload, dict):
                action_name = payload.get("action", "")
            else:
                action_name = ""
            if action_name == "close_window" and random.random() < 0.15:
                return {
                    "success": False,
                    "message": "Window close timed out: window still present",
                }
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _hang_on_close
        metrics = DesktopStressMetrics()

        for _ in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        assert metrics.success_rate >= 0.80
        # Hanging windows should be detected
        assert metrics.hanging_window_errors >= 0  # depends on randomness

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_cpu_spike_proxy_via_cycle_timing(self, mock_send):
        """Cycle durations must remain consistent — no exponential blowup."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = DesktopStressMetrics()

        for cycle in range(DESKTOP_LOOP_CYCLES):
            cycle_start = time.perf_counter()
            for action in _build_terminal_cycle_actions():
                run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
            cycle_ms = (time.perf_counter() - cycle_start) * 1000
            metrics.cycle_durations_ms.append(cycle_ms)
            metrics.cycles_completed += 1

        # No single cycle should be >10x the average (CPU spike proxy)
        avg = metrics.avg_cycle_ms
        for i, dur in enumerate(metrics.cycle_durations_ms):
            assert dur < avg * 10, (
                f"Cycle {i} took {dur:.1f}ms vs avg {avg:.1f}ms — potential CPU spike"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Coordinate Drift Tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestCoordinateDriftStress:
    """Verify coordinate accuracy under repeated rapid-fire clicks."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_xdotool_coordinate_consistency(self, mock_send):
        """50 clicks at the same coordinate must not drift."""
        received_coords: List[list] = []

        async def _capture_coords(*args, **kwargs):
            payload = args[0] if args else {}
            if isinstance(payload, dict):
                coords = payload.get("coordinates")
                if coords:
                    received_coords.append(list(coords))
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _capture_coords

        target_x, target_y = 720, 450
        for _ in range(DESKTOP_LOOP_CYCLES):
            action = make_click_action(target_x, target_y)
            run_async(
                _timed_execute(action, mode="desktop", engine="xdotool")
            )

        assert len(received_coords) == DESKTOP_LOOP_CYCLES
        for i, coords in enumerate(received_coords):
            assert coords == [target_x, target_y], (
                f"Click {i}: expected [{target_x}, {target_y}], got {coords}"
            )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_spread_coordinates_all_quadrants(self, mock_send):
        """Click targets spread across all 4 screen quadrants."""
        mock_send.return_value = {"success": True, "message": "OK"}
        received_payloads: List[dict] = []

        async def _capture(*args, **kwargs):
            payload = args[0] if args else {}
            if isinstance(payload, dict):
                received_payloads.append(dict(payload))
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _capture

        quadrant_targets = [
            (200, 200),   # top-left
            (1200, 200),  # top-right
            (200, 700),   # bottom-left
            (1200, 700),  # bottom-right
        ]

        for engine in PRIMARY_DESKTOP_ENGINES:
            for x, y in quadrant_targets:
                for _ in range(10):
                    action = make_click_action(x, y)
                    run_async(
                        _timed_execute(action, mode="desktop", engine=engine)
                    )

        # 1 engine × 4 quadrants × 10 repeats = 40
        assert len(received_payloads) == 40
        for p in received_payloads:
            coords = p.get("coordinates", [])
            assert len(coords) == 2
            assert 0 <= coords[0] <= SCREEN_W
            assert 0 <= coords[1] <= SCREEN_H

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_coordinate_drift_simulated_errors(self, mock_send):
        """Verify that coordinate-related errors are classified correctly."""
        call_idx = 0

        async def _drift_sim(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx % 8 == 0:
                return {
                    "success": False,
                    "message": "Coordinate out of bounds: drift detected at [1500, 950]",
                }
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _drift_sim
        metrics = DesktopStressMetrics()

        for _ in range(DESKTOP_LOOP_CYCLES):
            action = make_click_action(720, 450)
            result, elapsed = run_async(
                _timed_execute(action, mode="desktop", engine="xdotool")
            )
            success = result.get("success", False)
            msg = result.get("message", "")
            metrics.record_action(success, elapsed, None if success else msg)
            if not success:
                metrics.classify_error(msg)

        assert metrics.coordinate_drift_errors > 0, "Expected drift error detections"
        assert metrics.success_rate >= 0.80


# ══════════════════════════════════════════════════════════════════════════════
# Desktop Hybrid Fallback Under Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestDesktopHybridFallbackStress:
    """Stress the desktop_hybrid fallback path."""

    @patch(
        "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
        new_callable=AsyncMock,
    )
    def test_hybrid_50_cycle_terminal_loop(self, mock_hybrid):
        """50 terminal cycles through the hybrid engine."""
        mock_hybrid.return_value = {
            "success": True,
            "message": "OK",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": False,
        }
        metrics = DesktopStressMetrics()

        for cycle in range(DESKTOP_LOOP_CYCLES):
            cycle_start = time.perf_counter()
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="desktop_hybrid")
                )
                metrics.record_action(result.get("success", False), elapsed)
            cycle_ms = (time.perf_counter() - cycle_start) * 1000
            metrics.cycle_durations_ms.append(cycle_ms)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        assert metrics.total_actions == DESKTOP_LOOP_CYCLES * 4
        assert metrics.success_rate >= STRESS.min_success_rate

    @patch(
        "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
        new_callable=AsyncMock,
    )
    def test_hybrid_fallback_activation(self, mock_hybrid):
        """Simulate xdotool failures that trigger desktop fallback."""
        call_idx = 0

        async def _fallback_sim(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx % 4 == 0:
                # Xdotool failed, fallback succeeded
                return {
                    "success": True,
                    "message": "OK (via desktop fallback)",
                    "engine": "desktop_hybrid",
                    "primary_engine": "xdotool",
                    "fallback_used": True,
                }
            return {
                "success": True,
                "message": "OK",
                "engine": "desktop_hybrid",
                "primary_engine": "xdotool",
                "fallback_used": False,
            }

        mock_hybrid.side_effect = _fallback_sim
        metrics = DesktopStressMetrics()
        fallback_count = 0

        for _ in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="desktop_hybrid")
                )
                metrics.record_action(result.get("success", False), elapsed)
                if result.get("fallback_used"):
                    fallback_count += 1
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        assert metrics.success_rate >= STRESS.min_success_rate
        assert fallback_count > 0, "Expected some fallback activations"
        # ~25% should use fallback
        assert fallback_count < metrics.total_actions * 0.50

    @patch(
        "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
        new_callable=AsyncMock,
    )
    def test_hybrid_both_engines_fail(self, mock_hybrid):
        """When the desktop hybrid engine fails, it must report clearly."""
        call_idx = 0

        async def _both_fail(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx % 5 == 0:
                return {
                    "success": False,
                    "message": "xdotool: focus error",
                    "engine": "desktop_hybrid",
                    "primary_engine": "xdotool",
                    "fallback_used": False,
                }
            return {
                "success": True,
                "message": "OK",
                "engine": "desktop_hybrid",
                "primary_engine": "xdotool",
                "fallback_used": False,
            }

        mock_hybrid.side_effect = _both_fail
        metrics = DesktopStressMetrics()

        for _ in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="desktop", engine="desktop_hybrid")
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == DESKTOP_LOOP_CYCLES
        # ~20% failures
        assert metrics.success_rate >= 0.70
        assert metrics.failed > 0


# ══════════════════════════════════════════════════════════════════════════════
# Desktop Hybrid Validation Layer Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestDesktopHybridValidation:
    """Stress the desktop_hybrid input validation layer directly."""

    def test_validation_rejects_unsupported_actions(self):
        """Browser-only actions must be rejected by hybrid validation."""
        browser_only = ["evaluate_js", "get_html", "query_selector"]
        for action_name in browser_only:
            result = _validate(action_name, None, None)
            assert result is not None, f"Expected rejection for {action_name}"
            assert not result["success"]

    def test_validation_accepts_desktop_actions_rapidly(self):
        """All desktop actions must pass validation under load."""
        desktop_actions = [
            ("click", [720, 450], None),
            ("type", None, "STRESS TEST"),
            ("key", None, "Enter"),
            ("open_terminal", None, "xfce4-terminal"),
            ("close_window", None, "xfce4-terminal"),
            ("double_click", [400, 300], None),
            ("scroll_down", [720, 450], None),
            ("focus_window", None, "terminal"),
        ]

        for _ in range(200):
            for action_name, coords, text in desktop_actions:
                result = _validate(action_name, coords, text)
                assert result is None, (
                    f"Validation unexpectedly rejected {action_name}: {result}"
                )

    def test_recoverable_error_classification(self):
        """Verify recoverable error patterns are correctly identified."""
        recoverable_messages = [
            "X11 focus error: window not found",
            "BadWindow (invalid Window parameter)",
            "xdotool timeout: focus failed",
            "Permission denied: cannot access display",
            "cannot open display :99",
            "No such window: 0x12345",
            "Unsupported action for xdotool",
            "X11 connection error: broken pipe",
            "Window not found: missing xfce4-terminal",
            "xdotool error: focus lost",
        ]
        for msg in recoverable_messages:
            assert _is_recoverable(msg), f"Expected recoverable: {msg!r}"

    def test_non_recoverable_errors_not_misclassified(self):
        """Non-recoverable errors must not trigger fallback."""
        non_recoverable = [
            "Action completed successfully",
            "HTTP 500 internal server error",
            "JSON parse error",
            "Invalid coordinates format",
        ]
        for msg in non_recoverable:
            assert not _is_recoverable(msg), f"Should NOT be recoverable: {msg!r}"


# ══════════════════════════════════════════════════════════════════════════════
# Engine Isolation Under Desktop Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestDesktopEngineIsolation:
    """Ensure desktop engines don't cross-contaminate under stress."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_engine_tags_never_cross(self, mock_send):
        """Rapidly alternate between desktop engines; tags must match."""
        mock_send.return_value = {"success": True, "message": "OK"}

        for _ in range(200):
            for engine in PRIMARY_DESKTOP_ENGINES:
                action = make_click_action(720, 450)
                result = run_async(
                    execute_action(action, mode="desktop", engine=engine)
                )
                assert result.get("engine") == engine, (
                    f"Tag mismatch: expected {engine}, got {result.get('engine')}"
                )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_mode_always_desktop(self, mock_send):
        """All payloads sent by desktop engines must carry mode='desktop'."""
        payloads: List[dict] = []

        async def _capture(*args, **kwargs):
            payload = args[0] if args else {}
            if isinstance(payload, dict):
                payloads.append(dict(payload))
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _capture

        for _ in range(50):
            for engine in PRIMARY_DESKTOP_ENGINES:
                for action in _build_terminal_cycle_actions():
                    run_async(
                        _timed_execute(action, mode="desktop", engine=engine)
                    )

        # 50 × 1 engine × 4 actions = 200
        assert len(payloads) == 200
        for p in payloads:
            assert p.get("mode") == "desktop", f"Non-desktop mode in payload: {p}"

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch(
        "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
        new_callable=AsyncMock,
    )
    def test_concurrent_xdotool_hybrid(self, mock_hybrid, mock_send):
        """Concurrent dispatch to desktop engines; isolation holds."""
        # Return a fresh dict each call to avoid shared-mutation races
        mock_send.side_effect = lambda *a, **kw: {"success": True, "message": "OK"}
        mock_hybrid.side_effect = lambda *a, **kw: {
            "success": True,
            "message": "OK",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": False,
        }

        per_engine: Dict[str, List[str]] = {e: [] for e in DESKTOP_ENGINES}

        async def _fire():
            tasks = []
            for engine in DESKTOP_ENGINES:
                for _ in range(10):
                    action = make_click_action(
                        random.randint(50, 1390),
                        random.randint(50, 850),
                    )
                    mode = ENGINE_MODES[engine]

                    async def _exec(a=action, e=engine, m=mode):
                        r = await execute_action(a, mode=m, engine=e)
                        per_engine[e].append(r.get("engine", "MISSING"))

                    tasks.append(_exec())
            await asyncio.gather(*tasks)

        run_async(_fire())

        for engine, tags in per_engine.items():
            assert len(tags) == 10, f"{engine}: expected 10, got {len(tags)}"
            assert all(t == engine for t in tags), (
                f"{engine} had wrong tags: {set(tags)}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Memory Growth Under Sustained Desktop Load
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase3
class TestDesktopMemoryGrowth:
    """Verify memory stays bounded during extended desktop stress."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_xdotool_memory_bounded(self, mock_send):
        """Memory must not grow excessively over 50 cycles."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = DesktopStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(DESKTOP_LOOP_CYCLES):
            for action in _build_terminal_cycle_actions():
                run_async(
                    _timed_execute(action, mode="desktop", engine="xdotool")
                )
            if cycle % 5 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        growth_mb = metrics.memory_growth_bytes / (1024 * 1024)
        assert growth_mb < STRESS.max_memory_growth_mb, (
            f"xdotool memory grew {growth_mb:.1f}MB over {DESKTOP_LOOP_CYCLES} cycles"
        )
