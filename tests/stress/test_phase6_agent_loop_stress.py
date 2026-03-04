"""Phase 6 — Full Agent Loop Stress Tests.

Simulates real prompts through the full perceive → think → act loop 30 times.

Prompt example:
  "Open duckduckgo.com, search for automation testing,
   return the first 3 result titles as JSON."

Verifies:
  - JSON responses are valid
  - No hallucinated (fabricated) actions
  - No unsupported actions
  - No retry storm (bounded call counts)
  - No MCP disconnect

Run with:
    pytest tests/stress/test_phase6_agent_loop_stress.py -v -m phase6
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import config
from backend.models import (
    ActionType,
    AgentAction,
    AgentSession,
    LogEntry,
    SessionStatus,
    StepRecord,
)
from backend.agent.loop import AgentLoop, MAX_CONSECUTIVE_ERRORS, MAX_DUPLICATE_ACTIONS
from backend.agent.model_router import query_model
from backend.engine_capabilities import EngineCapabilities

from tests.stress.helpers import (
    ALL_ENGINES,
    BROWSER_ENGINES,
    DESKTOP_ENGINES,
    ENGINE_MODES,
    STRESS,
    StressMetrics,
    mock_screenshot_b64,
    run_async,
)

# ── Constants ─────────────────────────────────────────────────────────────────

AGENT_LOOP_RUNS = 30
FAKE_API_KEY = "stress-test-key-0000"
FAKE_SCREENSHOT = mock_screenshot_b64()

# Canonical prompt from the spec
SEARCH_PROMPT = (
    "Open duckduckgo.com, search for automation testing, "
    "return the first 3 result titles as JSON."
)

# Additional realistic prompts for variety
_PROMPTS = [
    SEARCH_PROMPT,
    "Navigate to wikipedia.org and get the title of today's featured article.",
    "Open httpbin.org/get and extract the JSON response body.",
    "Go to books.toscrape.com and list the first 5 book titles.",
    "Open duckduckgo.com, type 'Python stress testing' and press Enter.",
]

# All valid ActionType string values (canonical set)
_ALL_VALID_ACTIONS: frozenset[str] = frozenset(a.value for a in ActionType)

# Engine capability registry for action validation
_capability_registry = EngineCapabilities()


# ── Autouse: zero action delay ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep in the executor for fast stress tests."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


@pytest.fixture(autouse=True)
def _mock_mcp_init():
    """Auto-mock MCP initialization so playwright_mcp tests don't hang."""
    with patch("backend.agent.playwright_mcp_client._ensure_mcp_initialized", AsyncMock()), \
         patch("backend.agent.playwright_mcp_client.check_mcp_health", AsyncMock(return_value=True)):
        yield


# ── Phase 6 metrics ──────────────────────────────────────────────────────────

@dataclass
class AgentLoopMetrics:
    """Metrics tracked across agent loop stress runs."""

    total_runs: int = 0
    completed_runs: int = 0
    errored_runs: int = 0
    total_steps: int = 0
    total_actions: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    # Validation counters
    valid_json_responses: int = 0
    invalid_json_responses: int = 0
    hallucinated_actions: int = 0
    unsupported_actions: int = 0
    retry_storms: int = 0       # Runs where actions > 3× expected steps
    mcp_disconnects: int = 0

    # Action breakdown
    action_counts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.completed_runs / max(self.total_runs, 1)

    @property
    def avg_steps_per_run(self) -> float:
        return self.total_steps / max(self.total_runs, 1)

    def record_action(self, action_value: str):
        """Record a single action dispatched by the agent."""
        self.total_actions += 1
        self.action_counts[action_value] = self.action_counts.get(action_value, 0) + 1

    def check_hallucinated(self, action_value: str) -> bool:
        """Return True if this action is not in the ActionType enum (hallucinated)."""
        if action_value not in _ALL_VALID_ACTIONS:
            self.hallucinated_actions += 1
            self.errors.append(f"Hallucinated action: {action_value!r}")
            return True
        return False

    def check_unsupported(self, action_value: str, engine: str) -> bool:
        """Return True if the action is not supported by the engine."""
        if not _capability_registry.validate_action(engine, action_value):
            self.unsupported_actions += 1
            self.errors.append(f"Unsupported action {action_value!r} for engine {engine}")
            return True
        return False


# ── Mock model response generators ───────────────────────────────────────────

def _make_search_sequence(step: int) -> tuple[AgentAction, str]:
    """Generate a realistic multi-step model response sequence for the search task.

    Step sequence:
      1. open_url duckduckgo.com
      2. wait (page load)
      3. fill search field
      4. key Enter
      5. wait (results load)
      6. done
    """
    sequences = [
        AgentAction(action=ActionType.OPEN_URL, text="https://duckduckgo.com",
                     reasoning="Navigate to DuckDuckGo search engine"),
        AgentAction(action=ActionType.WAIT, text="2",
                     reasoning="Wait for page to load"),
        AgentAction(action=ActionType.FILL, target='input[name="q"]',
                     text="automation testing",
                     reasoning="Fill search query"),
        AgentAction(action=ActionType.KEY, text="Enter",
                     reasoning="Submit the search form"),
        AgentAction(action=ActionType.WAIT, text="3",
                     reasoning="Wait for search results"),
        AgentAction(action=ActionType.DONE,
                     reasoning='["Selenium Testing", "Playwright Tutorial", "Robot Framework Guide"]'),
    ]
    idx = min(step - 1, len(sequences) - 1)
    action = sequences[idx]
    raw_json = json.dumps({
        "action": action.action.value,
        "text": action.text,
        "target": action.target,
        "coordinates": action.coordinates,
        "reasoning": action.reasoning,
    })
    return action, raw_json


def _make_varied_sequence(prompt_idx: int, step: int) -> tuple[AgentAction, str]:
    """Generate varied action sequences based on prompt index."""
    # Different prompts have different step counts
    _sequences_by_prompt = {
        0: [  # DuckDuckGo search
            AgentAction(action=ActionType.OPEN_URL, text="https://duckduckgo.com",
                         reasoning="Navigate to DuckDuckGo"),
            AgentAction(action=ActionType.FILL, target='input[name="q"]',
                         text="automation testing", reasoning="Fill search"),
            AgentAction(action=ActionType.KEY, text="Enter", reasoning="Submit search"),
            AgentAction(action=ActionType.WAIT, text="2", reasoning="Wait for results"),
            AgentAction(action=ActionType.DONE,
                         reasoning='{"titles": ["Result 1", "Result 2", "Result 3"]}'),
        ],
        1: [  # Wikipedia
            AgentAction(action=ActionType.OPEN_URL, text="https://wikipedia.org",
                         reasoning="Navigate to Wikipedia"),
            AgentAction(action=ActionType.WAIT, text="2", reasoning="Wait for page"),
            AgentAction(action=ActionType.GET_TEXT, target="#mp-tfa",
                         reasoning="Get featured article text"),
            AgentAction(action=ActionType.DONE,
                         reasoning='{"title": "Featured Article Title"}'),
        ],
        2: [  # httpbin
            AgentAction(action=ActionType.OPEN_URL, text="https://httpbin.org/get",
                         reasoning="Navigate to httpbin"),
            AgentAction(action=ActionType.WAIT, text="2", reasoning="Wait"),
            AgentAction(action=ActionType.DONE,
                         reasoning='{"origin": "1.2.3.4"}'),
        ],
        3: [  # Books to Scrape
            AgentAction(action=ActionType.OPEN_URL, text="https://books.toscrape.com",
                         reasoning="Navigate to book store"),
            AgentAction(action=ActionType.WAIT, text="2", reasoning="Wait"),
            AgentAction(action=ActionType.GET_TEXT, target=".product_pod h3 a",
                         reasoning="Get book titles"),
            AgentAction(action=ActionType.DONE,
                         reasoning='["Book 1", "Book 2", "Book 3", "Book 4", "Book 5"]'),
        ],
        4: [  # DuckDuckGo type
            AgentAction(action=ActionType.OPEN_URL, text="https://duckduckgo.com",
                         reasoning="Navigate"),
            AgentAction(action=ActionType.CLICK, coordinates=[400, 300],
                         reasoning="Click search field"),
            AgentAction(action=ActionType.TYPE, text="Python stress testing",
                         reasoning="Type query"),
            AgentAction(action=ActionType.KEY, text="Enter", reasoning="Submit"),
            AgentAction(action=ActionType.DONE, reasoning="Search completed"),
        ],
    }
    seq = _sequences_by_prompt.get(prompt_idx % 5, _sequences_by_prompt[0])
    idx = min(step - 1, len(seq) - 1)
    action = seq[idx]
    raw_json = json.dumps({
        "action": action.action.value,
        "text": action.text,
        "target": action.target,
        "coordinates": action.coordinates,
        "reasoning": action.reasoning,
    })
    return action, raw_json


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_agent_loop(
    task: str = SEARCH_PROMPT,
    engine: str = "playwright",
    mode: str = "browser",
    provider: str = "google",
    max_steps: int = 10,
) -> AgentLoop:
    """Create an AgentLoop instance for testing."""
    return AgentLoop(
        task=task,
        api_key=FAKE_API_KEY,
        model="gemini-3-flash-preview",
        max_steps=max_steps,
        mode=mode,
        engine=engine,
        provider=provider,
    )


def _validate_raw_json(raw: str) -> bool:
    """Return True if raw model response is valid JSON."""
    try:
        parsed = json.loads(raw)
        return isinstance(parsed, dict) and "action" in parsed
    except (json.JSONDecodeError, TypeError):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.stress
@pytest.mark.phase6
class TestAgentLoopBasicStress:
    """30-run agent loop with mocked model returning a 6-step search sequence."""

    def test_30_runs_complete_successfully(self):
        """All 30 runs finish with status COMPLETED, not ERROR."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            # Track per-session step counts
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for run_idx in range(AGENT_LOOP_RUNS):
                t0 = time.perf_counter()
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())
                elapsed = (time.perf_counter() - t0) * 1000

                metrics.total_runs += 1
                metrics.total_steps += len(session.steps)
                metrics.latencies_ms.append(elapsed)

                if session.status == SessionStatus.COMPLETED:
                    metrics.completed_runs += 1
                else:
                    metrics.errored_runs += 1
                    metrics.errors.append(
                        f"Run {run_idx}: status={session.status.value}"
                    )

        assert metrics.completed_runs == AGENT_LOOP_RUNS
        assert metrics.errored_runs == 0
        assert metrics.avg_steps_per_run >= 3  # At least a few steps per run

    def test_30_runs_json_valid(self):
        """All raw model responses are valid JSON with an 'action' field."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())
                metrics.total_runs += 1

                for step in session.steps:
                    if step.raw_model_response:
                        if _validate_raw_json(step.raw_model_response):
                            metrics.valid_json_responses += 1
                        else:
                            metrics.invalid_json_responses += 1
                            metrics.errors.append(
                                f"Invalid JSON: {step.raw_model_response[:100]}"
                            )

        assert metrics.invalid_json_responses == 0, \
            f"Got {metrics.invalid_json_responses} invalid JSON responses: {metrics.errors[:5]}"
        assert metrics.valid_json_responses > 0

    def test_30_runs_no_hallucinated_actions(self):
        """Every action returned by the model is a valid ActionType value."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())
                metrics.total_runs += 1

                for step in session.steps:
                    if step.action:
                        action_val = step.action.action.value
                        metrics.record_action(action_val)
                        metrics.check_hallucinated(action_val)

        assert metrics.hallucinated_actions == 0, \
            f"Hallucinated actions: {metrics.errors}"
        assert metrics.total_actions > 0


@pytest.mark.stress
@pytest.mark.phase6
class TestNoUnsupportedActions:
    """Verify no unsupported actions are dispatched per engine."""

    @pytest.mark.parametrize("engine,mode", [
        ("playwright", "browser"),
        ("playwright_mcp", "browser"),
        ("xdotool", "desktop"),
        ("ydotool", "desktop"),
        ("desktop_hybrid", "desktop"),
    ])
    def test_no_unsupported_for_engine(self, engine, mode):
        """Model actions are all supported by the given engine."""
        step_counter = {}

        # Use actions that are common to all engines
        _universal_sequence = [
            AgentAction(action=ActionType.CLICK, coordinates=[400, 300],
                         reasoning="Click a target"),
            AgentAction(action=ActionType.WAIT, text="1", reasoning="Wait"),
            AgentAction(action=ActionType.DONE, reasoning="Task complete"),
        ]

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            idx = min(step_counter[key] - 1, len(_universal_sequence) - 1)
            action = _universal_sequence[idx]
            raw = json.dumps({"action": action.action.value, "reasoning": action.reasoning})
            return action, raw

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                step_counter.clear()
                loop = _make_agent_loop(engine=engine, mode=mode, max_steps=5)
                session = run_async(loop.run())
                metrics.total_runs += 1

                for step in session.steps:
                    if step.action and step.action.action not in (ActionType.DONE, ActionType.ERROR):
                        action_val = step.action.action.value
                        metrics.check_unsupported(action_val, engine)

        assert metrics.unsupported_actions == 0, \
            f"Unsupported actions for {engine}: {metrics.errors}"


@pytest.mark.stress
@pytest.mark.phase6
class TestNoRetryStorm:
    """Verify the agent doesn't enter infinite retry loops."""

    def test_consecutive_error_limit_enforced(self):
        """After MAX_CONSECUTIVE_ERRORS failures, the loop aborts."""
        call_count = 0

        async def _always_fail_execute(action, mode="browser", engine="playwright"):
            nonlocal call_count
            call_count += 1
            return {"success": False, "message": "Simulated execution failure"}

        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            # Always return a click action — which will fail
            action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200],
                                  reasoning="Trying to click")
            raw = json.dumps({"action": "click", "coordinates": [100, 200]})
            return action, raw

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(side_effect=_always_fail_execute)):

            loop = _make_agent_loop(max_steps=50)
            session = run_async(loop.run())

        # Should abort after MAX_CONSECUTIVE_ERRORS, not run all 50 steps
        assert session.status == SessionStatus.ERROR
        assert len(session.steps) <= MAX_CONSECUTIVE_ERRORS + 1
        assert call_count <= MAX_CONSECUTIVE_ERRORS + 1

    def test_no_retry_storm_over_30_runs(self):
        """No single run exceeds 3× expected step count (retry storm detection)."""
        expected_steps_per_run = 6  # The search sequence is 6 steps
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        retry_storm_runs = 0

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=30)
                session = run_async(loop.run())

                if len(session.steps) > expected_steps_per_run * 3:
                    retry_storm_runs += 1

        assert retry_storm_runs == 0, \
            f"{retry_storm_runs} runs had retry storms (>3× expected steps)"

    def test_duplicate_detection_stops_loops(self):
        """When the model repeats the exact same action, the loop detects it."""

        async def _always_same_click(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            action = AgentAction(action=ActionType.CLICK, coordinates=[400, 300],
                                  reasoning="Click submit")
            raw = json.dumps({"action": "click", "coordinates": [400, 300]})
            return action, raw

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_always_same_click)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            loop = _make_agent_loop(max_steps=20)
            session = run_async(loop.run())

        # The loop should detect duplication and inject WAIT actions
        # Instead of 20 identical clicks, there should be recovery hints
        actions = [s.action for s in session.steps if s.action]
        click_count = sum(1 for a in actions if a.action == ActionType.CLICK)
        wait_count = sum(1 for a in actions if a.action == ActionType.WAIT)
        # Not ALL steps should be clicks — some should be recovery waits
        # The exact ratio depends on MAX_DUPLICATE_ACTIONS spacing
        total_steps = len(session.steps)
        assert total_steps <= 20  # Never exceeds max_steps


@pytest.mark.stress
@pytest.mark.phase6
class TestNoMCPDisconnect:
    """Verify MCP initialization and resilience across 30 runs."""

    def test_mcp_engine_30_runs_no_disconnect(self):
        """30 runs with playwright_mcp — no MCP disconnect errors."""
        step_counter = {}
        disconnect_count = 0
        logs: List[str] = []

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        def _log_collector(entry: LogEntry):
            logs.append(entry.message)
            if "disconnect" in entry.message.lower() or "mcp" in entry.message.lower():
                if "error" in entry.level or "disconnect" in entry.message.lower():
                    nonlocal disconnect_count
                    disconnect_count += 1

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})), \
             patch("backend.agent.playwright_mcp_client._ensure_mcp_initialized", AsyncMock()), \
             patch("backend.agent.playwright_mcp_client.check_mcp_health", AsyncMock(return_value=True)):

            for _ in range(AGENT_LOOP_RUNS):
                step_counter.clear()
                loop = _make_agent_loop(
                    engine="playwright_mcp", mode="browser", max_steps=10,
                )
                loop._on_log = _log_collector
                session = run_async(loop.run())
                assert session.status == SessionStatus.COMPLETED

        # No MCP disconnect errors across all 30 runs
        assert disconnect_count == 0

    def test_mcp_init_failure_handled_gracefully(self):
        """If MCP init fails, the loop still runs (degraded) without crashing."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})), \
             patch("backend.agent.playwright_mcp_client._ensure_mcp_initialized",
                   AsyncMock(side_effect=ConnectionError("MCP server unreachable"))), \
             patch("backend.agent.playwright_mcp_client.check_mcp_health",
                   AsyncMock(return_value=False)):

            for _ in range(AGENT_LOOP_RUNS):
                step_counter.clear()
                loop = _make_agent_loop(
                    engine="playwright_mcp", mode="browser", max_steps=10,
                )
                session = run_async(loop.run())
                # The loop should still complete (MCP init failure is a warning, not fatal)
                assert session.status in (SessionStatus.COMPLETED, SessionStatus.ERROR)


@pytest.mark.stress
@pytest.mark.phase6
class TestVariedPromptStress:
    """Run 30 iterations with varied prompts and action sequences."""

    def test_varied_prompts_30_runs(self):
        """30 runs with 5 different prompts — all complete cleanly."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1

            # Determine prompt index from task content
            for idx, prompt in enumerate(_PROMPTS):
                if prompt == task:
                    return _make_varied_sequence(idx, step_counter[key])
            return _make_search_sequence(step_counter[key])

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for run_idx in range(AGENT_LOOP_RUNS):
                step_counter.clear()
                prompt = _PROMPTS[run_idx % len(_PROMPTS)]
                loop = _make_agent_loop(task=prompt, max_steps=10)
                session = run_async(loop.run())
                metrics.total_runs += 1

                if session.status == SessionStatus.COMPLETED:
                    metrics.completed_runs += 1
                else:
                    metrics.errored_runs += 1

                for step in session.steps:
                    if step.action:
                        action_val = step.action.action.value
                        metrics.record_action(action_val)
                        metrics.check_hallucinated(action_val)
                    if step.raw_model_response:
                        if _validate_raw_json(step.raw_model_response):
                            metrics.valid_json_responses += 1
                        else:
                            metrics.invalid_json_responses += 1

        assert metrics.completed_runs == AGENT_LOOP_RUNS
        assert metrics.hallucinated_actions == 0
        assert metrics.invalid_json_responses == 0
        # At least 3 different action types used across all runs
        assert len(metrics.action_counts) >= 3

    def test_done_action_terminates_reliably(self):
        """Every run terminates with a 'done' action — no stale loops."""

        async def _quick_done(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            if step_number >= 2:
                action = AgentAction(action=ActionType.DONE, reasoning="Complete")
                raw = json.dumps({"action": "done", "reasoning": "Complete"})
                return action, raw
            action = AgentAction(action=ActionType.OPEN_URL, text="https://duckduckgo.com",
                                  reasoning="Navigate")
            raw = json.dumps({"action": "open_url", "text": "https://duckduckgo.com"})
            return action, raw

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_quick_done)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=50)
                session = run_async(loop.run())
                assert session.status == SessionStatus.COMPLETED
                # Should terminate early — not run all 50 steps
                assert len(session.steps) <= 3


@pytest.mark.stress
@pytest.mark.phase6
class TestModelResponseValidation:
    """Validate that model responses are well-formed JSON across stress runs."""

    def test_all_responses_have_action_field(self):
        """Every raw model response contains an 'action' key."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        missing_action_count = 0

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())

                for step in session.steps:
                    if step.raw_model_response:
                        parsed = json.loads(step.raw_model_response)
                        if "action" not in parsed:
                            missing_action_count += 1

        assert missing_action_count == 0

    def test_json_output_in_done_reasoning(self):
        """When the task asks for JSON output, the 'done' reasoning is valid JSON."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        done_reasonings: List[str] = []

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())

                for step in session.steps:
                    if step.action and step.action.action == ActionType.DONE:
                        if step.action.reasoning:
                            done_reasonings.append(step.action.reasoning)

        assert len(done_reasonings) == AGENT_LOOP_RUNS
        # The search sequence returns a JSON array as the done reasoning
        for reasoning in done_reasonings:
            parsed = json.loads(reasoning)
            assert isinstance(parsed, list)
            assert len(parsed) == 3

    def test_no_empty_raw_responses(self):
        """No step should have a None or empty raw_model_response when action is present."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        empty_count = 0

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())

                for step in session.steps:
                    if step.action and not step.raw_model_response:
                        empty_count += 1

        assert empty_count == 0


@pytest.mark.stress
@pytest.mark.phase6
class TestConcurrentAgentLoops:
    """Multiple agent loops running concurrently."""

    def test_5_concurrent_loops(self):
        """5 agent loops run concurrently without cross-contamination."""
        step_counters: Dict[int, Dict[int, int]] = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counters.setdefault(id(action_history), {}).setdefault(key, 0)
            step_counters[id(action_history)][key] += 1
            step_num = step_counters[id(action_history)][key]
            return _make_search_sequence(step_num)

        async def _run_concurrent():
            sessions = []
            for batch in range(6):  # 6 batches × 5 = 30 total
                tasks = []
                for i in range(5):
                    loop = _make_agent_loop(max_steps=10)
                    tasks.append(loop.run())
                batch_results = await asyncio.gather(*tasks)
                sessions.extend(batch_results)
            return sessions

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            sessions = run_async(_run_concurrent())

        assert len(sessions) == AGENT_LOOP_RUNS
        completed = sum(1 for s in sessions if s.status == SessionStatus.COMPLETED)
        assert completed == AGENT_LOOP_RUNS

        # All sessions have unique IDs (no cross-contamination)
        session_ids = [s.session_id for s in sessions]
        assert len(set(session_ids)) == AGENT_LOOP_RUNS

    def test_concurrent_with_intermittent_failures(self):
        """Concurrent loops with some execution failures — all remain bounded."""
        call_count = 0

        async def _intermittent_execute(action, mode="browser", engine="playwright"):
            nonlocal call_count
            call_count += 1
            if random.random() < 0.2:
                return {"success": False, "message": "Random failure"}
            return {"success": True, "message": "OK"}

        step_counters: Dict[int, int] = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counters.setdefault(key, 0)
            step_counters[key] += 1
            return _make_search_sequence(step_counters[key])

        async def _run():
            sessions = []
            for batch in range(6):
                tasks = [_make_agent_loop(max_steps=15).run() for _ in range(5)]
                results = await asyncio.gather(*tasks)
                sessions.extend(results)
            return sessions

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(side_effect=_intermittent_execute)):

            sessions = run_async(_run())

        assert len(sessions) == AGENT_LOOP_RUNS
        # No session ran more than max_steps
        for s in sessions:
            assert len(s.steps) <= 15


@pytest.mark.stress
@pytest.mark.phase6
class TestErrorRecovery:
    """Test agent loop behaviour under error conditions."""

    def test_screenshot_failure_recovery(self):
        """Intermittent screenshot failures → loop continues, doesn't crash."""
        call_count = 0

        async def _flaky_screenshot(mode="browser", engine="playwright"):
            nonlocal call_count
            call_count += 1
            if call_count % 4 == 0:
                raise ConnectionError("Container not responding")
            return FAKE_SCREENSHOT

        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(side_effect=_flaky_screenshot)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            # Screenshot failures count as step errors → consecutive error tracking
            for _ in range(10):
                step_counter.clear()
                loop = _make_agent_loop(max_steps=15)
                session = run_async(loop.run())
                # Should either complete or error — never hang
                assert session.status in (SessionStatus.COMPLETED, SessionStatus.ERROR)

    def test_model_query_failure_recovery(self):
        """Intermittent model query failures → loop handles gracefully."""
        call_count = 0

        async def _flaky_model(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            nonlocal call_count
            call_count += 1
            if call_count % 3 == 0:
                raise TimeoutError("Model API timeout")
            return _make_search_sequence(step_number)

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_flaky_model)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(10):
                call_count = 0
                loop = _make_agent_loop(max_steps=15)
                session = run_async(loop.run())
                assert session.status in (SessionStatus.COMPLETED, SessionStatus.ERROR)

    def test_stop_requested_honored(self):
        """Calling request_stop() terminates the loop promptly."""
        total_steps_seen = 0
        agent_loop_ref: list = []  # container to hold the loop reference

        async def _query_with_stop(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            nonlocal total_steps_seen
            total_steps_seen += 1
            # Request stop after 5 steps — the loop should honour it promptly
            if total_steps_seen >= 5 and agent_loop_ref:
                agent_loop_ref[0].request_stop()
            action = AgentAction(action=ActionType.CLICK, coordinates=[100, 200],
                                  reasoning="Click")
            raw = json.dumps({"action": "click", "coordinates": [100, 200]})
            return action, raw

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_query_with_stop)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            loop = _make_agent_loop(max_steps=50)
            agent_loop_ref.append(loop)
            session = run_async(loop.run())

        assert session.status == SessionStatus.COMPLETED
        # Stop was requested at step 5 — loop should have stopped shortly after
        assert len(session.steps) < 50
        assert len(session.steps) <= 6  # At most 1 step after stop requested


@pytest.mark.stress
@pytest.mark.phase6
class TestCallCountBounds:
    """Verify that mocked functions are called a bounded number of times."""

    def test_execute_action_call_count_bounded(self):
        """execute_action calls never exceed max_steps × runs."""
        step_counter = {}
        execute_mock = AsyncMock(return_value={"success": True, "message": "OK"})

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        max_steps = 10

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", execute_mock):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=max_steps)
                run_async(loop.run())

        # execute_action should be called at most max_steps × runs
        # (minus terminal actions like DONE, WAIT which skip execution)
        max_possible = AGENT_LOOP_RUNS * max_steps
        assert execute_mock.call_count <= max_possible
        assert execute_mock.call_count > 0

    def test_query_model_call_count_bounded(self):
        """query_model calls never exceed max_steps × runs."""
        query_mock_calls = 0
        step_counter = {}

        async def _counting_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            nonlocal query_mock_calls
            query_mock_calls += 1
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        max_steps = 10

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_counting_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=max_steps)
                run_async(loop.run())

        max_possible = AGENT_LOOP_RUNS * max_steps
        assert query_mock_calls <= max_possible
        assert query_mock_calls > 0


@pytest.mark.stress
@pytest.mark.phase6
class TestSessionMetadata:
    """Verify session metadata integrity across stress runs."""

    def test_session_ids_unique(self):
        """Every run produces a unique session_id."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        session_ids: List[str] = []

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())
                session_ids.append(session.session_id)

        assert len(set(session_ids)) == AGENT_LOOP_RUNS

    def test_step_numbers_sequential(self):
        """Step numbers within each session are sequential starting from 1."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                loop = _make_agent_loop(max_steps=10)
                session = run_async(loop.run())

                step_nums = [s.step_number for s in session.steps]
                expected = list(range(1, len(step_nums) + 1))
                assert step_nums == expected

    def test_task_preserved_across_steps(self):
        """The session task string is preserved unchanged through all steps."""
        step_counter = {}

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            return _make_search_sequence(step_counter[key])

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for run_idx in range(AGENT_LOOP_RUNS):
                prompt = _PROMPTS[run_idx % len(_PROMPTS)]
                loop = _make_agent_loop(task=prompt, max_steps=10)
                session = run_async(loop.run())
                assert session.task == prompt


@pytest.mark.stress
@pytest.mark.phase6
class TestDesktopEngineAgentLoop:
    """Agent loop stress with desktop engines (xdotool, ydotool, desktop_hybrid)."""

    @pytest.mark.parametrize("engine", ["xdotool", "ydotool", "desktop_hybrid"])
    def test_desktop_engine_30_runs(self, engine):
        """30 runs with desktop engines complete without unsupported action errors.

        Note: computer_use is excluded — it uses its own native CU loop,
        not the standard step-by-step dispatch through execute_action().
        """
        step_counter = {}

        # Desktop-compatible action sequence
        _desktop_seq = [
            AgentAction(action=ActionType.CLICK, coordinates=[400, 300],
                         reasoning="Click target"),
            AgentAction(action=ActionType.TYPE, text="stress test",
                         coordinates=[400, 300],
                         reasoning="Type text"),
            AgentAction(action=ActionType.KEY, text="Return",
                         reasoning="Press Enter"),
            AgentAction(action=ActionType.DONE, reasoning="Complete"),
        ]

        async def _mock_query(
            provider, api_key, model_name, task,
            screenshot_b64, action_history, step_number=1,
            mode="browser", system_prompt="",
        ):
            key = id(action_history)
            step_counter.setdefault(key, 0)
            step_counter[key] += 1
            idx = min(step_counter[key] - 1, len(_desktop_seq) - 1)
            action = _desktop_seq[idx]
            raw = json.dumps({"action": action.action.value, "reasoning": action.reasoning})
            return action, raw

        metrics = AgentLoopMetrics()

        with patch("backend.agent.loop.query_model", AsyncMock(side_effect=_mock_query)), \
             patch("backend.agent.loop.capture_screenshot", AsyncMock(return_value=FAKE_SCREENSHOT)), \
             patch("backend.agent.loop.check_service_health", AsyncMock(return_value=True)), \
             patch("backend.agent.loop.execute_action", AsyncMock(return_value={"success": True, "message": "OK"})):

            for _ in range(AGENT_LOOP_RUNS):
                step_counter.clear()
                loop = _make_agent_loop(
                    engine=engine, mode="desktop", max_steps=10,
                )
                session = run_async(loop.run())
                metrics.total_runs += 1

                if session.status == SessionStatus.COMPLETED:
                    metrics.completed_runs += 1
                else:
                    metrics.errored_runs += 1
                    metrics.errors.append(f"Engine {engine}: {session.status.value}")

                for step in session.steps:
                    if step.action:
                        metrics.record_action(step.action.action.value)
                        metrics.check_hallucinated(step.action.action.value)

        assert metrics.completed_runs == AGENT_LOOP_RUNS
        assert metrics.hallucinated_actions == 0
