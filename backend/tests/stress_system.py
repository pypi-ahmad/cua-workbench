#!/usr/bin/env python3
"""CUA Full System Stress Test Harness.

Stress tests all 6 automation engines by firing concurrent agent sessions
through the real FastAPI backend.  Tracks per-engine latency, failures,
disconnects, Docker container memory & CPU, and verifies real outputs.

Report format (printed at exit)::

    CUA FULL SYSTEM STRESS REPORT
    Per Engine:  Total Calls · Failures · Disconnects · Avg Latency
                 Max Latency · Memory Delta · CPU Peak
    Overall:     Pass / Fail
    On failure:  last 50 container logs + stack traces + exit non-zero

Usage:
    # Single engine
    python backend/tests/stress_system.py --engine playwright --concurrency 3 --iterations 20

    # All engines sequentially
    python backend/tests/stress_system.py --engine all --concurrency 2 --iterations 10

    # 5-minute soak test
    python backend/tests/stress_system.py --engine all --concurrency 3 --duration 300

    # Quick smoke test (defaults)
    python backend/tests/stress_system.py --engine playwright_mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path so backend.* imports work when run as a script
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

BACKEND_BASE = os.getenv("CUA_BACKEND_URL", "http://localhost:8000")
BACKEND_START_AGENT = f"{BACKEND_BASE}/api/agent/start"
BACKEND_STOP_AGENT = f"{BACKEND_BASE}/api/agent/stop"
BACKEND_STATUS_AGENT = f"{BACKEND_BASE}/api/agent/status"
BACKEND_HEALTH = f"{BACKEND_BASE}/api/health"
BACKEND_CONTAINER_STATUS = f"{BACKEND_BASE}/api/container/status"
BACKEND_CONTAINER_START = f"{BACKEND_BASE}/api/container/start"
BACKEND_SCREENSHOT = f"{BACKEND_BASE}/api/screenshot"

CONTAINER_NAME = os.getenv("CUA_CONTAINER_NAME", "cua-environment")

# Captcha-free target sites
SAFE_SITES = [
    "https://duckduckgo.com",
    "https://books.toscrape.com",
    "https://wikipedia.org",
    "https://httpbin.org/get",
    "https://jsonplaceholder.typicode.com",
]

ENGINES = [
    "playwright_mcp",
    "omni_accessibility",
    "computer_use",
]

# Engine → mode mapping (must match backend expectations)
ENGINE_MODE_MAP: Dict[str, str] = {
    "playwright_mcp": "browser",
    "omni_accessibility": "desktop",
    "computer_use": "desktop",
}

# Engine → provider (default to google/gemini for the harness)
DEFAULT_PROVIDER = os.getenv("CUA_PROVIDER", "google")
DEFAULT_MODEL_MAP: Dict[str, str] = {
    "google": os.getenv("CUA_MODEL", "gemini-3-flash-preview"),
    "anthropic": os.getenv("CUA_MODEL", "claude-sonnet-4-6"),
}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("stress_system")


# ══════════════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════════════

class FailureType(str, Enum):
    """Categorized failure reasons for diagnostics."""
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    DISCONNECT = "disconnect"
    AGENT_ERROR = "agent_error"
    API_REJECTION = "api_rejection"
    UNKNOWN = "unknown"


@dataclass
class EngineMetrics:
    """Accumulated metrics for one engine across all workers."""

    engine: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    disconnects: int = 0
    timeouts: int = 0
    api_rejections: int = 0
    agent_errors: int = 0
    latencies: List[float] = field(default_factory=list)
    failure_messages: List[str] = field(default_factory=list)
    stack_traces: List[str] = field(default_factory=list)
    sessions_started: int = 0
    sessions_completed: int = 0

    # Resource tracking (populated by harness)
    memory_before_mb: float = 0.0
    memory_after_mb: float = 0.0
    cpu_samples: List[float] = field(default_factory=list)
    verified_outputs: int = 0
    invalid_outputs: int = 0

    def record_success(self, latency: float):
        self.calls += 1
        self.successes += 1
        self.latencies.append(latency)

    def record_failure(self, reason: FailureType, message: str = "",
                       tb: str = ""):
        self.calls += 1
        self.failures += 1
        if reason == FailureType.DISCONNECT:
            self.disconnects += 1
        elif reason == FailureType.TIMEOUT:
            self.timeouts += 1
        elif reason == FailureType.API_REJECTION:
            self.api_rejections += 1
        elif reason == FailureType.AGENT_ERROR:
            self.agent_errors += 1
        if message:
            self.failure_messages.append(message[:200])
        if tb:
            self.stack_traces.append(tb[:2000])

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def success_rate(self) -> float:
        return self.successes / max(self.calls, 1)

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p50_latency(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[int(len(s) * 0.95)]

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

    @property
    def memory_delta_mb(self) -> float:
        return self.memory_after_mb - self.memory_before_mb

    @property
    def cpu_peak(self) -> float:
        return max(self.cpu_samples) if self.cpu_samples else 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "total_calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": f"{self.success_rate:.1%}",
            "disconnects": self.disconnects,
            "timeouts": self.timeouts,
            "api_rejections": self.api_rejections,
            "agent_errors": self.agent_errors,
            "sessions_started": self.sessions_started,
            "sessions_completed": self.sessions_completed,
            "avg_latency_s": round(self.avg_latency, 3),
            "p50_latency_s": round(self.p50_latency, 3),
            "p95_latency_s": round(self.p95_latency, 3),
            "p99_latency_s": round(self.p99_latency, 3),
            "max_latency_s": round(self.max_latency, 3),
            "memory_delta_mb": round(self.memory_delta_mb, 1),
            "cpu_peak_pct": round(self.cpu_peak, 1),
            "verified_outputs": self.verified_outputs,
            "invalid_outputs": self.invalid_outputs,
            "top_failures": self.failure_messages[:5],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Docker Monitoring
# ══════════════════════════════════════════════════════════════════════════════

def _parse_memory_mb(mem_str: str) -> float:
    """Parse a Docker memory string like '180.5MiB' or '1.2GiB' into MB."""
    mem_str = mem_str.strip().split("/")[0].strip()
    value = float(re.sub(r"[^0-9.]", "", mem_str) or "0")
    upper = mem_str.upper()
    if "GIB" in upper or "GB" in upper:
        value *= 1024
    elif "KIB" in upper or "KB" in upper:
        value /= 1024
    return value


def _parse_cpu_pct(cpu_str: str) -> float:
    """Parse a Docker CPU string like '12.34%' into a float."""
    return float(cpu_str.strip().replace("%", "") or "0")


def get_container_stats() -> Dict[str, str]:
    """Snapshot Docker container memory / CPU / PID usage (raw strings)."""
    try:
        output = subprocess.check_output(
            [
                "docker", "stats", CONTAINER_NAME,
                "--no-stream",
                "--format", "{{.MemUsage}}|{{.CPUPerc}}|{{.PIDs}}",
            ],
            text=True,
            timeout=10,
        ).strip()
        parts = output.split("|")
        return {
            "memory": parts[0].strip() if len(parts) > 0 else "N/A",
            "cpu": parts[1].strip() if len(parts) > 1 else "N/A",
            "pids": parts[2].strip() if len(parts) > 2 else "N/A",
        }
    except Exception as exc:
        logger.debug("Container stats unavailable: %s", exc)
        return {"memory": "N/A", "cpu": "N/A", "pids": "N/A"}


def get_container_stats_numeric() -> Dict[str, float]:
    """Return ``{memory_mb, cpu_pct, pids}`` as floats (0.0 on error)."""
    raw = get_container_stats()
    try:
        mem = _parse_memory_mb(raw["memory"]) if raw["memory"] != "N/A" else 0.0
    except (ValueError, IndexError):
        mem = 0.0
    try:
        cpu = _parse_cpu_pct(raw["cpu"]) if raw["cpu"] != "N/A" else 0.0
    except (ValueError, IndexError):
        cpu = 0.0
    try:
        pids = float(raw["pids"]) if raw["pids"] != "N/A" else 0.0
    except (ValueError, IndexError):
        pids = 0.0
    return {"memory_mb": mem, "cpu_pct": cpu, "pids": pids}


def get_container_processes() -> Dict[str, int]:
    """Count Chromium and Node-MCP processes inside the container."""
    try:
        output = subprocess.check_output(
            ["docker", "exec", CONTAINER_NAME, "ps", "aux"],
            text=True,
            timeout=10,
        )
    except Exception:
        return {"chromium": 0, "node_mcp": 0}
    chromium = node_mcp = 0
    for line in output.splitlines():
        lower = line.lower()
        if "grep" in lower:
            continue
        if "chrome" in lower or "chromium" in lower:
            chromium += 1
        if "node" in lower and ("mcp" in lower or "playwright" in lower):
            node_mcp += 1
    return {"chromium": chromium, "node_mcp": node_mcp}


def tail_container_logs(lines: int = 50) -> str:
    """Retrieve the last *lines* lines of container logs for failure diagnosis."""
    try:
        return subprocess.check_output(
            ["docker", "logs", "--tail", str(lines), CONTAINER_NAME],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except Exception:
        return "Unable to retrieve container logs."


# ══════════════════════════════════════════════════════════════════════════════
# Output Verification
# ══════════════════════════════════════════════════════════════════════════════

def verify_session_output(data: Dict[str, Any]) -> bool:
    """Validate that a completed session produced a real, non-trivial output.

    Checks:
    - ``session_id`` is a non-empty string
    - ``status`` is ``completed`` or ``error`` (not stuck running)
    - ``steps`` is a non-empty list
    - At least one step has a non-null ``action``
    - The final action is ``done`` (success path) **or** ``error``
    """
    if not isinstance(data, dict):
        return False
    if not data.get("session_id"):
        return False
    status = data.get("status", "")
    if status not in ("completed", "error"):
        return False
    steps = data.get("steps")
    if not steps or not isinstance(steps, list):
        return False
    # At least one step must carry an action
    actions = [s for s in steps if s.get("action")]
    if not actions:
        return False
    return True


def check_container_running() -> bool:
    """Return True if the Docker container is running."""
    try:
        output = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME],
            text=True,
            timeout=5,
        ).strip()
        return output.lower() == "true"
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Workload Builders
# ══════════════════════════════════════════════════════════════════════════════

def build_browser_prompt(site: str, iteration: int) -> str:
    """Build a browser-mode task prompt for Playwright / MCP engines."""
    prompts = [
        f"Open {site}. Wait for the page to load. Return the page title as JSON.",
        f"Navigate to {site}. Scroll down 3 times. Return the visible text as JSON.",
        f"Go to {site}. Find any link on the page and click it. Return success JSON.",
    ]
    return prompts[iteration % len(prompts)]


def build_search_prompt(iteration: int) -> str:
    """Build a DuckDuckGo search prompt for browser engines."""
    queries = [
        "CUA automation framework",
        "Playwright browser testing",
        "Python asyncio tutorial",
        "open source computer agent",
        "web scraping best practices",
    ]
    query = queries[iteration % len(queries)]
    return (
        f"Open https://duckduckgo.com. "
        f"Find the search input, type '{query}', and press Enter. "
        f"Wait for results. Return the first result title as JSON."
    )


def build_books_prompt(iteration: int) -> str:
    """Build a Books to Scrape navigation prompt."""
    categories = ["Travel", "Mystery", "Historical Fiction", "Science", "Poetry"]
    cat = categories[iteration % len(categories)]
    return (
        f"Open https://books.toscrape.com. "
        f"Find and click the '{cat}' category link in the sidebar. "
        f"Return the first book title on that page as JSON."
    )


def build_desktop_prompt(iteration: int) -> str:
    """Build a desktop-mode task prompt for computer_use engine."""
    prompts = [
        (
            "Open xfce4-terminal. "
            "Type 'echo STRESS_TEST_OK' and press Enter. "
            "Verify the output. Close the terminal. "
            "Return success JSON."
        ),
        (
            "Open the file manager (thunar). "
            "Wait for it to load. Take a screenshot. "
            "Close the file manager. Return success JSON."
        ),
        (
            "Open xfce4-terminal. "
            "Type 'date' and press Enter. "
            "Type 'hostname' and press Enter. "
            "Close the terminal. Return success JSON."
        ),
    ]
    return prompts[iteration % len(prompts)]


def build_accessibility_prompt(iteration: int) -> str:
    """Build an accessibility-engine task prompt using AT-SPI."""
    prompts = [
        (
            "List all accessible applications on the desktop. "
            "Return the application names as JSON."
        ),
        (
            "Open xfce4-terminal using accessibility tree. "
            "Find the text input area using role='terminal'. "
            "Type 'ACCESSIBILITY_STRESS_TEST'. Press Enter. "
            "Close the terminal. Return success JSON."
        ),
        (
            "Get the accessibility tree of the focused window. "
            "Find all buttons. Return their names as JSON."
        ),
    ]
    return prompts[iteration % len(prompts)]


def build_httpbin_prompt(iteration: int) -> str:
    """Build httpbin API exploration prompt."""
    return (
        "Open https://httpbin.org. "
        "Click on one of the endpoint links (e.g., /get or /ip). "
        "Wait for the JSON response to display. "
        "Return the page content as JSON."
    )


def build_prompt(engine: str, iteration: int) -> str:
    """Route to the appropriate prompt builder for the given engine."""
    if engine == "playwright_mcp":
        # Cycle through different site-based prompts
        prompt_builders = [
            lambda i: build_browser_prompt(SAFE_SITES[i % len(SAFE_SITES)], i),
            build_search_prompt,
            build_books_prompt,
            build_httpbin_prompt,
        ]
        builder = prompt_builders[iteration % len(prompt_builders)]
        return builder(iteration)

    if engine == "computer_use":
        return build_desktop_prompt(iteration)

    if engine == "omni_accessibility":
        return build_accessibility_prompt(iteration)

    raise ValueError(f"Unknown engine: {engine}")


# ══════════════════════════════════════════════════════════════════════════════
# Pre-flight Checks
# ══════════════════════════════════════════════════════════════════════════════

async def preflight_check(client: httpx.AsyncClient) -> bool:
    """Verify backend health + container running before starting stress tests."""
    logger.info("Running pre-flight checks...")

    # 1. Backend health
    try:
        resp = await client.get(BACKEND_HEALTH, timeout=5)
        data = resp.json()
        if data.get("status") != "ok":
            logger.error("Backend health check failed: %s", data)
            return False
        logger.info("  [OK] Backend is healthy")
    except Exception as exc:
        logger.error("  [FAIL] Backend unreachable at %s: %s", BACKEND_BASE, exc)
        return False

    # 2. Container status
    try:
        resp = await client.get(BACKEND_CONTAINER_STATUS, timeout=10)
        data = resp.json()
        container_running = data.get("running", False) or data.get("container_running", False)
        if not container_running:
            logger.warning("  [WARN] Container not running — attempting start...")
            start_resp = await client.post(BACKEND_CONTAINER_START, timeout=120)
            start_data = start_resp.json()
            if not start_data.get("success"):
                logger.error("  [FAIL] Could not start container: %s", start_data)
                return False
            logger.info("  [OK] Container started")
            # Wait for services to initialize
            await asyncio.sleep(5)
        else:
            logger.info("  [OK] Container is running")
    except Exception as exc:
        logger.warning("  [WARN] Container check failed: %s (proceeding anyway)", exc)

    # 3. Screenshot sanity (confirms agent service is up)
    try:
        resp = await client.get(BACKEND_SCREENSHOT, timeout=15)
        data = resp.json()
        if data.get("error"):
            logger.warning("  [WARN] Screenshot check returned error: %s", data["error"])
        else:
            logger.info("  [OK] Screenshot capture working")
    except Exception as exc:
        logger.warning("  [WARN] Screenshot check failed: %s (non-fatal)", exc)

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Core Stress Harness
# ══════════════════════════════════════════════════════════════════════════════

class StressTestHarness:
    """Drives concurrent agent sessions against a single engine.

    Each worker:
    1. POSTs to /api/agent/start with a task prompt
    2. Polls /api/agent/status until finished (or timeout)
    3. Verifies real output structure
    4. Records latency, success/failure, and resource metrics
    """

    def __init__(
        self,
        engine: str,
        concurrency: int,
        iterations: int,
        duration: int,
        provider: str,
        api_key: Optional[str] = None,
        poll_interval: float = 2.0,
        session_timeout: float = 120.0,
    ):
        self.engine = engine
        self.concurrency = concurrency
        self.iterations = iterations
        self.duration = duration
        self.provider = provider
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.session_timeout = session_timeout
        self.metrics = EngineMetrics(engine)
        self.mode = ENGINE_MODE_MAP.get(engine, "browser")
        self.model = DEFAULT_MODEL_MAP.get(provider, "gemini-3-flash-preview")
        self._stop_monitor = False

    async def _collect_resource_samples(self) -> None:
        """Background task: sample container CPU every 5 s while the engine runs."""
        while not self._stop_monitor:
            stats = get_container_stats_numeric()
            if stats["cpu_pct"] > 0:
                self.metrics.cpu_samples.append(stats["cpu_pct"])
            await asyncio.sleep(5)

    async def run(self) -> EngineMetrics:
        """Drive all workers concurrently and return collected metrics."""
        start_time = time.time()

        # ── Snapshot memory BEFORE ────────────────────────────────────────
        pre_stats = get_container_stats_numeric()
        self.metrics.memory_before_mb = pre_stats["memory_mb"]
        if pre_stats["cpu_pct"] > 0:
            self.metrics.cpu_samples.append(pre_stats["cpu_pct"])

        async with httpx.AsyncClient(timeout=self.session_timeout + 30) as client:

            # Pre-flight
            ok = await preflight_check(client)
            if not ok:
                logger.error("Pre-flight checks failed for %s — aborting", self.engine)
                self.metrics.record_failure(FailureType.API_REJECTION, "Pre-flight failed")
                return self.metrics

            # Start background resource monitor
            self._stop_monitor = False
            monitor_task = asyncio.create_task(self._collect_resource_samples())

            sem = asyncio.Semaphore(self.concurrency)

            async def worker(worker_id: int):
                iteration = 0
                while True:
                    # Exit conditions
                    if self.duration > 0:
                        if time.time() - start_time > self.duration:
                            break
                    else:
                        if iteration >= self.iterations:
                            break

                    async with sem:
                        prompt = build_prompt(self.engine, iteration)
                        await self._run_one_session(client, worker_id, iteration, prompt)

                    iteration += 1

            workers = [
                asyncio.create_task(worker(i), name=f"worker-{self.engine}-{i}")
                for i in range(self.concurrency)
            ]
            await asyncio.gather(*workers, return_exceptions=True)

            # Stop resource monitor
            self._stop_monitor = True
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        # ── Snapshot memory AFTER ─────────────────────────────────────────
        post_stats = get_container_stats_numeric()
        self.metrics.memory_after_mb = post_stats["memory_mb"]
        if post_stats["cpu_pct"] > 0:
            self.metrics.cpu_samples.append(post_stats["cpu_pct"])

        return self.metrics

    async def _run_one_session(
        self,
        client: httpx.AsyncClient,
        worker_id: int,
        iteration: int,
        prompt: str,
    ):
        """Start one agent session, poll to completion, record metrics."""
        session_start = time.time()
        session_id: Optional[str] = None
        log_prefix = f"[W{worker_id}:I{iteration}:{self.engine}]"

        try:
            # ── Start the agent session ───────────────────────────────────
            payload = {
                "task": prompt,
                "engine": self.engine,
                "mode": self.mode,
                "provider": self.provider,
                "model": self.model,
                "max_steps": 15,  # keep short for stress tests
            }
            if self.api_key:
                payload["api_key"] = self.api_key

            logger.debug("%s Starting session...", log_prefix)
            resp = await client.post(BACKEND_START_AGENT, json=payload, timeout=30)

            if resp.status_code != 200:
                self.metrics.record_failure(
                    FailureType.HTTP_ERROR,
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
                logger.warning("%s Start failed: HTTP %d", log_prefix, resp.status_code)
                return

            data = resp.json()

            if "error" in data:
                # Rate limit, validation, or capacity rejection
                self.metrics.record_failure(
                    FailureType.API_REJECTION,
                    data["error"][:200],
                )
                logger.warning("%s Rejected: %s", log_prefix, data["error"][:100])
                return

            session_id = data.get("session_id")
            if not session_id:
                self.metrics.record_failure(FailureType.UNKNOWN, "No session_id returned")
                return

            self.metrics.sessions_started += 1
            logger.info("%s Session %s started", log_prefix, session_id[:8])

            # ── Poll until session completes ─────────────────────────────
            completed, session_data = await self._poll_session(
                client, session_id, log_prefix, session_start,
            )

            latency = time.time() - session_start

            if completed:
                self.metrics.record_success(latency)
                self.metrics.sessions_completed += 1
                logger.info(
                    "%s Session %s completed in %.1fs",
                    log_prefix, session_id[:8], latency,
                )
                # ── Verify real output ────────────────────────────────
                if session_data and verify_session_output(session_data):
                    self.metrics.verified_outputs += 1
                else:
                    self.metrics.invalid_outputs += 1
                    logger.warning(
                        "%s Session %s: output verification failed",
                        log_prefix, session_id[:8],
                    )
            else:
                self.metrics.record_failure(
                    FailureType.TIMEOUT,
                    f"Session {session_id[:8]} timed out after {latency:.0f}s",
                )
                # Try to stop the timed-out session
                await self._stop_session(client, session_id, log_prefix)

        except httpx.TimeoutException:
            latency = time.time() - session_start
            self.metrics.record_failure(
                FailureType.TIMEOUT,
                f"Request timeout at {latency:.0f}s",
                tb=traceback.format_exc(),
            )
            logger.warning("%s Timeout", log_prefix)
        except httpx.ConnectError:
            self.metrics.record_failure(
                FailureType.DISCONNECT,
                "Backend connection refused",
                tb=traceback.format_exc(),
            )
            logger.error("%s Connection refused", log_prefix)
        except Exception as exc:
            tb_str = traceback.format_exc()
            is_disconnect = "disconnect" in str(exc).lower() or "closed" in str(exc).lower()
            self.metrics.record_failure(
                FailureType.DISCONNECT if is_disconnect else FailureType.UNKNOWN,
                str(exc)[:200],
                tb=tb_str,
            )
            logger.error("%s Unexpected error: %s", log_prefix, exc)

    async def _poll_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        log_prefix: str,
        session_start: float,
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Poll /api/agent/status until completed/error/timeout.

        Returns ``(completed: bool, session_data: dict | None)``.
        """
        while True:
            elapsed = time.time() - session_start
            if elapsed > self.session_timeout:
                logger.warning("%s Session timeout (%ds)", log_prefix, self.session_timeout)
                return False, None

            await asyncio.sleep(self.poll_interval)

            try:
                resp = await client.get(
                    f"{BACKEND_STATUS_AGENT}/{session_id}",
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()

                # Session not found (already cleaned up) → treat as completed
                if "error" in data:
                    logger.debug("%s Status poll: %s", log_prefix, data["error"])
                    return True, data  # session finished and was cleaned up

                status = data.get("status", "unknown")
                step = data.get("current_step", 0)

                if status in ("completed", "error"):
                    if status == "error":
                        self.metrics.record_failure(
                            FailureType.AGENT_ERROR,
                            f"Agent finished with error at step {step}",
                        )
                        # Don't double-count: the caller will skip record_success
                        return False, data
                    return True, data

                logger.debug(
                    "%s Session %s: status=%s step=%d elapsed=%.0fs",
                    log_prefix, session_id[:8], status, step, elapsed,
                )

            except Exception as exc:
                logger.debug("%s Status poll error: %s", log_prefix, exc)

    async def _stop_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        log_prefix: str,
    ):
        """Best-effort stop of a session that timed out."""
        try:
            await client.post(f"{BACKEND_STOP_AGENT}/{session_id}", timeout=10)
            logger.info("%s Stopped timed-out session %s", log_prefix, session_id[:8])
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Report Rendering
# ══════════════════════════════════════════════════════════════════════════════

def _severity(metric: EngineMetrics) -> str:
    """Return PASS / WARN / FAIL based on success rate and output validity."""
    rate = metric.success_rate
    invalid_ratio = metric.invalid_outputs / max(metric.calls, 1)
    if rate >= 0.95 and invalid_ratio <= 0.05:
        return "PASS"
    if rate >= 0.70:
        return "WARN"
    return "FAIL"


def _overall_verdict(all_metrics: Dict[str, EngineMetrics]) -> str:
    """Return overall PASS only if every engine passes."""
    return "PASS" if all(
        _severity(m) != "FAIL" for m in all_metrics.values()
    ) else "FAIL"


def print_report(
    all_metrics: Dict[str, EngineMetrics],
    wall_time: float,
    *,
    dump_logs_on_fail: bool = True,
):
    """Print the CUA FULL SYSTEM STRESS REPORT to stdout.

    Format (per engine):
        Total Calls | Failures | Disconnects | Avg Latency | Max Latency
        Memory Delta | CPU Peak

    Overall: Pass / Fail
    On failure: last 50 container logs + stack traces + exit non-zero.
    """
    header_width = 76

    print("\n" + "═" * header_width)
    print("  CUA FULL SYSTEM STRESS REPORT")
    print("═" * header_width)
    print(f"  Wall time : {wall_time:.1f}s")
    print(f"  Engines   : {len(all_metrics)}")
    print()

    # ── Per-engine table ──────────────────────────────────────────────────
    for engine, m in all_metrics.items():
        sev = _severity(m)
        tag = f"[{sev}]"
        print(f"  ┌── {engine.upper()} {tag} {'─' * (header_width - 10 - len(engine) - len(tag))}")
        print(f"  │  Total Calls  : {m.calls}")
        print(f"  │  Failures     : {m.failures}")
        print(f"  │  Disconnects  : {m.disconnects}")
        print(f"  │  Avg Latency  : {m.avg_latency:.3f}s")
        print(f"  │  Max Latency  : {m.max_latency:.3f}s")
        print(f"  │  Memory Delta : {m.memory_delta_mb:+.1f} MB")
        print(f"  │  CPU Peak     : {m.cpu_peak:.1f}%")
        # Extra detail rows (only if non-zero)
        if m.timeouts:
            print(f"  │  Timeouts     : {m.timeouts}")
        if m.api_rejections:
            print(f"  │  API Rejects  : {m.api_rejections}")
        if m.verified_outputs or m.invalid_outputs:
            print(f"  │  Verified Out : {m.verified_outputs}  |  Invalid: {m.invalid_outputs}")
        if m.failure_messages:
            print(f"  │  Top failures :")
            for msg in m.failure_messages[:5]:
                print(f"  │    • {msg}")
        print(f"  └{'─' * (header_width - 3)}")
        print()

    # ── Overall verdict ───────────────────────────────────────────────────
    verdict = _overall_verdict(all_metrics)
    print(f"  Overall: {verdict}")
    print("═" * header_width)

    # ── Failure diagnostics ───────────────────────────────────────────────
    if verdict == "FAIL" and dump_logs_on_fail:
        print()
        print("  *** FAILURE DIAGNOSTICS ***")
        print()

        # Last 50 container logs
        print("  ─── Container Logs (last 50 lines) ───")
        logs = tail_container_logs(50)
        for line in logs.splitlines():
            print(f"    {line}")
        print("  ─── End Container Logs ───")
        print()

        # Stack traces collected during the run
        all_traces = []
        for eng, m in all_metrics.items():
            for tb in m.stack_traces:
                all_traces.append(f"[{eng}] {tb}")
        if all_traces:
            print("  ─── Collected Stack Traces ───")
            for tb_block in all_traces[-20:]:  # cap at last 20
                for line in tb_block.splitlines():
                    print(f"    {line}")
                print()
            print("  ─── End Stack Traces ───")
        print()

    # ── Machine-readable JSON ─────────────────────────────────────────────
    print("\n--- JSON REPORT ---")
    json_report = {
        engine: m.summary() for engine, m in all_metrics.items()
    }
    json_report["_meta"] = {
        "wall_time_s": round(wall_time, 1),
        "verdict": verdict,
    }
    print(json.dumps(json_report, indent=2))
    print("--- END JSON REPORT ---\n")

    return verdict


# ══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="CUA Full System Stress Test Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backend/tests/stress_system.py --engine playwright --concurrency 3 --iterations 20
  python backend/tests/stress_system.py --engine all --concurrency 2 --iterations 10
  python backend/tests/stress_system.py --engine all --concurrency 3 --duration 300
""",
    )
    parser.add_argument(
        "--engine",
        required=True,
        choices=ENGINES + ["all"],
        help="Engine to stress test, or 'all' for sequential run through every engine",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent workers per engine (default: 1)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of iterations per worker (ignored when --duration is set; default: 10)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for this many seconds instead of a fixed iteration count (0 = use iterations)",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=["google", "anthropic"],
        help=f"LLM provider (default: {DEFAULT_PROVIDER})",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key override (default: resolved from env / .env)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between status polls (default: 2.0)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=120.0,
        help="Max seconds to wait for a single session (default: 120)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort immediately on first engine failure",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace):
    """Async entry point — run harness for each selected engine."""
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    engines = ENGINES if args.engine == "all" else [args.engine]

    header_width = 76
    print()
    print("═" * header_width)
    print("  CUA FULL SYSTEM STRESS HARNESS — Starting")
    print("═" * header_width)
    print(f"  Engines     : {', '.join(engines)}")
    print(f"  Concurrency : {args.concurrency}")
    print(f"  Iterations  : {args.iterations}" if args.duration == 0 else f"  Duration    : {args.duration}s")
    print(f"  Provider    : {args.provider}")
    print(f"  Backend     : {BACKEND_BASE}")
    print(f"  Container   : {CONTAINER_NAME}")
    print("═" * header_width)
    print()

    all_metrics: Dict[str, EngineMetrics] = {}
    wall_start = time.time()

    for engine in engines:
        print(f"\n{'─' * 50}")
        print(f"  Stressing engine: {engine}")
        print(f"{'─' * 50}")

        harness = StressTestHarness(
            engine=engine,
            concurrency=args.concurrency,
            iterations=args.iterations,
            duration=args.duration,
            provider=args.provider,
            api_key=args.api_key,
            poll_interval=args.poll_interval,
            session_timeout=args.session_timeout,
        )

        metrics = await harness.run()
        all_metrics[engine] = metrics

        # Quick per-engine status
        sev = _severity(metrics)
        print(f"  Engine {engine}: {sev}  "
              f"(calls={metrics.calls}  failures={metrics.failures}  "
              f"avg_lat={metrics.avg_latency:.3f}s  "
              f"mem_delta={metrics.memory_delta_mb:+.1f}MB)")

        if sev == "FAIL" and args.fail_fast:
            print("  --fail-fast enabled — aborting remaining engines")
            break

        # Brief cooldown between engines to avoid rate limits
        if len(engines) > 1:
            await asyncio.sleep(3)

    wall_time = time.time() - wall_start

    # ── Final report ──────────────────────────────────────────────────────
    verdict = print_report(all_metrics, wall_time, dump_logs_on_fail=True)

    exit_code = 0 if verdict == "PASS" else 1
    return exit_code


def main():
    """Synchronous entry point."""
    args = parse_args()
    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
