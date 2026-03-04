"""Phase 1 — Backend engine-level stress tests (pytest integration).

Tests the executor dispatch, capability validation, and engine routing
under heavy load across all 6 engines. Uses mocks for hermetic unit-level
stress (no live container needed).

Run with:
    pytest tests/stress/test_phase1_engine_stress.py -v -m phase1
    pytest tests/stress/test_phase1_engine_stress.py -v -k "rapid_fire"
"""

from __future__ import annotations

import asyncio
import random
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engine_capabilities import EngineCapabilities
from backend.models import ActionType, AgentAction, AutomationEngine
from backend.tools.router import SUPPORTED_ENGINES, InvalidEngineError, validate_engine
from backend.agent.executor import execute_action

from tests.stress.helpers import (
    ALL_ENGINES,
    BROWSER_ENGINES,
    DESKTOP_ENGINES,
    ENGINE_MODES,
    SITES,
    STRESS,
    StressMetrics,
    generate_rapid_click_sequence,
    generate_navigation_sequence,
    generate_typing_barrage,
    make_click_action,
    make_done_action,
    make_key_action,
    make_open_url_action,
    make_scroll_action,
    make_type_action,
    mock_agent_service_success,
    mock_agent_service_intermittent,
    mock_screenshot_b64,
    run_async,
)


# ── Autouse fixture: zero out action_delay_ms so tests don't sleep 500ms/action

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep in the executor for fast stress tests."""
    from backend.config import config
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mode_for(engine: str) -> str:
    return ENGINE_MODES.get(engine, "browser")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Engine Dispatch Stress Tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase1
class TestEngineValidationStress:
    """Stress test engine validation under rapid-fire invalid/valid inputs."""

    def test_rapid_valid_engine_validation(self):
        """Validate all 6 engines 1000 times in tight loop."""
        for _ in range(1000):
            for engine in ALL_ENGINES:
                assert validate_engine(engine) == engine

    def test_rapid_invalid_engine_rejection(self):
        """Reject 1000 invalid engines without leaking state."""
        invalids = [
            "", "PLAYWRIGHT", "magic", "auto", "gpt4", "browser",
            "xdotool ", " playwright", "playwright_mcp_v2", "hybrid",
            "Desktop_Hybrid", "OMNI_ACCESSIBILITY", "ydotool", "ydotool2", "none",
            "null", "undefined", "NaN", "true", "false",
        ]
        for _ in range(100):
            for bad in invalids:
                with pytest.raises(InvalidEngineError):
                    validate_engine(bad)

    def test_concurrent_engine_validation(self):
        """Validate engines concurrently across multiple async tasks."""

        async def _validate_many():
            tasks = []
            for _ in range(200):
                engine = random.choice(ALL_ENGINES)
                tasks.append(asyncio.to_thread(validate_engine, engine))
            results = await asyncio.gather(*tasks)
            assert all(r in ALL_ENGINES for r in results)

        run_async(_validate_many())


@pytest.mark.stress
@pytest.mark.phase1
class TestCapabilityGateStress:
    """Stress test the engine capability registry under high throughput."""

    def test_bulk_capability_lookups(self):
        """Query every engine × every action 100 times."""
        registry = EngineCapabilities()
        actions_to_check = [
            "click", "type", "open_url", "scroll_down", "key",
            "screenshot", "wait", "done", "evaluate_js",
        ]
        for _ in range(100):
            for engine in ALL_ENGINES:
                for action in actions_to_check:
                    # Just checking it doesn't crash or leak
                    registry.validate_action(engine, action)

    def test_capability_consistency_under_load(self):
        """Engine capabilities must return identical results across 500 lookups."""
        registry = EngineCapabilities()
        baseline = {}
        for engine in ALL_ENGINES:
            baseline[engine] = registry.get_engine_actions(engine)

        for _ in range(500):
            engine = random.choice(ALL_ENGINES)
            actions = registry.get_engine_actions(engine)
            assert actions == baseline[engine], f"{engine} capabilities changed under load"

    def test_cross_engine_queries_stress(self):
        """engines_supporting() must be consistent under repeated calls."""
        registry = EngineCapabilities()
        test_actions = ["click", "type", "open_url", "scroll_down", "screenshot"]
        baseline = {a: registry.engines_supporting(a) for a in test_actions}

        for _ in range(300):
            action = random.choice(test_actions)
            result = registry.engines_supporting(action)
            assert result == baseline[action]


@pytest.mark.stress
@pytest.mark.phase1
class TestExecutorRapidFireStress:
    """Rapid-fire action execution through the executor with mocked backends."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_rapid_clicks_playwright(self, mock_send):
        """Fire 50 click actions through Playwright executor in rapid succession."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = StressMetrics()
        actions = generate_rapid_click_sequence(STRESS.rapid_fire_actions)

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="browser", engine="playwright"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate
        assert mock_send.call_count == STRESS.rapid_fire_actions

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_rapid_clicks_mcp(self, mock_mcp):
        """Fire 50 click actions through MCP executor."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        metrics = StressMetrics()
        # MCP needs target/selector for clicks
        actions = [
            AgentAction(action=ActionType.CLICK, target=f"#btn-{i}", coordinates=[100, 200])
            for i in range(STRESS.rapid_fire_actions)
        ]

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="browser", engine="playwright_mcp"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_rapid_clicks_xdotool(self, mock_send):
        """Fire 50 click actions through xdotool executor."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = StressMetrics()
        actions = generate_rapid_click_sequence(STRESS.rapid_fire_actions)

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="desktop", engine="xdotool"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate

    @patch("backend.engines.accessibility_engine.execute_accessibility_action", new_callable=AsyncMock)
    def test_rapid_clicks_accessibility(self, mock_a11y):
        """Fire 50 click actions through accessibility executor."""
        mock_a11y.return_value = {"success": True, "message": "OK", "engine": "omni_accessibility"}
        metrics = StressMetrics()
        actions = generate_rapid_click_sequence(STRESS.rapid_fire_actions)

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="desktop", engine="omni_accessibility"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate

    @patch("backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action", new_callable=AsyncMock)
    def test_rapid_clicks_desktop_hybrid(self, mock_hybrid):
        """Fire 50 click actions through desktop_hybrid executor."""
        mock_hybrid.return_value = {
            "success": True, "message": "OK", "engine": "desktop_hybrid",
            "primary_engine": "xdotool", "fallback_used": False,
        }
        metrics = StressMetrics()
        actions = generate_rapid_click_sequence(STRESS.rapid_fire_actions)

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="desktop", engine="desktop_hybrid"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate


@pytest.mark.stress
@pytest.mark.phase1
class TestExecutorMixedActionStress:
    """Stress the executor with diverse action types through each engine."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_mixed_actions_playwright(self, mock_send):
        """Execute a mixed sequence of navigations, clicks, types, scrolls, keys."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = StressMetrics()

        actions = (
            generate_navigation_sequence()
            + generate_rapid_click_sequence(10)
            + generate_typing_barrage(10)
            + [make_scroll_action("down") for _ in range(5)]
            + [make_key_action("Enter") for _ in range(5)]
            + [make_done_action()]
        )

        for action in actions:
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="browser", engine="playwright"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        assert metrics.success_rate >= STRESS.min_success_rate
        assert metrics.total_actions >= 30

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_mixed_actions_desktop_engines(self, mock_send):
        """Fire mixed actions through xdotool sequentially."""
        mock_send.return_value = {"success": True, "message": "OK"}

        for engine in ("xdotool",):
            metrics = StressMetrics()
            actions = (
                generate_rapid_click_sequence(15)
                + generate_typing_barrage(10)
                + [make_key_action("Return") for _ in range(5)]
                + [make_done_action()]
            )

            for action in actions:
                start = time.perf_counter()
                result = run_async(execute_action(action, mode="desktop", engine=engine))
                elapsed = (time.perf_counter() - start) * 1000
                metrics.record(result.get("success", False), elapsed)

            assert metrics.success_rate >= STRESS.min_success_rate, (
                f"{engine} mixed-action success rate {metrics.success_rate:.1%} "
                f"below threshold {STRESS.min_success_rate:.1%}"
            )


@pytest.mark.stress
@pytest.mark.phase1
class TestExecutorIntermittentFailureStress:
    """Test engine resilience when the agent service has intermittent failures."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_intermittent_failures_playwright(self, mock_send):
        """Playwright executor handles random 15% failures gracefully."""
        call_count = [0]

        async def _intermittent(*args, **kwargs):
            call_count[0] += 1
            if random.random() < STRESS.error_injection_rate:
                return {"success": False, "message": "Intermittent service error"}
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _intermittent
        metrics = StressMetrics()

        for _ in range(STRESS.rapid_fire_actions):
            action = make_click_action(random.randint(50, 1390), random.randint(50, 850))
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="browser", engine="playwright"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)

        # Even with 15% failures, executor must not crash/hang
        assert metrics.total_actions == STRESS.rapid_fire_actions
        # Success rate should roughly match 1 - error_injection_rate
        assert metrics.success_rate >= (1 - STRESS.error_injection_rate - 0.10)

    @patch("backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action", new_callable=AsyncMock)
    def test_intermittent_failures_hybrid(self, mock_hybrid):
        """Desktop hybrid handles intermittent failures with fallback metadata."""
        async def _intermittent_hybrid(*args, **kwargs):
            if random.random() < STRESS.error_injection_rate:
                return {
                    "success": False, "message": "xdotool timeout",
                    "engine": "desktop_hybrid", "primary_engine": "xdotool",
                    "fallback_used": True, "fallback_engine": "xdotool",
                }
            return {
                "success": True, "message": "OK",
                "engine": "desktop_hybrid", "primary_engine": "xdotool",
                "fallback_used": False,
            }

        mock_hybrid.side_effect = _intermittent_hybrid
        metrics = StressMetrics()

        for _ in range(STRESS.rapid_fire_actions):
            action = make_click_action(random.randint(50, 1390), random.randint(50, 850))
            start = time.perf_counter()
            result = run_async(execute_action(action, mode="desktop", engine="desktop_hybrid"))
            elapsed = (time.perf_counter() - start) * 1000
            metrics.record(result.get("success", False), elapsed)
            # Verify engine tag is always correct
            assert result.get("engine") == "desktop_hybrid"

        assert metrics.total_actions == STRESS.rapid_fire_actions


@pytest.mark.stress
@pytest.mark.phase1
class TestExecutorBoundaryStress:
    """Stress test edge cases and boundary conditions under load."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_out_of_bounds_coordinates_stress(self, mock_send):
        """Reject out-of-bounds coordinates consistently under rapid fire."""
        mock_send.return_value = {"success": True, "message": "OK"}
        oob_coords = [
            [1441, 500], [500, 901], [1500, 1000], [-1, 200], [200, -1],
            [99999, 99999],
        ]
        for _ in range(50):
            for coords in oob_coords:
                action = AgentAction(action=ActionType.CLICK, coordinates=coords)
                result = run_async(execute_action(action, mode="browser", engine="playwright"))
                assert not result["success"], f"OOB coords {coords} should have been rejected"

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    @patch("backend.engines.accessibility_engine.execute_accessibility_action", new_callable=AsyncMock)
    @patch("backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action", new_callable=AsyncMock)
    def test_oversized_text_handled_safely(self, mock_hybrid, mock_a11y, mock_mcp, mock_send):
        """Oversized text payloads are truncated (not crash) across all engines.

        The unified_schema truncates text >5000 chars to 5000. Verify the
        system handles this gracefully without crashing or hanging.
        """
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        mock_a11y.return_value = {"success": True, "message": "OK", "engine": "omni_accessibility"}
        mock_hybrid.return_value = {"success": True, "message": "OK", "engine": "desktop_hybrid",
                                    "primary_engine": "xdotool", "fallback_used": False}
        long_text = "A" * 10_000  # exceeds 5000 char limit — will be truncated
        action = AgentAction(action=ActionType.TYPE, text=long_text, coordinates=[100, 200])

        for engine in ALL_ENGINES:
            # Must not crash, hang, or raise — may succeed (truncated) or fail (validation)
            result = run_async(execute_action(action, mode=_mode_for(engine), engine=engine))
            assert isinstance(result, dict), f"Engine {engine} returned non-dict"
            assert "success" in result, f"Engine {engine} missing 'success' key"

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_done_action_always_succeeds(self, mock_send):
        """'done' action is terminal and bypasses the agent service."""
        # mock_send should NOT be called for done
        action = make_done_action("Stress test complete")
        for engine in ALL_ENGINES:
            result = run_async(execute_action(action, mode=_mode_for(engine), engine=engine))
            assert result["success"]
        assert mock_send.call_count == 0

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_high_throughput_navigation_all_sites(self, mock_mcp, mock_send):
        """Navigate to all 5 sites × 10 cycles across browser engines."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        metrics = StressMetrics()

        for cycle in range(STRESS.bulk_navigation_cycles):
            for site in SITES.values():
                action = make_open_url_action(site.url)
                for engine in BROWSER_ENGINES:
                    start = time.perf_counter()
                    result = run_async(
                        execute_action(action, mode="browser", engine=engine)
                    )
                    elapsed = (time.perf_counter() - start) * 1000
                    metrics.record(result.get("success", False), elapsed)

        expected = STRESS.bulk_navigation_cycles * len(SITES) * len(BROWSER_ENGINES)
        assert metrics.total_actions == expected
        assert metrics.success_rate >= STRESS.min_success_rate


@pytest.mark.stress
@pytest.mark.phase1
class TestExecutorConcurrencyStress:
    """Stress test executor with concurrent async action dispatch."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_concurrent_actions_single_engine(self, mock_send):
        """Fire 20 concurrent actions to a single engine."""
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = StressMetrics()

        async def _fire_concurrent():
            tasks = []
            for i in range(STRESS.max_concurrent_actions):
                action = make_click_action(
                    random.randint(50, 1390),
                    random.randint(50, 850),
                )
                tasks.append(execute_action(action, mode="browser", engine="playwright"))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    metrics.record(False, 0, str(r))
                else:
                    metrics.record(r.get("success", False), 0)

        run_async(_fire_concurrent())
        assert metrics.total_actions == STRESS.max_concurrent_actions
        assert metrics.success_rate >= STRESS.min_success_rate

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    @patch("backend.engines.accessibility_engine.execute_accessibility_action", new_callable=AsyncMock)
    @patch("backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action", new_callable=AsyncMock)
    def test_concurrent_actions_all_engines(
        self, mock_hybrid, mock_a11y, mock_mcp, mock_send
    ):
        """Fire concurrent actions across ALL 6 engines simultaneously."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        mock_a11y.return_value = {"success": True, "message": "OK", "engine": "omni_accessibility"}
        mock_hybrid.return_value = {
            "success": True, "message": "OK", "engine": "desktop_hybrid",
            "primary_engine": "xdotool", "fallback_used": False,
        }

        per_engine_metrics = {e: StressMetrics() for e in ALL_ENGINES}

        async def _fire_all():
            tasks = []
            for engine in ALL_ENGINES:
                mode = _mode_for(engine)
                for _ in range(5):
                    action = make_click_action(
                        random.randint(50, 1390),
                        random.randint(50, 850),
                    )
                    # MCP needs target
                    if engine == "playwright_mcp":
                        action = AgentAction(
                            action=ActionType.CLICK,
                            target="#btn",
                            coordinates=[100, 200],
                        )

                    async def _exec(a=action, e=engine, m=mode):
                        r = await execute_action(a, mode=m, engine=e)
                        per_engine_metrics[e].record(r.get("success", False), 0)
                        return r

                    tasks.append(_exec())

            await asyncio.gather(*tasks, return_exceptions=True)

        run_async(_fire_all())

        for engine, m in per_engine_metrics.items():
            assert m.total_actions == 5, f"{engine}: expected 5 actions, got {m.total_actions}"
            assert m.success_rate >= STRESS.min_success_rate, (
                f"{engine} concurrent success rate {m.success_rate:.1%} below threshold"
            )


@pytest.mark.stress
@pytest.mark.phase1
class TestEngineIsolationUnderStress:
    """Ensure engine isolation holds even under high-volume concurrent load."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_engine_tag_never_mutates_under_load(self, mock_send):
        """Every result must carry the exact engine tag it was dispatched to."""
        mock_send.return_value = {"success": True, "message": "OK"}

        for _ in range(100):
            for engine in ("playwright", "xdotool"):
                action = make_click_action(100, 200)
                result = run_async(execute_action(action, mode=_mode_for(engine), engine=engine))
                assert result.get("engine") == engine, (
                    f"Engine tag mutated: expected {engine}, got {result.get('engine')}"
                )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_no_cross_engine_leakage(self, mock_mcp, mock_send):
        """Playwright calls must not leak to MCP and vice versa."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        for _ in range(50):
            # Playwright action
            action_pw = make_click_action(200, 300)
            run_async(execute_action(action_pw, mode="browser", engine="playwright"))

            # MCP action
            action_mcp = AgentAction(
                action=ActionType.CLICK, target="#test", coordinates=[200, 300]
            )
            run_async(execute_action(action_mcp, mode="browser", engine="playwright_mcp"))

        # Playwright went through _send_with_retry, MCP went through execute_mcp_action
        assert mock_send.call_count == 50
        assert mock_mcp.call_count == 50


@pytest.mark.stress
@pytest.mark.phase1
class TestSiteNavigationMatrix:
    """Stress test navigation to all 5 target sites across all browser engines."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_all_sites_all_browser_engines(self, mock_mcp, mock_send):
        """Navigate to each site with each browser engine × 5 cycles."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        metrics = StressMetrics()

        for _ in range(5):
            for site in SITES.values():
                for engine in BROWSER_ENGINES:
                    action = make_open_url_action(site.url)
                    start = time.perf_counter()
                    result = run_async(
                        execute_action(action, mode="browser", engine=engine)
                    )
                    elapsed = (time.perf_counter() - start) * 1000
                    metrics.record(result.get("success", False), elapsed)

        expected_total = 5 * len(SITES) * len(BROWSER_ENGINES)
        assert metrics.total_actions == expected_total
        assert metrics.success_rate >= STRESS.min_success_rate
        summary = metrics.summary()
        assert float(summary["avg_latency_ms"]) < STRESS.max_acceptable_latency_ms
