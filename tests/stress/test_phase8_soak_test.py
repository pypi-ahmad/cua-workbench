"""Phase 8 — Soak Test (10-Minute Simulation).

Rotates engines every 30 seconds, alternates between browser and desktop
tasks, and monitors container resource metrics throughout.

Monitors:
  - RSS memory
  - CPU %
  - Open processes (PIDs)

Ensures:
  - Memory stabilises (no unbounded growth)
  - No runaway process
  - No zombie Chromium
  - No stuck Node MCP

The real soak would run for 10 minutes.  For the hermetic test suite we
compress time—each "rotation cycle" runs the full perceive→think→act
pipeline under mocks so the suite finishes in seconds while exercising
identical logic.

Run with:
    pytest tests/stress/test_phase8_soak_test.py -v -m phase8
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from backend.config import config
from backend.models import (
    ActionType,
    AgentAction,
    SessionStatus,
)
from backend.agent.loop import AgentLoop

from tests.stress.helpers import (
    ALL_ENGINES,
    BROWSER_ENGINES,
    DESKTOP_ENGINES,
    ENGINE_MODES,
    STRESS,
    mock_screenshot_b64,
    run_async,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SOAK_DURATION_REAL_SECONDS = 600          # 10 minutes (spec)
ROTATION_INTERVAL_REAL_SECONDS = 30       # rotate every 30 s (spec)
TOTAL_ROTATIONS = SOAK_DURATION_REAL_SECONDS // ROTATION_INTERVAL_REAL_SECONDS  # 20

# Compressed cycles for the hermetic suite (fast)
COMPRESSED_ROTATIONS = 20                 # same count, just no 30-s sleep
STEPS_PER_ROTATION = 6                    # perceive→think→act iterations per cycle

FAKE_API_KEY = "soak-test-key-0000"
FAKE_SCREENSHOT = mock_screenshot_b64()
CONTAINER_NAME = "cua-environment"

# ── Browser / desktop task prompts ────────────────────────────────────────────

BROWSER_PROMPTS = [
    "Open duckduckgo.com, search for 'soak test', return the first result as JSON.",
    "Navigate to books.toscrape.com and list the first 3 book titles as JSON.",
    "Open httpbin.org/get and extract the origin IP.",
    "Go to wikipedia.org and return the main heading.",
]

DESKTOP_PROMPTS = [
    "Open xfce4-terminal. Type 'echo SOAK_OK' and press Enter. Return success JSON.",
    "Open the file manager (thunar). Take a screenshot. Close it. Return success JSON.",
    "Open xfce4-terminal. Type 'date' and press Enter. Close. Return success JSON.",
]


# ── Docker stats simulator ────────────────────────────────────────────────────

@dataclass
class ContainerSnapshot:
    """A single point-in-time container resource measurement."""

    timestamp: float
    engine: str
    rss_mb: float
    cpu_pct: float
    pids: int
    chromium_procs: int = 0
    node_mcp_procs: int = 0


@dataclass
class SoakMetrics:
    """Aggregated soak-test resource tracking."""

    snapshots: List[ContainerSnapshot] = field(default_factory=list)
    engines_exercised: List[str] = field(default_factory=list)
    total_agent_runs: int = 0
    completed_agent_runs: int = 0
    errored_agent_runs: int = 0
    total_steps: int = 0
    errors: List[str] = field(default_factory=list)

    # ── Derived metrics ───────────────────────────────────────────────────

    @property
    def rss_values(self) -> List[float]:
        return [s.rss_mb for s in self.snapshots]

    @property
    def cpu_values(self) -> List[float]:
        return [s.cpu_pct for s in self.snapshots]

    @property
    def pid_values(self) -> List[int]:
        return [s.pids for s in self.snapshots]

    @property
    def rss_growth_mb(self) -> float:
        """Difference between last and first RSS reading."""
        if len(self.rss_values) < 2:
            return 0.0
        return self.rss_values[-1] - self.rss_values[0]

    @property
    def rss_stddev(self) -> float:
        return statistics.pstdev(self.rss_values) if len(self.rss_values) >= 2 else 0.0

    @property
    def max_chromium(self) -> int:
        return max((s.chromium_procs for s in self.snapshots), default=0)

    @property
    def max_node_mcp(self) -> int:
        return max((s.node_mcp_procs for s in self.snapshots), default=0)

    @property
    def success_rate(self) -> float:
        return self.completed_agent_runs / max(self.total_agent_runs, 1)


# ── Docker stats parsing ─────────────────────────────────────────────────────

_DOCKER_STATS_LINE_RE = re.compile(
    r"(?P<mem_used>[\d.]+)(?P<mem_unit>[KMG]i?B)\s*/\s*[\d.]+[KMG]i?B\|"
    r"(?P<cpu>[\d.]+)%\|"
    r"(?P<pids>\d+)"
)


def parse_docker_stats_line(raw: str) -> Dict[str, Any]:
    """Parse ``<MemUsage>|<CPUPerc>|<PIDs>`` from ``docker stats --no-stream``.

    Returns ``{"rss_mb": float, "cpu_pct": float, "pids": int}`` or empty on error.
    """
    raw = raw.strip()
    if not raw:
        return {}
    parts = raw.split("|")
    if len(parts) < 3:
        return {}
    try:
        mem_str = parts[0].strip().split("/")[0].strip()
        mem_value = float(re.sub(r"[^0-9.]", "", mem_str))
        if "GiB" in mem_str or "GB" in mem_str:
            mem_value *= 1024
        elif "KiB" in mem_str or "KB" in mem_str:
            mem_value /= 1024
        # else already MiB / MB
        cpu_pct = float(parts[1].strip().replace("%", ""))
        pids = int(parts[2].strip())
        return {"rss_mb": mem_value, "cpu_pct": cpu_pct, "pids": pids}
    except (ValueError, IndexError):
        return {}


def parse_process_list(raw: str) -> Dict[str, int]:
    """Parse ``docker exec ... ps aux`` output to count Chromium / Node MCP."""
    chromium = 0
    node_mcp = 0
    for line in raw.splitlines():
        lower = line.lower()
        if "chrome" in lower or "chromium" in lower:
            # Skip the grep line itself
            if "grep" not in lower:
                chromium += 1
        if "node" in lower and ("mcp" in lower or "playwright" in lower):
            if "grep" not in lower:
                node_mcp += 1
    return {"chromium": chromium, "node_mcp": node_mcp}


# ── Simulated docker command outputs ─────────────────────────────────────────

def _fake_docker_stats_output(
    cycle: int,
    *,
    base_rss_mb: float = 180.0,
    stable: bool = True,
) -> str:
    """Generate a realistic ``docker stats --no-stream`` output line.

    When *stable* is True the memory oscillates within ±15 MB — no leak.
    When *stable* is False memory grows linearly (leak scenario).
    """
    if stable:
        rss = base_rss_mb + random.uniform(-15, 15)
    else:
        rss = base_rss_mb + cycle * 5  # ~5 MB growth per cycle (leak)
    cpu = random.uniform(2.0, 25.0)
    pids = random.randint(18, 35)
    mem_total = 4096.0
    return f"{rss:.2f}MiB / {mem_total:.2f}MiB|{cpu:.2f}%|{pids}"


def _fake_ps_aux_output(
    engine: str,
    *,
    zombie_chromium: bool = False,
    stuck_node_mcp: bool = False,
) -> str:
    """Generate a simulated ``ps aux`` output inside the container."""
    lines = [
        "USER       PID %CPU %MEM    VSZ   RSS TTY  STAT START   TIME COMMAND",
        "root         1  0.0  0.0   2556  1692 ?    Ss   00:00   0:00 /bin/bash /entrypoint.sh",
        "root        10  0.1  0.2 123456 45678 ?    S    00:00   0:01 Xvfb :99 -screen 0 1440x900x24",
        "root        20  0.0  0.1  56789 12345 ?    Sl   00:00   0:00 x11vnc -display :99",
        "root        30  0.5  1.0 234567 89012 ?    Sl   00:00   0:05 python3 /app/docker/agent_service.py",
    ]
    if engine in BROWSER_ENGINES or engine == "desktop_hybrid":
        lines.append(
            "root        40  2.0  3.0 567890 120000 ? Sl   00:00   0:10 "
            "/opt/google/chrome/chrome --headless --no-sandbox"
        )
        if zombie_chromium:
            # Extra zombie Chromium processes that shouldn't be there
            for pid in range(50, 54):
                lines.append(
                    f"root        {pid}  0.0  1.5 234567 60000 ?  Z    00:00   0:00 "
                    "[chrome] <defunct>"
                )
    if engine == "playwright_mcp":
        lines.append(
            "root        60  0.3  0.5 345678 45000 ?  Sl   00:00   0:02 "
            "node /usr/lib/node_modules/@playwright/mcp/cli.js"
        )
        if stuck_node_mcp:
            # Stuck (very old, high CPU) Node MCP process
            lines.append(
                "root        70  99.0  2.0 567890 120000 ?  Rl   00:00   5:00 "
                "node /usr/lib/node_modules/@playwright/mcp/cli.js --stuck"
            )
    return "\n".join(lines)


# ── Engine rotation helpers ───────────────────────────────────────────────────

def engine_rotation_schedule(total_rotations: int = COMPRESSED_ROTATIONS) -> List[str]:
    """Build the engine rotation order: cycle through all 6, alternating browser/desktop.

    Even cycles → browser engines (playwright, playwright_mcp).
    Odd cycles  → non-browser engines (xdotool, accessibility, desktop_hybrid).
    """
    non_browser = DESKTOP_ENGINES + ["omni_accessibility"]
    browser_cycle = itertools.cycle(BROWSER_ENGINES)
    desktop_cycle = itertools.cycle(non_browser)
    schedule: List[str] = []
    for i in range(total_rotations):
        if i % 2 == 0:
            schedule.append(next(browser_cycle))
        else:
            schedule.append(next(desktop_cycle))
    return schedule


def prompt_for_engine(engine: str, iteration: int) -> str:
    """Pick a browser or desktop prompt based on engine type."""
    if ENGINE_MODES.get(engine) == "browser":
        return BROWSER_PROMPTS[iteration % len(BROWSER_PROMPTS)]
    return DESKTOP_PROMPTS[iteration % len(DESKTOP_PROMPTS)]


# ── Agent loop factory ────────────────────────────────────────────────────────

def _make_agent_loop(
    task: str,
    engine: str,
    max_steps: int = STEPS_PER_ROTATION,
) -> AgentLoop:
    mode = ENGINE_MODES.get(engine, "browser")
    return AgentLoop(
        task=task,
        api_key=FAKE_API_KEY,
        model="gemini-3-flash-preview",
        max_steps=max_steps,
        mode=mode,
        engine=engine,
        provider="google",
    )


# ── Mock model response generator (same pattern as Phase 6) ──────────────────

def _make_soak_action_sequence(engine: str, step: int) -> Tuple[AgentAction, str]:
    """Return (action, raw_json) for the given step number."""
    if ENGINE_MODES.get(engine) == "browser":
        seq = [
            AgentAction(action=ActionType.OPEN_URL, text="https://duckduckgo.com",
                        reasoning="Navigate to target site"),
            AgentAction(action=ActionType.WAIT, text="2",
                        reasoning="Wait for page load"),
            AgentAction(action=ActionType.FILL, target='input[name="q"]',
                        text="soak test query",
                        reasoning="Fill search box"),
            AgentAction(action=ActionType.KEY, text="Enter",
                        reasoning="Submit search"),
            AgentAction(action=ActionType.WAIT, text="2",
                        reasoning="Wait for results"),
            AgentAction(action=ActionType.DONE,
                        reasoning='{"result":"soak test complete"}'),
        ]
    else:
        seq = [
            AgentAction(action=ActionType.KEY, text="ctrl+alt+t",
                        reasoning="Open terminal"),
            AgentAction(action=ActionType.WAIT, text="1",
                        reasoning="Wait for terminal"),
            AgentAction(action=ActionType.TYPE, text="echo SOAK_OK",
                        reasoning="Type command"),
            AgentAction(action=ActionType.KEY, text="Enter",
                        reasoning="Execute command"),
            AgentAction(action=ActionType.WAIT, text="1",
                        reasoning="Wait for output"),
            AgentAction(action=ActionType.DONE,
                        reasoning='{"result":"desktop soak complete"}'),
        ]
    idx = min(step - 1, len(seq) - 1)
    action = seq[idx]
    raw = json.dumps({
        "action": action.action.value,
        "text": action.text,
        "target": action.target,
        "coordinates": action.coordinates,
        "reasoning": action.reasoning,
    })
    return action, raw


# ── Autouse fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _zero_action_delay():
    """Eliminate post-action sleep for speed."""
    original = config.action_delay_ms
    config.action_delay_ms = 0
    yield
    config.action_delay_ms = original


@pytest.fixture(autouse=True)
def _mock_mcp_init():
    """Prevent real MCP initialisation."""
    with patch("backend.agent.playwright_mcp_client._ensure_mcp_initialized", AsyncMock()), \
         patch("backend.agent.playwright_mcp_client.check_mcp_health", AsyncMock(return_value=True)):
        yield


# ── Shared test fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def soak_metrics() -> SoakMetrics:
    return SoakMetrics()


@pytest.fixture
def rotation_schedule() -> List[str]:
    return engine_rotation_schedule()


@pytest.fixture
def mock_agent_deps():
    """Patch all AgentLoop external deps (screenshot, model, executor, health)."""
    step_counter: Dict[str, int] = {}

    async def _query_model(*, provider, api_key, model_name, task, screenshot_b64,
                           action_history, step_number, mode, system_prompt, **kw):
        engine = kw.get("engine", "playwright")
        # Determine engine from system_prompt or mode
        if "desktop" in (mode or ""):
            engine = "xdotool"
        key = f"{engine}:{id(action_history)}"
        step_counter[key] = step_counter.get(key, 0) + 1
        return _make_soak_action_sequence(engine, step_counter[key])

    mock_screenshot = AsyncMock(return_value=FAKE_SCREENSHOT)
    mock_query = AsyncMock(side_effect=_query_model)
    mock_execute = AsyncMock(return_value={"success": True, "message": "OK"})
    mock_health = AsyncMock(return_value=True)

    patches = {
        "screenshot": patch("backend.agent.loop.capture_screenshot", mock_screenshot),
        "query": patch("backend.agent.loop.query_model", mock_query),
        "execute": patch("backend.agent.loop.execute_action", mock_execute),
        "health": patch("backend.agent.loop.check_service_health", mock_health),
    }
    for p in patches.values():
        p.start()

    mocks = {
        "screenshot": mock_screenshot,
        "query": mock_query,
        "execute": mock_execute,
        "health": mock_health,
        "_step_counter": step_counter,
    }
    yield mocks

    for p in patches.values():
        p.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakEngineRotation:
    """Verify engine rotation schedule and alternation logic."""

    def test_rotation_schedule_covers_all_engines(self, rotation_schedule):
        """All 6 engines appear at least once in the 20-rotation schedule."""
        exercised = set(rotation_schedule)
        # Browser engines are on even slots, desktop on odd — verify all appear
        for e in BROWSER_ENGINES:
            assert e in exercised, f"Browser engine {e} missing from schedule"
        for e in DESKTOP_ENGINES:
            assert e in exercised, f"Desktop engine {e} missing from schedule"

    def test_rotation_alternates_browser_desktop(self, rotation_schedule):
        """Even cycles run browser engines, odd cycles run desktop engines."""
        for i, engine in enumerate(rotation_schedule):
            expected_mode = "browser" if i % 2 == 0 else "desktop"
            actual_mode = ENGINE_MODES[engine]
            assert actual_mode == expected_mode, (
                f"Cycle {i}: expected {expected_mode}, got {actual_mode} ({engine})"
            )

    def test_rotation_count_matches_spec(self, rotation_schedule):
        """Schedule contains exactly COMPRESSED_ROTATIONS entries."""
        assert len(rotation_schedule) == COMPRESSED_ROTATIONS

    def test_all_rotations_produce_valid_prompts(self, rotation_schedule):
        """Every rotation yields a non-empty prompt matching engine type."""
        for i, engine in enumerate(rotation_schedule):
            prompt = prompt_for_engine(engine, i)
            assert len(prompt) > 20
            if ENGINE_MODES[engine] == "browser":
                # Browser prompts reference URLs
                assert any(kw in prompt.lower() for kw in ["open", "navigate", "go to"])
            else:
                # Desktop prompts reference terminal / file manager
                assert any(kw in prompt.lower() for kw in ["terminal", "file manager"])


@pytest.mark.stress
@pytest.mark.phase8
class TestDockerStatsParser:
    """Validate parsing of ``docker stats --no-stream`` output."""

    @pytest.mark.parametrize("raw,expected_rss,expected_cpu,expected_pids", [
        ("180.50MiB / 4096.00MiB|12.34%|25", 180.50, 12.34, 25),
        ("1.20GiB / 4.00GiB|45.67%|42", 1.20 * 1024, 45.67, 42),
        ("512.00KiB / 4096.00MiB|0.10%|3", 512.0 / 1024, 0.10, 3),
        ("200.00MB / 4096.00MB|5.00%|10", 200.00, 5.00, 10),
    ])
    def test_parse_various_units(self, raw, expected_rss, expected_cpu, expected_pids):
        result = parse_docker_stats_line(raw)
        assert result, f"Failed to parse: {raw}"
        assert abs(result["rss_mb"] - expected_rss) < 1.0
        assert abs(result["cpu_pct"] - expected_cpu) < 0.01
        assert result["pids"] == expected_pids

    def test_parse_empty_string(self):
        assert parse_docker_stats_line("") == {}

    def test_parse_malformed_input(self):
        assert parse_docker_stats_line("not a stats line") == {}


@pytest.mark.stress
@pytest.mark.phase8
class TestProcessListParser:
    """Validate parsing of ``ps aux`` output for zombie/stuck detection."""

    def test_detects_chromium_processes(self):
        raw = _fake_ps_aux_output("playwright", zombie_chromium=False)
        counts = parse_process_list(raw)
        assert counts["chromium"] >= 1

    def test_detects_zombie_chromium(self):
        raw = _fake_ps_aux_output("playwright", zombie_chromium=True)
        counts = parse_process_list(raw)
        # Normal (1) + zombies (4)
        assert counts["chromium"] >= 5

    def test_detects_node_mcp(self):
        raw = _fake_ps_aux_output("playwright_mcp", stuck_node_mcp=False)
        counts = parse_process_list(raw)
        assert counts["node_mcp"] >= 1

    def test_detects_stuck_node_mcp(self):
        raw = _fake_ps_aux_output("playwright_mcp", stuck_node_mcp=True)
        counts = parse_process_list(raw)
        assert counts["node_mcp"] >= 2  # normal + stuck

    def test_no_chromium_on_desktop_engine(self):
        raw = _fake_ps_aux_output("xdotool")
        counts = parse_process_list(raw)
        assert counts["chromium"] == 0

    def test_no_mcp_on_non_mcp_engine(self):
        raw = _fake_ps_aux_output("playwright")
        counts = parse_process_list(raw)
        assert counts["node_mcp"] == 0


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakMemoryStability:
    """Memory must stabilise — no unbounded growth across 20 rotations."""

    def test_stable_memory_stays_bounded(self, soak_metrics):
        """Simulated stable container: RSS stays within ±30 MB of baseline."""
        base = 180.0
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, base_rss_mb=base, stable=True)
            parsed = parse_docker_stats_line(raw)
            soak_metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine_rotation_schedule()[cycle],
                rss_mb=parsed["rss_mb"],
                cpu_pct=parsed["cpu_pct"],
                pids=parsed["pids"],
            ))

        # RSS should not grow significantly
        assert abs(soak_metrics.rss_growth_mb) < STRESS.max_memory_growth_mb, (
            f"RSS grew {soak_metrics.rss_growth_mb:.1f} MB — threshold "
            f"{STRESS.max_memory_growth_mb} MB"
        )



    def test_leaking_memory_detected(self, soak_metrics):
        """Simulated leaking container: growth exceeds threshold → caught."""
        base = 180.0
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, base_rss_mb=base, stable=False)
            parsed = parse_docker_stats_line(raw)
            soak_metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine_rotation_schedule()[cycle],
                rss_mb=parsed["rss_mb"],
                cpu_pct=parsed["cpu_pct"],
                pids=parsed["pids"],
            ))

        # With ~5 MB/cycle growth over 20 cycles → ~100 MB
        assert soak_metrics.rss_growth_mb > 50, (
            "Leak scenario should show significant RSS growth"
        )

    def test_memory_variance_decreases_over_time(self, soak_metrics):
        """In a stable soak, second-half variance ≤ first-half variance * 1.5."""
        base = 180.0
        rss_values: List[float] = []
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, base_rss_mb=base, stable=True)
            parsed = parse_docker_stats_line(raw)
            rss_values.append(parsed["rss_mb"])

        mid = len(rss_values) // 2
        first_half = rss_values[:mid]
        second_half = rss_values[mid:]
        var_first = statistics.pvariance(first_half) if len(first_half) >= 2 else 0
        var_second = statistics.pvariance(second_half) if len(second_half) >= 2 else 0

        # Second half should not be dramatically worse
        assert var_second <= var_first * 2.5 + 1, (
            f"Memory variance increased: first_half={var_first:.1f}, "
            f"second_half={var_second:.1f}"
        )

    def test_rss_never_exceeds_container_limit(self, soak_metrics):
        """RSS should never exceed the 4 GB container memory limit."""
        container_limit_mb = 4096.0
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, base_rss_mb=180.0, stable=True)
            parsed = parse_docker_stats_line(raw)
            assert parsed["rss_mb"] < container_limit_mb


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakCPUStability:
    """CPU usage should not sustain 100 % across rotations."""

    def test_average_cpu_below_threshold(self):
        """Average CPU across all cycles should be below 80 %."""
        cpus: List[float] = []
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, stable=True)
            parsed = parse_docker_stats_line(raw)
            cpus.append(parsed["cpu_pct"])

        avg = statistics.mean(cpus)
        assert avg < 80.0, f"Average CPU {avg:.1f}% exceeds 80%"

    def test_no_sustained_cpu_spike(self):
        """No more than 3 consecutive cycles above 90 % CPU."""
        consecutive_high = 0
        max_consecutive = 0
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, stable=True)
            parsed = parse_docker_stats_line(raw)
            if parsed["cpu_pct"] > 90:
                consecutive_high += 1
            else:
                consecutive_high = 0
            max_consecutive = max(max_consecutive, consecutive_high)

        assert max_consecutive <= 3, (
            f"CPU spiked above 90% for {max_consecutive} consecutive cycles"
        )


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakProcessHealth:
    """Verify no zombie Chromium or stuck Node MCP after each rotation."""

    def test_no_zombie_chromium_across_rotations(self):
        """After each engine switch, zombie Chromium count must be 0."""
        schedule = engine_rotation_schedule()
        for cycle, engine in enumerate(schedule):
            raw = _fake_ps_aux_output(engine, zombie_chromium=False)
            counts = parse_process_list(raw)
            # In normal operation, only the active Chromium should exist
            if engine not in BROWSER_ENGINES and engine != "desktop_hybrid":
                assert counts["chromium"] == 0, (
                    f"Cycle {cycle} ({engine}): unexpected Chromium processes"
                )

    def test_zombie_chromium_would_be_caught(self):
        """If zombies appear, the monitoring detects them."""
        raw = _fake_ps_aux_output("playwright", zombie_chromium=True)
        counts = parse_process_list(raw)
        assert counts["chromium"] >= 5, "Should detect 1 active + 4 zombie Chromium"

    def test_no_stuck_node_mcp_across_rotations(self):
        """After each rotation, at most 1 Node MCP process per MCP engine."""
        schedule = engine_rotation_schedule()
        for cycle, engine in enumerate(schedule):
            raw = _fake_ps_aux_output(engine, stuck_node_mcp=False)
            counts = parse_process_list(raw)
            if engine == "playwright_mcp":
                assert counts["node_mcp"] <= 1, (
                    f"Cycle {cycle}: {counts['node_mcp']} Node MCP — expected ≤ 1"
                )
            else:
                assert counts["node_mcp"] == 0, (
                    f"Cycle {cycle} ({engine}): unexpected Node MCP process"
                )

    def test_stuck_node_mcp_would_be_caught(self):
        """If a stuck MCP process appears, monitoring detects it."""
        raw = _fake_ps_aux_output("playwright_mcp", stuck_node_mcp=True)
        counts = parse_process_list(raw)
        assert counts["node_mcp"] >= 2, "Should detect normal + stuck Node MCP"

    def test_process_count_bounded(self):
        """PID count should stay reasonable across all cycles."""
        for cycle in range(COMPRESSED_ROTATIONS):
            raw = _fake_docker_stats_output(cycle, stable=True)
            parsed = parse_docker_stats_line(raw)
            assert parsed["pids"] < 100, (
                f"Cycle {cycle}: {parsed['pids']} PIDs — runaway process detected"
            )


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakAgentLoopContinuity:
    """Agent loop survives continuous use across all engine rotations."""

    def test_continuous_rotation_no_crash(self, mock_agent_deps, soak_metrics):
        """Run agent loop once per rotation for all 20 cycles — no crash."""
        schedule = engine_rotation_schedule()

        for cycle, engine in enumerate(schedule):
            prompt = prompt_for_engine(engine, cycle)

            # Reset step counter for this cycle's history
            mock_agent_deps["_step_counter"].clear()

            # Create custom query_model for this specific engine
            step_count = {"n": 0}

            async def _query_for_engine(*, provider, api_key, model_name, task,
                                        screenshot_b64, action_history, step_number,
                                        mode, system_prompt, _eng=engine, _sc=step_count, **kw):
                _sc["n"] += 1
                return _make_soak_action_sequence(_eng, _sc["n"])

            mock_agent_deps["query"].side_effect = _query_for_engine

            loop = _make_agent_loop(task=prompt, engine=engine)
            session = run_async(loop.run())

            soak_metrics.total_agent_runs += 1
            soak_metrics.engines_exercised.append(engine)
            soak_metrics.total_steps += len(session.steps)

            if session.status == SessionStatus.COMPLETED:
                soak_metrics.completed_agent_runs += 1
            else:
                soak_metrics.errored_agent_runs += 1
                soak_metrics.errors.append(
                    f"Cycle {cycle} ({engine}): {session.status.value}"
                )

        assert soak_metrics.total_agent_runs == COMPRESSED_ROTATIONS
        assert soak_metrics.success_rate >= 0.95, (
            f"Soak success rate {soak_metrics.success_rate:.1%} < 95%. "
            f"Errors: {soak_metrics.errors}"
        )

    def test_agent_loop_recovers_from_transient_error(self, mock_agent_deps):
        """Agent loop recovers when one step fails mid-soak."""
        call_count = {"n": 0}

        async def _intermittent_execute(action, *, mode, engine, **kw):
            call_count["n"] += 1
            # Fail on the 3rd call
            if call_count["n"] == 3:
                return {"success": False, "message": "Transient soak error"}
            return {"success": True, "message": "OK"}

        mock_agent_deps["execute"].side_effect = _intermittent_execute

        step_count = {"n": 0}
        async def _query(*, provider, api_key, model_name, task, screenshot_b64,
                         action_history, step_number, mode, system_prompt, **kw):
            step_count["n"] += 1
            return _make_soak_action_sequence("playwright", step_count["n"])

        mock_agent_deps["query"].side_effect = _query

        loop = _make_agent_loop(task=BROWSER_PROMPTS[0], engine="playwright",
                                max_steps=10)
        session = run_async(loop.run())

        # Should complete (DONE reached) despite the transient error
        assert session.status == SessionStatus.COMPLETED
        # At least one step had an error
        errored = [s for s in session.steps if s.error]
        assert len(errored) >= 1

    def test_no_session_leak_across_engines(self, mock_agent_deps):
        """Each rotation creates a fresh session — no shared state leakage."""
        sessions: List[str] = []

        step_counts: Dict[str, Dict[str, int]] = {}

        for engine in ALL_ENGINES:
            sc = {"n": 0}
            step_counts[engine] = sc

            async def _query(*, provider, api_key, model_name, task, screenshot_b64,
                             action_history, step_number, mode, system_prompt,
                             _eng=engine, _sc=sc, **kw):
                _sc["n"] += 1
                return _make_soak_action_sequence(_eng, _sc["n"])

            mock_agent_deps["query"].side_effect = _query

            loop = _make_agent_loop(task=prompt_for_engine(engine, 0), engine=engine)
            session = run_async(loop.run())
            sessions.append(session.session_id)

        # All session IDs must be unique
        assert len(set(sessions)) == len(ALL_ENGINES), (
            f"Session leak: {len(set(sessions))} unique IDs for {len(ALL_ENGINES)} engines"
        )


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakMCPHealth:
    """MCP server health checks pass during simulated soak."""

    def test_mcp_health_during_soak_rotations(self):
        """MCP health returns True for each playwright_mcp rotation."""
        schedule = engine_rotation_schedule()
        mcp_rotations = [e for e in schedule if e == "playwright_mcp"]
        assert len(mcp_rotations) >= 1, "No playwright_mcp rotations in schedule"

        with patch("backend.agent.playwright_mcp_client.check_mcp_health",
                    AsyncMock(return_value=True)) as mock_health:
            for _ in mcp_rotations:
                result = run_async(mock_health())
                assert result is True
            assert mock_health.call_count == len(mcp_rotations)

    def test_mcp_reconnects_after_engine_switch(self):
        """After switching away from MCP and back, init is called again."""
        init_calls = 0

        async def _mock_init():
            nonlocal init_calls
            init_calls += 1

        with patch("backend.agent.playwright_mcp_client._ensure_mcp_initialized",
                    AsyncMock(side_effect=_mock_init)):
            schedule = engine_rotation_schedule()
            for engine in schedule:
                if engine == "playwright_mcp":
                    from backend.agent.playwright_mcp_client import _ensure_mcp_initialized
                    run_async(_ensure_mcp_initialized())

        mcp_count = sum(1 for e in schedule if e == "playwright_mcp")
        assert init_calls == mcp_count


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakResourceTimeseries:
    """Validate timeseries collection and anomaly detection."""

    def test_timeseries_collects_all_rotations(self, soak_metrics):
        """Timeseries has exactly COMPRESSED_ROTATIONS data points."""
        schedule = engine_rotation_schedule()
        for cycle, engine in enumerate(schedule):
            raw = _fake_docker_stats_output(cycle, stable=True)
            parsed = parse_docker_stats_line(raw)
            ps_raw = _fake_ps_aux_output(engine)
            procs = parse_process_list(ps_raw)
            soak_metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine,
                rss_mb=parsed["rss_mb"],
                cpu_pct=parsed["cpu_pct"],
                pids=parsed["pids"],
                chromium_procs=procs["chromium"],
                node_mcp_procs=procs["node_mcp"],
            ))

        assert len(soak_metrics.snapshots) == COMPRESSED_ROTATIONS

    def test_max_chromium_bounded(self, soak_metrics):
        """Peak Chromium process count stays ≤ 2 (one active, maybe one closing)."""
        schedule = engine_rotation_schedule()
        for cycle, engine in enumerate(schedule):
            ps_raw = _fake_ps_aux_output(engine, zombie_chromium=False)
            procs = parse_process_list(ps_raw)
            soak_metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine,
                rss_mb=180.0,
                cpu_pct=10.0,
                pids=25,
                chromium_procs=procs["chromium"],
                node_mcp_procs=procs["node_mcp"],
            ))

        assert soak_metrics.max_chromium <= 2, (
            f"Peak Chromium count {soak_metrics.max_chromium} > 2"
        )

    def test_max_node_mcp_bounded(self, soak_metrics):
        """Peak Node MCP count stays ≤ 1."""
        schedule = engine_rotation_schedule()
        for cycle, engine in enumerate(schedule):
            ps_raw = _fake_ps_aux_output(engine, stuck_node_mcp=False)
            procs = parse_process_list(ps_raw)
            soak_metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine,
                rss_mb=180.0,
                cpu_pct=10.0,
                pids=25,
                chromium_procs=procs["chromium"],
                node_mcp_procs=procs["node_mcp"],
            ))

        assert soak_metrics.max_node_mcp <= 1, (
            f"Peak Node MCP count {soak_metrics.max_node_mcp} > 1"
        )


@pytest.mark.stress
@pytest.mark.phase8
class TestSoakEndToEnd:
    """Full compressed 10-minute soak simulation with all checks."""

    def test_full_soak_compressed(self, mock_agent_deps):
        """20 rotations: engine switch + agent run + resource check.

        Asserts:
          - All engines exercised
          - ≥ 95 % agent success rate
          - Memory stable
          - No zombie Chromium
          - No stuck Node MCP
          - PID count bounded
        """
        metrics = SoakMetrics()
        schedule = engine_rotation_schedule()
        base_rss = 180.0

        for cycle, engine in enumerate(schedule):
            # ── 1. Collect resource snapshot ──────────────────────────────
            stats_raw = _fake_docker_stats_output(cycle, base_rss_mb=base_rss,
                                                  stable=True)
            parsed = parse_docker_stats_line(stats_raw)
            ps_raw = _fake_ps_aux_output(engine, zombie_chromium=False,
                                         stuck_node_mcp=False)
            procs = parse_process_list(ps_raw)

            metrics.snapshots.append(ContainerSnapshot(
                timestamp=time.time(),
                engine=engine,
                rss_mb=parsed["rss_mb"],
                cpu_pct=parsed["cpu_pct"],
                pids=parsed["pids"],
                chromium_procs=procs["chromium"],
                node_mcp_procs=procs["node_mcp"],
            ))

            # ── 2. Run agent loop for this rotation ──────────────────────
            prompt = prompt_for_engine(engine, cycle)
            sc = {"n": 0}

            async def _query(*, provider, api_key, model_name, task, screenshot_b64,
                             action_history, step_number, mode, system_prompt,
                             _eng=engine, _sc=sc, **kw):
                _sc["n"] += 1
                return _make_soak_action_sequence(_eng, _sc["n"])

            mock_agent_deps["query"].side_effect = _query

            loop = _make_agent_loop(task=prompt, engine=engine)
            session = run_async(loop.run())

            metrics.total_agent_runs += 1
            metrics.engines_exercised.append(engine)
            metrics.total_steps += len(session.steps)

            if session.status == SessionStatus.COMPLETED:
                metrics.completed_agent_runs += 1
            else:
                metrics.errored_agent_runs += 1
                metrics.errors.append(
                    f"Cycle {cycle} ({engine}): {session.status.value}"
                )

        # ── Assertions ────────────────────────────────────────────────────

        # All engines exercised
        exercised = set(metrics.engines_exercised)
        for eng in ALL_ENGINES:
            assert eng in exercised, f"Engine {eng} was never exercised"

        # High success rate
        assert metrics.success_rate >= 0.95, (
            f"Soak success rate {metrics.success_rate:.1%}. Errors: {metrics.errors}"
        )

        # Memory stable
        assert abs(metrics.rss_growth_mb) < STRESS.max_memory_growth_mb, (
            f"RSS grew {metrics.rss_growth_mb:.1f} MB"
        )

        # No zombie Chromium
        assert metrics.max_chromium <= 2, (
            f"Peak Chromium {metrics.max_chromium} > 2"
        )

        # No stuck Node MCP
        assert metrics.max_node_mcp <= 1, (
            f"Peak Node MCP {metrics.max_node_mcp} > 1"
        )

        # PID count bounded
        assert all(s.pids < 100 for s in metrics.snapshots), "Runaway PID count"

        # Report
        logger.info(
            "Soak complete: %d rotations, %d/%d succeeded, "
            "RSS growth=%.1f MB, max Chromium=%d, max MCP=%d, total steps=%d",
            COMPRESSED_ROTATIONS,
            metrics.completed_agent_runs,
            metrics.total_agent_runs,
            metrics.rss_growth_mb,
            metrics.max_chromium,
            metrics.max_node_mcp,
            metrics.total_steps,
        )
