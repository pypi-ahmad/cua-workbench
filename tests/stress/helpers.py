"""Shared constants, helpers, and site configurations for stress tests.

All 8 phases import from this module to avoid duplication.
Uses only safe, captcha-free public websites.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock

logger = logging.getLogger(__name__)


# ── Target websites (captcha-free) ────────────────────────────────────────────

@dataclass(frozen=True)
class TargetSite:
    """Descriptor for a public test target website."""

    name: str
    url: str
    search_selector: Optional[str] = None  # CSS selector for a search/input field
    submit_selector: Optional[str] = None  # CSS selector for submit button
    expected_title_fragment: Optional[str] = None
    has_forms: bool = False
    has_links: bool = True
    has_api: bool = False


SITES = {
    "duckduckgo": TargetSite(
        name="DuckDuckGo",
        url="https://duckduckgo.com",
        search_selector='input[name="q"]',
        submit_selector='button[type="submit"]',
        expected_title_fragment="DuckDuckGo",
        has_forms=True,
    ),
    "books": TargetSite(
        name="Books to Scrape",
        url="https://books.toscrape.com",
        expected_title_fragment="Books to Scrape",
        has_links=True,
    ),
    "httpbin": TargetSite(
        name="httpbin",
        url="https://httpbin.org",
        expected_title_fragment="httpbin",
        has_api=True,
        has_forms=True,
    ),
    "wikipedia": TargetSite(
        name="Wikipedia",
        url="https://wikipedia.org",
        search_selector='input#searchInput',
        submit_selector='button[type="submit"]',
        expected_title_fragment="Wikipedia",
        has_forms=True,
    ),
    "jsonplaceholder": TargetSite(
        name="JSONPlaceholder",
        url="https://jsonplaceholder.typicode.com",
        expected_title_fragment="JSONPlaceholder",
        has_api=True,
    ),
}

ALL_SITE_URLS = [s.url for s in SITES.values()]

# ── Engine constants ──────────────────────────────────────────────────────────

ALL_ENGINES = [
    "playwright_mcp",
    "omni_accessibility",
    "computer_use",
]

BROWSER_ENGINES = ["playwright_mcp"]
DESKTOP_ENGINES = ["omni_accessibility", "computer_use"]
A11Y_ENGINES = ["omni_accessibility"]
CU_ENGINES = ["computer_use"]

ENGINE_MODES = {
    "playwright_mcp": "browser",
    "omni_accessibility": "desktop",
    "computer_use": "desktop",
}


# ── Stress test parameters ────────────────────────────────────────────────────

@dataclass
class StressConfig:
    """Tunables for stress test intensity."""

    # Concurrency
    max_concurrent_sessions: int = 5
    max_concurrent_actions: int = 20

    # Volume
    rapid_fire_actions: int = 50
    bulk_navigation_cycles: int = 10
    sequential_task_count: int = 15

    # Timing
    action_burst_delay_ms: float = 50
    step_timeout_sec: float = 30.0
    session_timeout_sec: float = 120.0

    # Error injection
    error_injection_rate: float = 0.15  # 15% of actions get injected faults
    corrupt_payload_count: int = 20
    oversized_payload_chars: int = 100_000

    # Thresholds
    max_acceptable_latency_ms: float = 5_000
    min_success_rate: float = 0.70  # 70% of stress actions should succeed
    max_memory_growth_mb: float = 200


STRESS = StressConfig()


# ── Helper: async timing decorator ────────────────────────────────────────────

def timed_async(fn: Callable) -> Callable:
    """Decorator that records wall-clock duration on the returned dict."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        result = await fn(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if isinstance(result, dict):
            result["_elapsed_ms"] = elapsed_ms
        return result

    return wrapper


# ── Helper: run coroutine in sync tests ───────────────────────────────────────

def run_async(coro):
    """Run an async coroutine from a synchronous test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Helper: generate bulk actions ─────────────────────────────────────────────

def make_click_action(x: int = 100, y: int = 200, target: str | None = None):
    """Create a minimal AgentAction for click."""
    from backend.models import ActionType, AgentAction
    return AgentAction(action=ActionType.CLICK, coordinates=[x, y], target=target)


def make_type_action(text: str = "stress test", x: int = 100, y: int = 200):
    """Create a minimal AgentAction for typing."""
    from backend.models import ActionType, AgentAction
    return AgentAction(action=ActionType.TYPE, text=text, coordinates=[x, y])


def make_open_url_action(url: str = "https://duckduckgo.com"):
    """Create a minimal AgentAction for navigation."""
    from backend.models import ActionType, AgentAction
    return AgentAction(action=ActionType.OPEN_URL, text=url)


def make_scroll_action(direction: str = "down", x: int = 720, y: int = 450):
    """Create a minimal AgentAction for scrolling."""
    from backend.models import ActionType, AgentAction
    action_type = ActionType.SCROLL_DOWN if direction == "down" else ActionType.SCROLL_UP
    return AgentAction(action=action_type, coordinates=[x, y])


def make_key_action(key: str = "Enter"):
    """Create a minimal AgentAction for a key press."""
    from backend.models import ActionType, AgentAction
    return AgentAction(action=ActionType.KEY, text=key)


def make_done_action(reason: str = "Task complete"):
    """Create a terminal done action."""
    from backend.models import ActionType, AgentAction
    return AgentAction(action=ActionType.DONE, reasoning=reason)


# ── Helper: bulk action sequences ─────────────────────────────────────────────

def generate_navigation_sequence(sites: Sequence[TargetSite] | None = None) -> list:
    """Generate a sequence of open_url actions cycling through target sites."""
    sites = sites or list(SITES.values())
    return [make_open_url_action(s.url) for s in sites]


def generate_rapid_click_sequence(count: int = 50, spread: bool = True) -> list:
    """Generate N click actions, optionally spread across the viewport."""
    import random
    actions = []
    for i in range(count):
        if spread:
            x = random.randint(50, 1390)
            y = random.randint(50, 850)
        else:
            x, y = 720, 450  # centered
        actions.append(make_click_action(x, y))
    return actions


def generate_typing_barrage(count: int = 20) -> list:
    """Generate N type actions with varying text lengths."""
    actions = []
    for i in range(count):
        text = f"stress_input_{i}_" + "x" * (i * 10)
        actions.append(make_type_action(text))
    return actions


# ── Helper: mock factories ────────────────────────────────────────────────────

def mock_agent_service_success() -> AsyncMock:
    """AsyncMock that simulates a successful agent service response."""
    mock = AsyncMock()
    mock.return_value = {"success": True, "message": "OK", "engine": "playwright_mcp"}
    return mock


def mock_agent_service_failure(message: str = "Service error") -> AsyncMock:
    """AsyncMock that simulates a failing agent service response."""
    mock = AsyncMock()
    mock.return_value = {"success": False, "message": message}
    return mock


def mock_agent_service_intermittent(fail_rate: float = 0.3) -> AsyncMock:
    """AsyncMock that fails randomly at the given rate."""
    import random

    async def _intermittent(*args: Any, **kwargs: Any) -> dict:
        if random.random() < fail_rate:
            return {"success": False, "message": "Intermittent failure"}
        return {"success": True, "message": "OK"}

    mock = AsyncMock(side_effect=_intermittent)
    return mock


def mock_screenshot_b64() -> str:
    """Return a tiny valid base64-encoded 1x1 white PNG."""
    import base64
    # Minimal valid PNG: 1x1 white pixel
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return base64.b64encode(png_bytes).decode()


def mock_gemini_response(action: str = "click", coords: list | None = None,
                         text: str | None = None, target: str | None = None) -> str:
    """Build a mock JSON response string as Gemini would return."""
    import json
    payload: dict = {"action": action}
    if coords:
        payload["coordinates"] = coords
    if text:
        payload["text"] = text
    if target:
        payload["target"] = target
    payload["reasoning"] = "Stress test mock response"
    return json.dumps(payload)


# ── Helper: latency / throughput collectors ───────────────────────────────────

@dataclass
class StressMetrics:
    """Aggregate metrics collected during a stress run."""

    total_actions: int = 0
    successful: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)
    latencies_ms: List[float] = field(default_factory=list)

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
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    def record(self, success: bool, latency_ms: float, error: str | None = None):
        self.total_actions += 1
        self.latencies_ms.append(latency_ms)
        if success:
            self.successful += 1
        else:
            self.failed += 1
            if error:
                self.errors.append(error)

    def summary(self) -> Dict[str, Any]:
        return {
            "total": self.total_actions,
            "success_rate": f"{self.success_rate:.1%}",
            "avg_latency_ms": f"{self.avg_latency_ms:.1f}",
            "p95_latency_ms": f"{self.p95_latency_ms:.1f}",
            "max_latency_ms": f"{self.max_latency_ms:.1f}",
            "errors": len(self.errors),
        }
