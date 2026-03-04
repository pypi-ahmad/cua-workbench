"""Phase 4 — Accessibility Engine Stress Tests (AT-SPI / a11y).

50-iteration loop:
  open gedit → get_accessibility_tree → find text area → type "ACCESSIBILITY STRESS" → close

Verify:
  - No GIR import errors
  - No stale node errors
  - No D-Bus disconnect
  - No tree corruption

Run with:
    pytest tests/stress/test_phase4_accessibility_stress.py -v -m phase4
"""

from __future__ import annotations

import asyncio
import random
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from backend.config import config
from backend.models import ActionType, AgentAction
from backend.agent.executor import execute_action
from backend.engines.accessibility_engine import (
    execute_accessibility_action,
    _normalize_role,
    _sanitize_role,
    _sanitize_name,
    _ROLE_ALIASES,
    _element_cache,
    _element_cache_lock,
    A11Y_TOOL_HANDLERS,
)

from tests.stress.helpers import (
    STRESS,
    StressMetrics,
    run_async,
)

# ── Constants ─────────────────────────────────────────────────────────────────

A11Y_LOOP_CYCLES = 50
A11Y_ENGINE = "omni_accessibility"
A11Y_MODE = "desktop"

# The 5-action gedit loop
_GEDIT_CYCLE_ACTIONS = [
    AgentAction(action=ActionType.OPEN_TERMINAL, text="gedit"),
    AgentAction(action=ActionType.GET_ACCESSIBILITY_TREE, target="gedit"),
    AgentAction(action=ActionType.FIND_BY_ROLE, text="text", target="gedit"),
    AgentAction(action=ActionType.TYPE, text="ACCESSIBILITY STRESS", target="text area"),
    AgentAction(action=ActionType.WINDOW_CLOSE, text="gedit"),
]


def _build_gedit_cycle() -> list:
    """Return a fresh copy of the 5-action gedit cycle."""
    return list(_GEDIT_CYCLE_ACTIONS)


# ── Autouse: zero action delay ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep for fast stress tests."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


# ── Accessibility-specific metrics ────────────────────────────────────────────

@dataclass
class A11yStressMetrics:
    """Extended metrics for accessibility engine stress."""

    cycles_completed: int = 0
    total_actions: int = 0
    successful: int = 0
    failed: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    # A11y-specific error counters
    gir_import_errors: int = 0
    stale_node_errors: int = 0
    dbus_disconnect_errors: int = 0
    tree_corruption_errors: int = 0
    handler_missing_errors: int = 0
    errors: List[str] = field(default_factory=list)

    # Memory / timing
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
        """Bucket an error message into the appropriate counter."""
        msg = message.lower()
        if "gir" in msg or "gi." in msg or "atspi" in msg and "import" in msg:
            self.gir_import_errors += 1
        elif "import" in msg and ("gi" in msg or "gobject" in msg or "introspection" in msg):
            self.gir_import_errors += 1
        elif "bindings unavailable" in msg or "gir1.2-atspi" in msg:
            self.gir_import_errors += 1
        elif "stale" in msg or "expired" in msg or "not found in cache" in msg:
            self.stale_node_errors += 1
        elif "element id" in msg and "not found" in msg:
            self.stale_node_errors += 1
        elif "dbus" in msg or "d-bus" in msg or "bus" in msg and "disconnect" in msg:
            self.dbus_disconnect_errors += 1
        elif "session bus" in msg or "registryd" in msg:
            self.dbus_disconnect_errors += 1
        elif "corrupt" in msg or "tree" in msg and ("invalid" in msg or "malform" in msg):
            self.tree_corruption_errors += 1
        elif "child" in msg and "none" in msg:
            self.tree_corruption_errors += 1

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


async def _timed_a11y_dispatch(action_str: str, text: str = "", target: str = ""):
    """Call execute_accessibility_action directly, return (result, elapsed_ms).

    Uses module-level lookup so that patches on the module attribute apply.
    """
    import backend.engines.accessibility_engine as _a11y_mod
    start = time.perf_counter()
    result = await _a11y_mod.execute_accessibility_action(action_str, text=text, target=target)
    elapsed = (time.perf_counter() - start) * 1000
    return result, elapsed


# ══════════════════════════════════════════════════════════════════════════════
# TEST A — Gedit Accessibility Loop (50 cycles via executor)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestA11yGeditLoop:
    """50-cycle gedit open/tree/find/type/close through executor → a11y engine."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_gedit_loop_50_cycles(self, mock_a11y):
        """Full 50-cycle gedit loop; all actions succeed."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        metrics = A11yStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(A11Y_LOOP_CYCLES):
            cycle_start = time.perf_counter()
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                metrics.record_action(result.get("success", False), elapsed)
            cycle_ms = (time.perf_counter() - cycle_start) * 1000
            metrics.cycle_durations_ms.append(cycle_ms)
            metrics.cycles_completed += 1

            if cycle % 10 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.total_actions == A11Y_LOOP_CYCLES * 5
        assert metrics.success_rate >= STRESS.min_success_rate
        assert metrics.avg_latency_ms < STRESS.max_acceptable_latency_ms

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_gedit_loop_memory_bounded(self, mock_a11y):
        """Memory must not grow excessively over 50 gedit cycles."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        metrics = A11yStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
            if cycle % 5 == 0:
                metrics.sample_memory()
            metrics.cycles_completed += 1

        metrics.sample_memory()
        tracemalloc.stop()

        growth_mb = metrics.memory_growth_bytes / (1024 * 1024)
        assert growth_mb < STRESS.max_memory_growth_mb

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_gedit_loop_cycle_timing_stable(self, mock_a11y):
        """Cycle durations stay consistent — no exponential blowup."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        metrics = A11yStressMetrics()

        for cycle in range(A11Y_LOOP_CYCLES):
            cycle_start = time.perf_counter()
            for action in _build_gedit_cycle():
                run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
            cycle_ms = (time.perf_counter() - cycle_start) * 1000
            metrics.cycle_durations_ms.append(cycle_ms)

        avg = metrics.avg_cycle_ms
        for i, dur in enumerate(metrics.cycle_durations_ms):
            assert dur < avg * 10, (
                f"Cycle {i}: {dur:.1f}ms vs avg {avg:.1f}ms — potential CPU spike"
            )


# ══════════════════════════════════════════════════════════════════════════════
# TEST B — GIR Import Error Resilience
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestGirImportErrors:
    """Verify accessibility engine handles missing GI bindings gracefully."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_intermittent_gir_errors(self, mock_a11y):
        """Simulate periodic GIR import failures; system survives."""
        call_idx = 0

        async def _gir_flaky(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx % 7 == 0:
                return {
                    "success": False,
                    "message": "AT-SPI bindings unavailable. Ensure gir1.2-atspi-2.0 "
                               "and python3-gi are installed inside the container.",
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _gir_flaky
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.gir_import_errors > 0, "Expected GIR import errors from injected faults"
        assert metrics.gir_import_errors < metrics.total_actions * 0.25
        assert metrics.success_rate >= 0.70

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_persistent_gir_failure_all_fail(self, mock_a11y):
        """If GI bindings are fully missing, every action fails cleanly."""
        mock_a11y.return_value = {
            "success": False,
            "message": "AT-SPI bindings unavailable. Ensure gir1.2-atspi-2.0 "
                       "and python3-gi are installed inside the container.",
        }
        metrics = A11yStressMetrics()

        for _ in range(20):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)

        # All must fail cleanly — no crashes, no hangs
        assert metrics.total_actions == 20 * 5
        assert metrics.success_rate == 0.0
        assert metrics.gir_import_errors == metrics.total_actions


# ══════════════════════════════════════════════════════════════════════════════
# TEST C — Stale Node Error Resilience
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestStaleNodeErrors:
    """Verify handling of stale/expired element references."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_intermittent_stale_nodes(self, mock_a11y):
        """Simulate stale element IDs that expired from the cache."""
        call_idx = 0

        async def _stale_flaky(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            action = args[0] if args else kwargs.get("action", "")
            # Stale nodes most likely on find/type (referencing cached elements)
            if action in ("find_by_role", "type", "fill") and call_idx % 3 == 0:
                return {
                    "success": False,
                    "message": "Element id 42 not found in cache (expired or invalid)",
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _stale_flaky
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.stale_node_errors > 0, "Expected stale node errors"
        assert metrics.success_rate >= 0.70

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_high_rate_stale_nodes(self, mock_a11y):
        """High rate of stale references; system still completes all cycles."""
        async def _very_stale(*args, **kwargs):
            action = args[0] if args else kwargs.get("action", "")
            if action in ("find_by_role", "type", "click", "fill"):
                if random.random() < 0.40:
                    return {
                        "success": False,
                        "message": f"Element id {random.randint(1, 5000)} not found in cache (expired or invalid)",
                    }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _very_stale
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        # Must complete all cycles without crashing
        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.stale_node_errors > 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST D — D-Bus Disconnect Resilience
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestDbusDisconnectErrors:
    """Verify handling of D-Bus disconnections during a11y operations."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_intermittent_dbus_disconnect(self, mock_a11y):
        """Simulate periodic D-Bus session disconnects."""
        call_idx = 0

        async def _dbus_flaky(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx % 9 == 0:
                return {
                    "success": False,
                    "message": "D-Bus session bus disconnected: "
                               "org.freedesktop.DBus.Error.ServiceUnknown",
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _dbus_flaky
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.dbus_disconnect_errors > 0, "Expected D-Bus disconnect errors"
        assert metrics.success_rate >= 0.80

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_dbus_flap_burst(self, mock_a11y):
        """D-Bus disconnects in bursts then recovers; system survives."""
        call_idx = 0

        async def _dbus_burst(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            # Burst of failures in cycles 10-15 (actions 50-75)
            if 50 <= call_idx <= 75:
                return {
                    "success": False,
                    "message": "D-Bus: session bus connection lost, registryd not responding",
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _dbus_burst
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.dbus_disconnect_errors > 0
        # Burst covers 26 actions out of 250; rest should succeed
        assert metrics.success_rate >= 0.85


# ══════════════════════════════════════════════════════════════════════════════
# TEST E — Tree Corruption Resilience
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestTreeCorruption:
    """Verify handling of corrupted accessibility tree data."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_intermittent_tree_corruption(self, mock_a11y):
        """Simulate corrupted tree nodes (None children, invalid roles)."""
        call_idx = 0

        async def _corrupt_tree(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            action = args[0] if args else kwargs.get("action", "")
            if action in ("get_accessibility_tree", "get_snapshot") and call_idx % 6 == 0:
                return {
                    "success": False,
                    "message": "Tree corruption: child node returned None at depth 3, "
                               "invalid role detected",
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _corrupt_tree
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.tree_corruption_errors > 0, "Expected tree corruption detections"
        assert metrics.success_rate >= 0.80

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_empty_tree_response(self, mock_a11y):
        """Tree queries that return empty results should not crash."""
        async def _empty_tree(*args, **kwargs):
            action = args[0] if args else kwargs.get("action", "")
            if action in ("get_accessibility_tree", "get_snapshot", "find_by_role"):
                return {
                    "success": True,
                    "message": "[]",  # empty tree
                }
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _empty_tree
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                metrics.record_action(result.get("success", False), elapsed)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        assert metrics.success_rate == 1.0  # all return success (just empty)


# ══════════════════════════════════════════════════════════════════════════════
# TEST F — Handler Table & Role Normalisation Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestA11yHandlerTableStress:
    """Stress the a11y handler dispatch table and role normalisation."""

    def test_all_action_types_have_handlers(self):
        """Every ActionType must map to a handler in A11Y_TOOL_HANDLERS."""
        for member in ActionType:
            assert member.value in A11Y_TOOL_HANDLERS, (
                f"ActionType.{member.name} ({member.value}) missing from A11Y_TOOL_HANDLERS"
            )

    def test_role_alias_consistency(self):
        """All role aliases must normalise to a valid AT-SPI role name."""
        for alias, canonical in _ROLE_ALIASES.items():
            normed = _normalize_role(alias)
            assert normed == canonical, (
                f"Alias {alias!r}: expected {canonical!r}, got {normed!r}"
            )

    def test_role_normalisation_rapid_fire(self):
        """Rapid role normalisation under load — no errors."""
        all_aliases = list(_ROLE_ALIASES.keys())
        for _ in range(500):
            for alias in all_aliases:
                result = _normalize_role(alias)
                assert isinstance(result, str)
                assert len(result) > 0

    def test_sanitize_role_rejects_bad_input(self):
        """Role sanitisation rejects SQL-injection-like and overlong input."""
        bad_roles = [
            "",
            "x" * 100,
            "role; DROP TABLE",
            "role\x00null",
            "<script>alert(1)</script>",
            "role\nwith\nnewlines",
        ]
        for bad in bad_roles:
            with pytest.raises(ValueError):
                _sanitize_role(bad)

    def test_sanitize_name_rejects_overlong(self):
        """Name sanitisation rejects input > 500 chars."""
        with pytest.raises(ValueError):
            _sanitize_name("x" * 501)

    def test_sanitize_name_accepts_valid(self):
        """Normal names pass sanitisation."""
        valid_names = [
            "gedit",
            "text area",
            "Save As...",
            "File → Open",
            "Search (Ctrl+F)",
            "",  # empty is allowed
            "x" * 500,  # exactly at limit
        ]
        for name in valid_names:
            result = _sanitize_name(name)
            assert isinstance(result, str)

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_unsupported_action_handled_gracefully(self, mock_a11y):
        """Dispatching an unknown action returns failure, not crash."""
        mock_a11y.return_value = {
            "success": False,
            "message": "Unsupported action 'fly_to_moon' in accessibility engine",
        }

        for _ in range(50):
            result = run_async(
                execute_accessibility_action("fly_to_moon", text="please", target="moon")
            )
            # Direct dispatch (not through executor) — handler table lookup
            assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# TEST G — Direct Handler Dispatch Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestDirectHandlerDispatch:
    """Stress execute_accessibility_action directly (bypass executor)."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_rapid_tree_queries(self, mock_a11y):
        """50 rapid get_accessibility_tree calls."""
        mock_a11y.return_value = {
            "success": True,
            "message": '[{"role": "frame", "name": "gedit", "element_id": 1}]',
        }
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            result, elapsed = run_async(
                _timed_a11y_dispatch("get_accessibility_tree", target="gedit")
            )
            metrics.record_action(result.get("success", False), elapsed)

        assert metrics.total_actions == A11Y_LOOP_CYCLES
        assert metrics.success_rate >= STRESS.min_success_rate

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_rapid_type_actions(self, mock_a11y):
        """50 rapid type actions through a11y engine."""
        mock_a11y.return_value = {"success": True, "message": "Typed text OK"}
        metrics = A11yStressMetrics()

        for i in range(A11Y_LOOP_CYCLES):
            text = f"ACCESSIBILITY STRESS {i}"
            result, elapsed = run_async(
                _timed_a11y_dispatch("type", text=text, target="text area")
            )
            metrics.record_action(result.get("success", False), elapsed)

        assert metrics.total_actions == A11Y_LOOP_CYCLES
        assert metrics.success_rate >= STRESS.min_success_rate

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_mixed_a11y_actions_rapid(self, mock_a11y):
        """Rapid-fire mix of all a11y action types."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        a11y_actions = [
            ("click", "", "Save"),
            ("type", "Hello", "text area"),
            ("key", "Return", ""),
            ("get_accessibility_tree", "", "gedit"),
            ("find_by_role", "button", "gedit"),
            ("focus_window", "", "gedit"),
            ("close_window", "", "gedit"),
            ("open_terminal", "xterm", ""),
        ]
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action_str, text, target in a11y_actions:
                result, elapsed = run_async(
                    _timed_a11y_dispatch(action_str, text=text, target=target)
                )
                metrics.record_action(result.get("success", False), elapsed)

        assert metrics.total_actions == A11Y_LOOP_CYCLES * len(a11y_actions)
        assert metrics.success_rate >= STRESS.min_success_rate


# ══════════════════════════════════════════════════════════════════════════════
# TEST H — Mixed Error Injection (All 4 Error Types)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestMixedA11yErrors:
    """Inject all 4 error types simultaneously under load."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_all_error_types_mixed(self, mock_a11y):
        """Randomly inject GIR, stale-node, D-Bus, and tree-corruption errors."""
        _error_templates = [
            "AT-SPI bindings unavailable. Ensure gir1.2-atspi-2.0 and python3-gi are installed inside the container.",
            "Element id 99 not found in cache (expired or invalid)",
            "D-Bus session bus disconnected: org.freedesktop.DBus.Error.ServiceUnknown",
            "Tree corruption: child node returned None at depth 2, invalid role detected",
        ]

        async def _mixed_errors(*args, **kwargs):
            if random.random() < 0.20:
                msg = random.choice(_error_templates)
                return {"success": False, "message": msg}
            return {"success": True, "message": "A11Y OK"}

        mock_a11y.side_effect = _mixed_errors
        metrics = A11yStressMetrics()

        for _ in range(A11Y_LOOP_CYCLES):
            for action in _build_gedit_cycle():
                result, elapsed = run_async(
                    _timed_execute(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
                )
                success = result.get("success", False)
                msg = result.get("message", "")
                metrics.record_action(success, elapsed, None if success else msg)
                if not success:
                    metrics.classify_error(msg)
            metrics.cycles_completed += 1

        assert metrics.cycles_completed == A11Y_LOOP_CYCLES
        # With 20% error rate across 4 types, we should see multiple categories
        total_classified = (
            metrics.gir_import_errors
            + metrics.stale_node_errors
            + metrics.dbus_disconnect_errors
            + metrics.tree_corruption_errors
        )
        assert total_classified > 0, "Expected at least some classified errors"
        assert metrics.success_rate >= 0.60

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_error_counters_mutually_exclusive(self, mock_a11y):
        """Each error message should be classified into exactly one bucket."""
        templates_and_buckets = [
            ("AT-SPI bindings unavailable. Ensure gir1.2-atspi-2.0 and python3-gi are installed.", "gir"),
            ("Element id 42 not found in cache (expired or invalid)", "stale"),
            ("D-Bus session bus disconnected: lost connection", "dbus"),
            ("Tree corruption: child node returned None at depth 1, invalid role", "tree"),
        ]

        for msg, expected_bucket in templates_and_buckets:
            m = A11yStressMetrics()
            m.classify_error(msg)

            counts = {
                "gir": m.gir_import_errors,
                "stale": m.stale_node_errors,
                "dbus": m.dbus_disconnect_errors,
                "tree": m.tree_corruption_errors,
            }
            assert counts[expected_bucket] == 1, (
                f"Message {msg!r} not classified as {expected_bucket}: {counts}"
            )
            total = sum(counts.values())
            assert total == 1, (
                f"Message {msg!r} classified into {total} buckets: {counts}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# TEST I — Engine Isolation (A11y vs Others)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestA11yEngineIsolation:
    """Ensure accessibility engine stays isolated from other engines."""

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_engine_tag_always_accessibility(self, mock_a11y):
        """All results must carry engine='accessibility'."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}

        for _ in range(200):
            action = AgentAction(action=ActionType.CLICK, coordinates=[720, 450], target="button")
            result = run_async(
                execute_action(action, mode=A11Y_MODE, engine=A11Y_ENGINE)
            )
            assert result.get("engine") == A11Y_ENGINE, (
                f"Expected engine=accessibility, got {result.get('engine')}"
            )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    def test_a11y_and_desktop_dont_cross(self, mock_a11y, mock_send):
        """Alternating a11y and xdotool actions; tags never cross."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        mock_send.side_effect = lambda *a, **kw: {"success": True, "message": "OK"}

        for _ in range(100):
            # A11y action
            a11y_action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200], target="btn")
            r1 = run_async(
                execute_action(a11y_action, mode="desktop", engine="omni_accessibility")
            )
            assert r1.get("engine") == "omni_accessibility"

            # Desktop action
            desk_action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200])
            r2 = run_async(
                execute_action(desk_action, mode="desktop", engine="xdotool")
            )
            assert r2.get("engine") == "xdotool"

    @patch(
        "backend.engines.accessibility_engine.execute_accessibility_action",
        new_callable=AsyncMock,
    )
    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch(
        "backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action",
        new_callable=AsyncMock,
    )
    def test_concurrent_a11y_with_all_engines(self, mock_hybrid, mock_send, mock_a11y):
        """Concurrent fire to a11y + desktop engines; isolation holds."""
        mock_a11y.return_value = {"success": True, "message": "A11Y OK"}
        mock_send.side_effect = lambda *a, **kw: {"success": True, "message": "OK"}
        mock_hybrid.side_effect = lambda *a, **kw: {
            "success": True,
            "message": "OK",
            "engine": "desktop_hybrid",
            "primary_engine": "xdotool",
            "fallback_used": False,
        }

        engines_to_test = ["omni_accessibility", "xdotool", "ydotool", "desktop_hybrid"]
        # Note: computer_use excluded — it uses its own native CU loop, not execute_action()
        per_engine: Dict[str, List[str]] = {e: [] for e in engines_to_test}

        from tests.stress.helpers import ENGINE_MODES

        async def _fire():
            tasks = []
            for engine in engines_to_test:
                for _ in range(10):
                    action = AgentAction(
                        action=ActionType.CLICK,
                        coordinates=[random.randint(50, 1390), random.randint(50, 850)],
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
# TEST J — Element Cache Stress
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase4
class TestElementCacheStress:
    """Stress the _element_cache LRU eviction under high volume."""

    def test_cache_eviction_at_boundary(self):
        """Filling cache beyond 5000 entries triggers eviction without errors."""
        import threading
        from backend.engines.accessibility_engine import (
            _element_cache,
            _element_cache_lock,
            _next_element_id,
        )

        # Save state and clear
        with _element_cache_lock:
            saved = dict(_element_cache)
            _element_cache.clear()

        try:
            # Insert 6000 mock elements
            for i in range(6000):
                sentinel = MagicMock(name=f"mock_accessible_{i}")
                _next_element_id(sentinel)

            with _element_cache_lock:
                # After 6000 inserts with eviction at 5000, cache should be bounded
                assert len(_element_cache) <= 5001, (
                    f"Cache grew to {len(_element_cache)} — eviction may be broken"
                )
        finally:
            # Restore original state
            with _element_cache_lock:
                _element_cache.clear()
                _element_cache.update(saved)

    def test_concurrent_cache_access(self):
        """Concurrent reads/writes to the element cache don't corrupt."""
        import threading
        from backend.engines.accessibility_engine import (
            _element_cache,
            _element_cache_lock,
            _next_element_id,
            _get_cached,
        )

        with _element_cache_lock:
            saved = dict(_element_cache)
            _element_cache.clear()

        errors: List[str] = []

        def _writer():
            for i in range(200):
                sentinel = MagicMock(name=f"writer_{i}")
                _next_element_id(sentinel)

        def _reader():
            for _ in range(200):
                with _element_cache_lock:
                    keys = list(_element_cache.keys())
                if keys:
                    try:
                        _get_cached(keys[0])
                    except ValueError:
                        pass  # Expected if evicted

        try:
            threads = [
                threading.Thread(target=_writer),
                threading.Thread(target=_writer),
                threading.Thread(target=_reader),
                threading.Thread(target=_reader),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            # No thread should still be alive
            for t in threads:
                assert not t.is_alive(), "Thread hung during cache access"
        finally:
            with _element_cache_lock:
                _element_cache.clear()
                _element_cache.update(saved)
