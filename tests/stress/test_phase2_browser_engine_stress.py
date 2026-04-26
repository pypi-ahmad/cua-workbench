"""Phase 2 — Browser Engine Stress Tests (playwright + playwright_mcp).

Test A: Rapid Navigation
  50-cycle loop of: navigate DDG → search → extract titles →
  navigate Books to Scrape → click Travel → snapshot.
  Measures latency, JSON truncation, disconnects, memory growth,
  MCP session reuse.

Test B: Parallel Sessions
  Spawn 5 concurrent sessions, each: init → navigate → snapshot →
  extract title → close.  All must succeed.

Run with:
    pytest tests/stress/test_phase2_browser_engine_stress.py -v -m phase2
"""

from __future__ import annotations

import asyncio
import json
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from backend.config import config
from backend.models import ActionType, AgentAction
from backend.agent.executor import execute_action

from tests.stress.helpers import (
    BROWSER_ENGINES,
    ENGINE_MODES,
    SITES,
    STRESS,
    make_click_action,
    run_async,
)


# ── Constants ─────────────────────────────────────────────────────────────────

RAPID_NAV_CYCLES = 50
PARALLEL_SESSION_COUNT = 5
DDG = SITES["duckduckgo"]
BOOKS = SITES["books"]

# Simulated snapshot text with accessibility refs (realistic MCP output)
_FAKE_SNAPSHOT_TEXT = (
    "- document [ref=D1]\n"
    "  - navigation [ref=N1]\n"
    "    - link 'Home' [ref=L1]\n"
    "    - link 'Travel' [ref=L2]\n"
    "    - link 'Mystery' [ref=L3]\n"
    "  - heading 'All products' [ref=H1]\n"
    "  - list\n"
    "    - listitem\n"
    "      - link 'A Light in the Attic' [ref=B1]\n"
    "    - listitem\n"
    "      - link 'Tipping the Velvet' [ref=B2]\n"
)

# Simulated search result titles (realistic DDG output)
_FAKE_SEARCH_RESULTS = json.dumps([
    {"title": "CUA Automation - GitHub", "url": "https://github.com/cua"},
    {"title": "Computer Using Agent Docs", "url": "https://docs.cua.dev"},
    {"title": "Browser Automation with CUA", "url": "https://example.com/cua"},
])

# Large snapshot to test truncation handling
_OVERSIZED_SNAPSHOT = "- " + ("node [ref=X1] " * 5000)  # ~75KB


# ── Autouse: zero action delay ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep in the executor for fast stress tests."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


# ── Phase 2 metrics ──────────────────────────────────────────────────────────

@dataclass
class BrowserStressMetrics:
    """Extended metrics for browser engine stress tests."""

    cycles_completed: int = 0
    total_actions: int = 0
    successful: int = 0
    failed: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    disconnects: int = 0
    json_truncations: int = 0
    mcp_session_reuses: int = 0
    mcp_session_inits: int = 0
    errors: List[str] = field(default_factory=list)
    memory_samples_bytes: List[int] = field(default_factory=list)

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
    def memory_growth_bytes(self) -> int:
        if len(self.memory_samples_bytes) < 2:
            return 0
        return self.memory_samples_bytes[-1] - self.memory_samples_bytes[0]

    def record_action(self, success: bool, latency_ms: float, error: str | None = None):
        self.total_actions += 1
        self.latencies_ms.append(latency_ms)
        if success:
            self.successful += 1
        else:
            self.failed += 1
            if error:
                self.errors.append(error)

    def sample_memory(self):
        """Snapshot current tracemalloc reading."""
        current, _ = tracemalloc.get_traced_memory()
        self.memory_samples_bytes.append(current)


# ── Helper: timed execution ──────────────────────────────────────────────────

async def _timed_execute(action, mode, engine):
    """Execute an action and return (result, elapsed_ms)."""
    start = time.perf_counter()
    result = await execute_action(action, mode=mode, engine=engine)
    elapsed = (time.perf_counter() - start) * 1000
    return result, elapsed


def _mode_for(engine: str) -> str:
    return ENGINE_MODES.get(engine, "browser")


# ══════════════════════════════════════════════════════════════════════════════
# TEST A — Rapid Navigation (per browser engine)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase2
class TestRapidNavigationPlaywright:
    """50-cycle rapid navigation stress for the ``playwright`` engine."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_rapid_navigation_50_cycles(self, mock_send):
        """
        Loop 50×:
          1. navigate to duckduckgo.com
          2. type "CUA automation" in search
          3. press Enter (submit)
          4. extract result titles (evaluate_js)
          5. navigate to books.toscrape.com
          6. click Travel category
          7. get snapshot (evaluate_js)
        Verify: latency, success rate, throughput.
        """
        mock_send.return_value = {"success": True, "message": "OK"}
        metrics = BrowserStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(RAPID_NAV_CYCLES):
            actions = self._build_cycle_actions()
            for action in actions:
                result, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright")
                )
                metrics.record_action(result.get("success", False), elapsed)

            metrics.cycles_completed += 1

            # Memory sample every 10 cycles
            if cycle % 10 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        # ── Assertions ────────────────────────────────────────────────
        assert metrics.cycles_completed == RAPID_NAV_CYCLES
        expected_actions = RAPID_NAV_CYCLES * 7  # 7 actions per cycle
        assert metrics.total_actions == expected_actions
        assert metrics.success_rate >= STRESS.min_success_rate
        assert metrics.avg_latency_ms < STRESS.max_acceptable_latency_ms
        assert metrics.memory_growth_bytes < STRESS.max_memory_growth_mb * 1024 * 1024

    def _build_cycle_actions(self) -> list:
        """Build the 7-action sequence for one navigation cycle."""
        return [
            # 1. Navigate to DuckDuckGo
            AgentAction(action=ActionType.OPEN_URL, text=DDG.url),
            # 2. Type search query
            AgentAction(action=ActionType.TYPE, text="CUA automation", coordinates=[720, 400]),
            # 3. Press Enter
            AgentAction(action=ActionType.KEY, text="Enter"),
            # 4. Extract result titles (JS evaluation)
            AgentAction(
                action=ActionType.EVALUATE_JS,
                text="Array.from(document.querySelectorAll('h2')).map(h=>h.textContent)",
            ),
            # 5. Navigate to Books to Scrape
            AgentAction(action=ActionType.OPEN_URL, text=BOOKS.url),
            # 6. Click 'Travel' category
            AgentAction(action=ActionType.CLICK, coordinates=[200, 500]),
            # 7. Get page snapshot (evaluate)
            AgentAction(
                action=ActionType.EVALUATE_JS,
                text="document.body.innerText.slice(0, 2000)",
            ),
        ]

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_latency_percentiles(self, mock_send):
        """Verify p50/p95 latencies stay within bounds over 20 cycles."""
        mock_send.return_value = {"success": True, "message": "OK"}
        latencies: list[float] = []

        for _ in range(20):
            for action in self._build_cycle_actions():
                _, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright")
                )
                latencies.append(elapsed)

        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]

        assert p50 < 500, f"p50 latency {p50:.1f}ms exceeds 500ms"
        assert p95 < 2000, f"p95 latency {p95:.1f}ms exceeds 2000ms"

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_disconnect_resilience(self, mock_send):
        """Simulate intermittent disconnects; verify graceful degradation."""
        call_count = 0

        async def _intermittent_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Every 7th call simulates a disconnect
            if call_count % 7 == 0:
                return {
                    "success": False,
                    "message": "Agent service unreachable",
                    "error_type": "service_error",
                }
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _intermittent_send
        metrics = BrowserStressMetrics()

        for cycle in range(RAPID_NAV_CYCLES):
            for action in self._build_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright")
                )
                success = result.get("success", False)
                metrics.record_action(success, elapsed)
                if not success and "unreachable" in result.get("message", ""):
                    metrics.disconnects += 1

            metrics.cycles_completed += 1

        assert metrics.cycles_completed == RAPID_NAV_CYCLES
        # ~1/7 actions fail → success rate ~85%
        assert metrics.success_rate >= 0.70
        assert metrics.disconnects > 0, "Expected some disconnect events"
        assert metrics.disconnects < metrics.total_actions * 0.20


@pytest.mark.stress
@pytest.mark.phase2
class TestRapidNavigationMCP:
    """50-cycle rapid navigation stress for the ``playwright_mcp`` engine."""

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_rapid_navigation_50_cycles(self, mock_mcp):
        """
        Loop 50×: navigate DDG → search → extract → navigate Books →
        click Travel → snapshot.  All through MCP dispatch.
        """
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        metrics = BrowserStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(RAPID_NAV_CYCLES):
            actions = self._build_mcp_cycle_actions()
            for action in actions:
                result, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright_mcp")
                )
                metrics.record_action(result.get("success", False), elapsed)

            metrics.cycles_completed += 1
            if cycle % 10 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        assert metrics.cycles_completed == RAPID_NAV_CYCLES
        expected_actions = RAPID_NAV_CYCLES * 7
        assert metrics.total_actions == expected_actions
        assert metrics.success_rate >= STRESS.min_success_rate
        assert metrics.avg_latency_ms < STRESS.max_acceptable_latency_ms
        assert metrics.memory_growth_bytes < STRESS.max_memory_growth_mb * 1024 * 1024

    def _build_mcp_cycle_actions(self) -> list:
        """Build 7-action MCP sequence (uses target selectors, not coords)."""
        return [
            # 1. Navigate to DuckDuckGo
            AgentAction(action=ActionType.OPEN_URL, text=DDG.url),
            # 2. Type search query (MCP needs target)
            AgentAction(
                action=ActionType.TYPE,
                text="CUA automation",
                target='input[name="q"]',
                coordinates=[720, 400],
            ),
            # 3. Press Enter
            AgentAction(action=ActionType.KEY, text="Enter"),
            # 4. Extract result titles
            AgentAction(
                action=ActionType.EVALUATE_JS,
                text="Array.from(document.querySelectorAll('h2')).map(h=>h.textContent)",
            ),
            # 5. Navigate to Books to Scrape
            AgentAction(action=ActionType.OPEN_URL, text=BOOKS.url),
            # 6. Click 'Travel' (MCP needs target)
            AgentAction(action=ActionType.CLICK, target="Travel", coordinates=[200, 500]),
            # 7. Get page snapshot
            AgentAction(action=ActionType.GET_SNAPSHOT),
        ]

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_json_truncation_handling(self, mock_mcp):
        """Large JSON responses must not crash; truncation is detected."""
        metrics = BrowserStressMetrics()
        # Full snapshot length for reference
        full_len = len(_OVERSIZED_SNAPSHOT)
        truncated_snapshot = _OVERSIZED_SNAPSHOT[:5000]

        async def _truncation_aware_response(*args, **kwargs):
            action_name = args[0] if args else kwargs.get("action", "")
            # Simulate large snapshot responses that get truncated
            if action_name in ("get_snapshot", "get_accessibility_tree"):
                return {
                    "success": True,
                    "message": truncated_snapshot,
                    "engine": "playwright_mcp",
                    "_full_length": full_len,
                }
            return {"success": True, "message": "OK", "engine": "playwright_mcp"}

        mock_mcp.side_effect = _truncation_aware_response

        for cycle in range(RAPID_NAV_CYCLES):
            for action in self._build_mcp_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright_mcp")
                )
                metrics.record_action(result.get("success", False), elapsed)

                # Detect truncation: response has _full_length > message length
                msg = result.get("message", "")
                reported_full = result.get("_full_length", len(msg))
                if reported_full > len(msg):
                    metrics.json_truncations += 1

            metrics.cycles_completed += 1

        assert metrics.cycles_completed == RAPID_NAV_CYCLES
        assert metrics.success_rate >= STRESS.min_success_rate
        # Truncations happen on snapshot actions (1 per cycle)
        assert metrics.json_truncations > 0, "Expected truncation detections"
        assert metrics.json_truncations == RAPID_NAV_CYCLES, (
            f"Expected {RAPID_NAV_CYCLES} truncations (1 per cycle), got {metrics.json_truncations}"
        )

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_mcp_disconnect_and_session_recovery(self, mock_mcp):
        """Simulate MCP disconnects; verify session re-init tracking."""
        call_count = 0

        async def _disconnect_sim(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 10 == 0:
                return {
                    "success": False,
                    "message": "MCP server disconnected",
                    "engine": "playwright_mcp",
                }
            return {"success": True, "message": "OK", "engine": "playwright_mcp"}

        mock_mcp.side_effect = _disconnect_sim
        metrics = BrowserStressMetrics()

        for cycle in range(RAPID_NAV_CYCLES):
            for action in self._build_mcp_cycle_actions():
                result, elapsed = run_async(
                    _timed_execute(action, mode="browser", engine="playwright_mcp")
                )
                success = result.get("success", False)
                metrics.record_action(success, elapsed)
                if not success and "disconnect" in result.get("message", "").lower():
                    metrics.disconnects += 1

            metrics.cycles_completed += 1

        assert metrics.cycles_completed == RAPID_NAV_CYCLES
        assert metrics.success_rate >= 0.70
        assert metrics.disconnects > 0, "Expected MCP disconnect events"

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_mcp_session_reuse_tracking(self, mock_mcp):
        """Verify MCP session ID is reused across calls (no re-init per action)."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        import backend.agent.playwright_mcp_client as mcp_mod

        # Pre-set a session to simulate an already-initialized session
        original_session = mcp_mod._mcp_session_id
        mcp_mod._mcp_session_id = "test-session-12345"
        try:
            metrics = BrowserStressMetrics()

            for cycle in range(10):
                for action in self._build_mcp_cycle_actions():
                    result, elapsed = run_async(
                        _timed_execute(action, mode="browser", engine="playwright_mcp")
                    )
                    metrics.record_action(result.get("success", False), elapsed)

                    # Session should remain the same (reused)
                    if mcp_mod._mcp_session_id == "test-session-12345":
                        metrics.mcp_session_reuses += 1
                    else:
                        metrics.mcp_session_inits += 1

                metrics.cycles_completed += 1

            assert metrics.cycles_completed == 10
            # Since we mock execute_mcp_action, _mcp_session_id is never cleared
            assert metrics.mcp_session_reuses == metrics.total_actions
            assert metrics.mcp_session_inits == 0
        finally:
            mcp_mod._mcp_session_id = original_session

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_memory_growth_under_rapid_nav(self, mock_mcp):
        """Memory must not grow excessively over 50 rapid navigation cycles."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
        metrics = BrowserStressMetrics()

        tracemalloc.start()
        metrics.sample_memory()

        for cycle in range(RAPID_NAV_CYCLES):
            for action in self._build_mcp_cycle_actions():
                run_async(
                    _timed_execute(action, mode="browser", engine="playwright_mcp")
                )
            metrics.cycles_completed += 1
            if cycle % 5 == 0:
                metrics.sample_memory()

        metrics.sample_memory()
        tracemalloc.stop()

        growth_mb = metrics.memory_growth_bytes / (1024 * 1024)
        assert growth_mb < STRESS.max_memory_growth_mb, (
            f"Memory grew {growth_mb:.1f}MB over {RAPID_NAV_CYCLES} cycles "
            f"(limit: {STRESS.max_memory_growth_mb}MB)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TEST B — Parallel Sessions
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase2
class TestParallelSessionsPlaywright:
    """Spawn 5 concurrent browser sessions through ``playwright``."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_parallel_sessions_all_succeed(self, mock_send):
        """5 concurrent sessions: init → navigate → snapshot → title → close."""
        mock_send.return_value = {"success": True, "message": "OK"}

        session_results: Dict[int, Dict[str, Any]] = {}

        async def _run_session(session_id: int):
            """Single session lifecycle."""
            results = {"actions": [], "success": True}

            # 1. Initialize (navigate to home)
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.OPEN_URL, text=f"https://httpbin.org/get?session={session_id}"),
                mode="browser", engine="playwright",
            )
            results["actions"].append(("init", r.get("success")))

            # 2. Navigate to a target site
            site = list(SITES.values())[session_id % len(SITES)]
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.OPEN_URL, text=site.url),
                mode="browser", engine="playwright",
            )
            results["actions"].append(("navigate", r.get("success")))

            # 3. Snapshot
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.EVALUATE_JS, text="document.body.innerText.slice(0, 1000)"),
                mode="browser", engine="playwright",
            )
            results["actions"].append(("snapshot", r.get("success")))

            # 4. Extract title
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.EVALUATE_JS, text="document.title"),
                mode="browser", engine="playwright",
            )
            results["actions"].append(("title", r.get("success")))

            # 5. Close (navigate away / done)
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.DONE, reasoning=f"Session {session_id} complete"),
                mode="browser", engine="playwright",
            )
            results["actions"].append(("close", r.get("success")))

            results["success"] = all(s for _, s in results["actions"])
            session_results[session_id] = results

        async def _run_all():
            tasks = [_run_session(i) for i in range(PARALLEL_SESSION_COUNT)]
            await asyncio.gather(*tasks, return_exceptions=True)

        run_async(_run_all())

        assert len(session_results) == PARALLEL_SESSION_COUNT
        for sid, res in session_results.items():
            assert res["success"], (
                f"Session {sid} failed: {res['actions']}"
            )
            assert len(res["actions"]) == 5, f"Session {sid} incomplete"

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_parallel_sessions_no_cross_contamination(self, mock_send):
        """Actions from different sessions must carry correct engine tags."""
        mock_send.return_value = {"success": True, "message": "OK"}

        engine_tags: Dict[int, List[str]] = {i: [] for i in range(PARALLEL_SESSION_COUNT)}

        async def _session(sid: int):
            for _ in range(10):
                action = AgentAction(
                    action=ActionType.CLICK,
                    coordinates=[100 + sid * 50, 200],
                )
                r, _ = await _timed_execute(action, mode="browser", engine="playwright")
                engine_tags[sid].append(r.get("engine", "MISSING"))

        async def _run():
            await asyncio.gather(*[_session(i) for i in range(PARALLEL_SESSION_COUNT)])

        run_async(_run())

        for sid, tags in engine_tags.items():
            assert len(tags) == 10
            assert all(t == "playwright" for t in tags), (
                f"Session {sid} had non-playwright tags: {set(tags)}"
            )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    def test_parallel_sessions_latency_isolation(self, mock_send):
        """No single session should dominate latency across parallel runs."""
        # Simulate slight variance
        call_idx = 0

        async def _variant_send(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            # Occasional slow response
            if call_idx % 13 == 0:
                await asyncio.sleep(0.01)  # 10ms "slow" call
            return {"success": True, "message": "OK"}

        mock_send.side_effect = _variant_send

        session_latencies: Dict[int, List[float]] = {i: [] for i in range(PARALLEL_SESSION_COUNT)}

        async def _session(sid: int):
            for _ in range(20):
                action = AgentAction(action=ActionType.OPEN_URL, text=SITES["httpbin"].url)
                _, elapsed = await _timed_execute(action, mode="browser", engine="playwright")
                session_latencies[sid].append(elapsed)

        async def _run():
            await asyncio.gather(*[_session(i) for i in range(PARALLEL_SESSION_COUNT)])

        run_async(_run())

        for sid, lats in session_latencies.items():
            assert len(lats) == 20
            avg = sum(lats) / len(lats)
            assert avg < 500, f"Session {sid} avg latency {avg:.1f}ms too high"


@pytest.mark.stress
@pytest.mark.phase2
class TestParallelSessionsMCP:
    """Spawn 5 concurrent browser sessions through ``playwright_mcp``."""

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_parallel_sessions_all_succeed(self, mock_mcp):
        """5 concurrent MCP sessions: init → navigate → snapshot → title → close."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        session_results: Dict[int, Dict[str, Any]] = {}

        async def _run_session(session_id: int):
            results = {"actions": [], "success": True}

            # 1. Initialize
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.OPEN_URL, text=f"https://httpbin.org/get?session={session_id}"),
                mode="browser", engine="playwright_mcp",
            )
            results["actions"].append(("init", r.get("success")))

            # 2. Navigate
            site = list(SITES.values())[session_id % len(SITES)]
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.OPEN_URL, text=site.url),
                mode="browser", engine="playwright_mcp",
            )
            results["actions"].append(("navigate", r.get("success")))

            # 3. Snapshot
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.GET_SNAPSHOT),
                mode="browser", engine="playwright_mcp",
            )
            results["actions"].append(("snapshot", r.get("success")))

            # 4. Extract title
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.GET_PAGE_TITLE),
                mode="browser", engine="playwright_mcp",
            )
            results["actions"].append(("title", r.get("success")))

            # 5. Close
            r, _ = await _timed_execute(
                AgentAction(action=ActionType.DONE, reasoning=f"MCP session {session_id} complete"),
                mode="browser", engine="playwright_mcp",
            )
            results["actions"].append(("close", r.get("success")))

            results["success"] = all(s for _, s in results["actions"])
            session_results[session_id] = results

        async def _run_all():
            await asyncio.gather(
                *[_run_session(i) for i in range(PARALLEL_SESSION_COUNT)],
                return_exceptions=True,
            )

        run_async(_run_all())

        assert len(session_results) == PARALLEL_SESSION_COUNT
        for sid, res in session_results.items():
            assert res["success"], f"MCP session {sid} failed: {res['actions']}"
            assert len(res["actions"]) == 5

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_parallel_sessions_no_cross_contamination(self, mock_mcp):
        """MCP actions from different sessions must carry correct engine tags."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        engine_tags: Dict[int, List[str]] = {i: [] for i in range(PARALLEL_SESSION_COUNT)}

        async def _session(sid: int):
            for _ in range(10):
                action = AgentAction(
                    action=ActionType.CLICK,
                    target=f"#btn-{sid}",
                    coordinates=[100, 200],
                )
                r, _ = await _timed_execute(action, mode="browser", engine="playwright_mcp")
                engine_tags[sid].append(r.get("engine", "MISSING"))

        async def _run():
            await asyncio.gather(*[_session(i) for i in range(PARALLEL_SESSION_COUNT)])

        run_async(_run())

        for sid, tags in engine_tags.items():
            assert len(tags) == 10
            assert all(t == "playwright_mcp" for t in tags), (
                f"MCP Session {sid} had wrong tags: {set(tags)}"
            )

    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_parallel_sessions_mixed_actions(self, mock_mcp):
        """Each parallel session runs a different action mix; all must succeed."""
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        session_action_counts: Dict[int, int] = {}

        async def _varied_session(sid: int):
            count = 0
            # Each session gets a unique action mix
            action_sequences = [
                [ActionType.OPEN_URL, ActionType.GET_SNAPSHOT, ActionType.GET_PAGE_TITLE],
                [ActionType.OPEN_URL, ActionType.CLICK, ActionType.KEY, ActionType.GET_SNAPSHOT],
                [ActionType.OPEN_URL, ActionType.EVALUATE_JS, ActionType.GET_SNAPSHOT],
                [ActionType.OPEN_URL, ActionType.GET_ALL_TEXT, ActionType.GET_LINKS],
                [ActionType.OPEN_URL, ActionType.GET_SNAPSHOT, ActionType.GO_BACK],
            ]
            seq = action_sequences[sid % len(action_sequences)]
            for atype in seq:
                if atype == ActionType.OPEN_URL:
                    action = AgentAction(action=atype, text=SITES["books"].url)
                elif atype in (ActionType.CLICK, ActionType.HOVER):
                    action = AgentAction(action=atype, target="#link", coordinates=[100, 200])
                elif atype == ActionType.KEY:
                    action = AgentAction(action=atype, text="Enter")
                elif atype == ActionType.EVALUATE_JS:
                    action = AgentAction(action=atype, text="document.title")
                else:
                    action = AgentAction(action=atype)

                r, _ = await _timed_execute(action, mode="browser", engine="playwright_mcp")
                if r.get("success"):
                    count += 1

            session_action_counts[sid] = count

        async def _run():
            await asyncio.gather(*[_varied_session(i) for i in range(PARALLEL_SESSION_COUNT)])

        run_async(_run())

        for sid, c in session_action_counts.items():
            assert c > 0, f"Session {sid} had 0 successful actions"


# ══════════════════════════════════════════════════════════════════════════════
# Cross-engine comparison
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.stress
@pytest.mark.phase2
class TestBrowserEngineCrossComparison:
    """Compare playwright vs playwright_mcp under identical workloads."""

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_both_engines_complete_same_workload(self, mock_mcp, mock_send):
        """Both engines must complete the same 20-cycle navigation workload."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        per_engine_metrics: Dict[str, BrowserStressMetrics] = {}

        for engine in BROWSER_ENGINES:
            metrics = BrowserStressMetrics()
            for cycle in range(20):
                for site in SITES.values():
                    action = AgentAction(action=ActionType.OPEN_URL, text=site.url)
                    result, elapsed = run_async(
                        _timed_execute(action, mode="browser", engine=engine)
                    )
                    metrics.record_action(result.get("success", False), elapsed)
                metrics.cycles_completed += 1
            per_engine_metrics[engine] = metrics

        for engine, m in per_engine_metrics.items():
            assert m.cycles_completed == 20, f"{engine}: only {m.cycles_completed} cycles"
            assert m.success_rate >= STRESS.min_success_rate, (
                f"{engine} success rate {m.success_rate:.1%} below threshold"
            )

    @patch("backend.agent.executor._send_with_retry", new_callable=AsyncMock)
    @patch("backend.agent.playwright_mcp_client.execute_mcp_action", new_callable=AsyncMock)
    def test_engine_tag_integrity_under_rapid_switching(self, mock_mcp, mock_send):
        """Rapidly alternate between playwright/MCP; engine tags must stay correct."""
        mock_send.return_value = {"success": True, "message": "OK"}
        mock_mcp.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}

        for _ in range(100):
            for engine in BROWSER_ENGINES:
                if engine == "playwright":
                    action = make_click_action(300, 400)
                else:
                    action = AgentAction(
                        action=ActionType.CLICK,
                        target="#test",
                        coordinates=[300, 400],
                    )
                result = run_async(
                    execute_action(action, mode="browser", engine=engine)
                )
                assert result.get("engine") == engine, (
                    f"Tag mismatch: expected {engine}, got {result.get('engine')}"
                )
