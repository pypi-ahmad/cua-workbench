"""Pytest configuration and shared fixtures for CUA stress tests.

Provides:
- Custom markers (stress, integration, slow)
- Shared mock factories as fixtures
- Site rotation fixtures
- Per-engine parametrization helpers
- Stress metrics collection fixture
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.stress.helpers import (
    ALL_ENGINES,
    ALL_SITE_URLS,
    BROWSER_ENGINES,
    DESKTOP_ENGINES,
    ENGINE_MODES,
    SITES,
    STRESS,
    StressMetrics,
    mock_agent_service_success,
    mock_agent_service_failure,
    mock_agent_service_intermittent,
    mock_screenshot_b64,
)

logger = logging.getLogger(__name__)


# ── Custom markers ────────────────────────────────────────────────────────────

def pytest_configure(config):
    """Register custom markers for stress tests."""
    config.addinivalue_line("markers", "stress: system-wide stress tests")
    config.addinivalue_line("markers", "slow: tests that take >10 seconds")
    config.addinivalue_line("markers", "phase1: Phase 1 – Engine stress tests")
    config.addinivalue_line("markers", "phase2: Phase 2 – Agent loop stress tests")
    config.addinivalue_line("markers", "phase3: Phase 3 – MCP transport stress tests")
    config.addinivalue_line("markers", "phase4: Phase 4 – Desktop automation stress tests")
    config.addinivalue_line("markers", "phase5: Phase 5 – Accessibility engine stress tests")
    config.addinivalue_line("markers", "phase6: Phase 6 – Hybrid fallback stress tests")
    config.addinivalue_line("markers", "phase7: Phase 7 – Frontend interaction stress tests")
    config.addinivalue_line("markers", "phase8: Phase 8 – Soak test (engine rotation, resource monitoring)")


# ── Event loop fixture ────────────────────────────────────────────────────────

@pytest.fixture
def event_loop():
    """Provide a fresh event loop for each async test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Site fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(params=list(SITES.keys()), ids=lambda k: f"site-{k}")
def target_site(request):
    """Parametrized fixture yielding each TargetSite."""
    return SITES[request.param]


@pytest.fixture
def all_sites():
    """Return all target sites as a list."""
    return list(SITES.values())


@pytest.fixture
def site_urls():
    """Return all target site URLs."""
    return ALL_SITE_URLS


# ── Engine fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(params=ALL_ENGINES, ids=lambda e: f"engine-{e}")
def engine_name(request):
    """Parametrized fixture yielding each engine name."""
    return request.param


@pytest.fixture(params=BROWSER_ENGINES, ids=lambda e: f"browser-{e}")
def browser_engine(request):
    """Parametrized fixture for browser engines only."""
    return request.param


@pytest.fixture(params=DESKTOP_ENGINES, ids=lambda e: f"desktop-{e}")
def desktop_engine(request):
    """Parametrized fixture for desktop engines only."""
    return request.param


@pytest.fixture
def engine_mode():
    """Return the engine→mode mapping."""
    return ENGINE_MODES


# ── Mock fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_send_success():
    """Patch _send_with_retry to always succeed."""
    mock = mock_agent_service_success()
    with patch("backend.agent.executor._send_with_retry", mock):
        yield mock


@pytest.fixture
def mock_send_failure():
    """Patch _send_with_retry to always fail."""
    mock = mock_agent_service_failure()
    with patch("backend.agent.executor._send_with_retry", mock):
        yield mock


@pytest.fixture
def mock_send_intermittent():
    """Patch _send_with_retry with random failures."""
    mock = mock_agent_service_intermittent(fail_rate=STRESS.error_injection_rate)
    with patch("backend.agent.executor._send_with_retry", mock):
        yield mock


@pytest.fixture
def fake_screenshot():
    """Return a 1x1 white PNG as base64."""
    return mock_screenshot_b64()


@pytest.fixture
def mock_capture_screenshot(fake_screenshot):
    """Patch capture_screenshot to return a fake screenshot."""
    mock = AsyncMock(return_value=fake_screenshot)
    with patch("backend.agent.screenshot.capture_screenshot", mock):
        yield mock


@pytest.fixture
def mock_mcp_action():
    """Patch MCP execute_mcp_action to succeed."""
    mock = AsyncMock(return_value={"success": True, "message": "MCP OK", "engine": "playwright_mcp"})
    with patch("backend.agent.playwright_mcp_client.execute_mcp_action", mock):
        yield mock


@pytest.fixture
def mock_a11y_action():
    """Patch accessibility execute_accessibility_action to succeed."""
    mock = AsyncMock(return_value={"success": True, "message": "A11Y OK", "engine": "omni_accessibility"})
    with patch("backend.engines.accessibility_engine.execute_accessibility_action", mock):
        yield mock


@pytest.fixture
def mock_hybrid_action():
    """Patch desktop_hybrid execute_desktop_hybrid_action to succeed."""
    mock = AsyncMock(return_value={
        "success": True,
        "message": "Hybrid OK",
        "engine": "desktop_hybrid",
        "primary_engine": "xdotool",
        "fallback_used": False,
    })
    with patch("backend.engines.desktop_hybrid_engine.execute_desktop_hybrid_action", mock):
        yield mock


# ── Metrics fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def stress_metrics():
    """Fresh StressMetrics collector for a single test."""
    return StressMetrics()


@pytest.fixture
def stress_config():
    """Return the global StressConfig."""
    return STRESS


# ── Agent loop mock fixture ──────────────────────────────────────────────────

@pytest.fixture
def mock_agent_loop_deps(fake_screenshot):
    """Patch all external deps of AgentLoop for hermetic stress testing.

    Mocks: capture_screenshot, query_model, execute_action, check_service_health.
    Returns a dict of all mocks for assertion.
    """
    mock_screenshot = AsyncMock(return_value=fake_screenshot)
    mock_query = AsyncMock(return_value=(
        {"action": "click", "coordinates": [100, 200], "reasoning": "stress"},
        '{"action":"click","coordinates":[100,200]}',
    ))
    mock_execute = AsyncMock(return_value={"success": True, "message": "OK"})
    mock_health = MagicMock()

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
    }

    yield mocks

    for p in patches.values():
        p.stop()
