"""Phase 7 — Frontend Stress Tests (Playwright).

Uses Playwright (Python) to automate a real browser against the CUA frontend.

For each engine the test:
  1. Opens the frontend at localhost:5173
  2. Selects the engine from the dropdown
  3. Fills a task prompt
  4. Submits the form
  5. Waits for the agent-finished WebSocket event
  6. Validates the response

Repeats 20 times per engine (6 engines × 20 = 120 iterations).

Ensures:
  - No frontend crash (page stays alive, no unrecoverable JS errors)
  - No CORS errors
  - No backend 500 responses

Run with:
    pytest tests/stress/test_phase7_frontend_stress.py -v -m phase7
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

# These must be at module level so ``from __future__ import annotations``
# (PEP 563 deferred evaluation) can resolve the type-hints used in the
# mock FastAPI WebSocket handler registered by ``_build_mock_app()``.
from fastapi import WebSocket  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

FRONTEND_PORT = 5173
BACKEND_PORT = 8000
RUNS_PER_ENGINE = 20

# Use 127.0.0.1 (IPv4) everywhere to avoid IPv6 resolution issues.
# When the browser loads from http://127.0.0.1:5173, window.location.hostname
# is '127.0.0.1', so the useWebSocket hook connects to ws://127.0.0.1:8000/ws
# which directly hits the IPv4 mock backend.
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"

# The three CUA engines with their UI <option> values
ALL_ENGINES = [
    "playwright_mcp",
    "omni_accessibility",
    "computer_use",
]

SEARCH_PROMPT = (
    "Open duckduckgo.com, search for automation testing, "
    "return the first 3 result titles as JSON."
)

# ── Metrics ───────────────────────────────────────────────────────────────────


@dataclass
class FrontendStressMetrics:
    """Collected metrics for the Phase-7 frontend stress run."""

    total_submissions: int = 0
    successful_submissions: int = 0
    js_errors: List[str] = field(default_factory=list)
    cors_errors: List[str] = field(default_factory=list)
    http_500_errors: List[str] = field(default_factory=list)
    api_responses: List[dict] = field(default_factory=list)
    ws_messages: List[dict] = field(default_factory=list)
    page_crashes: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful_submissions / max(self.total_submissions, 1)


# ── Port availability helpers ─────────────────────────────────────────────────


def _port_in_use(port: int) -> bool:
    """Return True if *port* is already bound on 127.0.0.1 (IPv4)."""
    try:
        with closing(socket.create_connection(("127.0.0.1", port), timeout=1)):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Block until *port* is accepting connections or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use(port):
            return True
        time.sleep(0.3)
    return False


# ── Mock FastAPI backend ──────────────────────────────────────────────────────


def _build_mock_app():
    """Return a minimal FastAPI app that mimics the real CUA backend.

    Provides the endpoints that the frontend calls:
      GET  /api/health
      GET  /api/container/status
      GET  /api/keys/status
      POST /api/agent/start
      POST /api/agent/stop/{session_id}
      GET  /api/agent/status/{session_id}
      WS   /ws
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    mock_app = FastAPI()
    mock_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Track sessions so /agent/stop can acknowledge them
    _sessions: Dict[str, dict] = {}
    _ws_clients: List[WebSocket] = []

    @mock_app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @mock_app.get("/api/container/status")
    async def container_status():
        return {"running": True, "agent_service": True}

    @mock_app.get("/api/agent-service/health")
    async def agent_health():
        return {"healthy": True, "url": "http://localhost:7860"}

    @mock_app.get("/api/keys/status")
    async def keys_status():
        return {
            "keys": [
                {"provider": "google", "available": True, "source": "env", "masked_key": "AIza...test"},
                {"provider": "anthropic", "available": True, "source": "env", "masked_key": "sk-a...test"},
            ]
        }

    @mock_app.get("/api/screenshot")
    async def screenshot():
        # 1×1 transparent PNG (same fixture used in prior phases)
        return {"screenshot": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4DwAAAQEABRjYTgAAAABJRU5ErkJggg=="}

    @mock_app.post("/api/container/start")
    async def start_container():
        return {"success": True}

    @mock_app.post("/api/container/stop")
    async def stop_container():
        return {"success": True}

    @mock_app.post("/api/agent/start")
    async def start_agent(body: dict):
        sid = str(uuid.uuid4())
        _sessions[sid] = {
            "session_id": sid,
            "status": "running",
            "task": body.get("task", ""),
            "engine": body.get("engine", "playwright"),
            "mode": body.get("mode", "browser"),
            "provider": body.get("provider", "google"),
        }

        # Schedule WS notifications (step events + agent_finished)
        async def _simulate_agent():
            await asyncio.sleep(0.15)
            step_data = {
                "event": "step",
                "step": {
                    "step_number": 1,
                    "timestamp": "2026-02-28T12:00:00+00:00",
                    "action": {
                        "action": "open_url",
                        "text": "https://duckduckgo.com",
                        "reasoning": "Navigate to search engine",
                        "target": None,
                        "coordinates": None,
                    },
                    "error": None,
                },
            }
            done_step = {
                "event": "step",
                "step": {
                    "step_number": 2,
                    "timestamp": "2026-02-28T12:00:01+00:00",
                    "action": {
                        "action": "done",
                        "text": None,
                        "reasoning": json.dumps(["Selenium Testing", "Playwright Tutorial", "Robot Framework Guide"]),
                        "target": None,
                        "coordinates": None,
                    },
                    "error": None,
                },
            }
            finish_data = {
                "event": "agent_finished",
                "session_id": sid,
                "status": "completed",
                "steps": 2,
            }

            stale = []
            for ws in _ws_clients:
                try:
                    await ws.send_text(json.dumps(step_data))
                    await asyncio.sleep(0.05)
                    await ws.send_text(json.dumps(done_step))
                    await asyncio.sleep(0.05)
                    await ws.send_text(json.dumps(finish_data))
                except Exception:
                    stale.append(ws)
            for ws in stale:
                if ws in _ws_clients:
                    _ws_clients.remove(ws)

            _sessions[sid]["status"] = "completed"

        asyncio.create_task(_simulate_agent())

        return {
            "session_id": sid,
            "status": "running",
            "mode": body.get("mode", "browser"),
            "engine": body.get("engine", "playwright"),
            "provider": body.get("provider", "google"),
        }

    @mock_app.post("/api/agent/stop/{session_id}")
    async def stop_agent(session_id: str):
        if session_id in _sessions:
            _sessions[session_id]["status"] = "stopped"
        return {"session_id": session_id, "status": "stopped"}

    @mock_app.get("/api/agent/status/{session_id}")
    async def agent_status(session_id: str):
        s = _sessions.get(session_id)
        if not s:
            return {"error": "Session not found"}
        return {
            "session_id": session_id,
            "status": s["status"],
            "current_step": 2,
            "total_steps": 50,
            "last_action": None,
        }

    @mock_app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        _ws_clients.append(ws)
        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await ws.send_text(json.dumps({"event": "pong"}))
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if ws in _ws_clients:
                _ws_clients.remove(ws)

    return mock_app


def _run_mock_backend(app, port: int):
    """Start the mock backend in the current thread (blocking)."""
    import uvicorn

    # Use info level to capture WS 403 rejections for debugging
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _start_mock_backend_thread(port: int) -> threading.Thread:
    """Start the mock backend in a daemon thread and return the thread."""
    app = _build_mock_app()
    t = threading.Thread(target=_run_mock_backend, args=(app, port), daemon=True)
    t.start()
    return t


# ── Vite dev server management ────────────────────────────────────────────────


def _start_vite_dev_server() -> subprocess.Popen:
    """Launch the Vite dev server as a child process and return the Popen handle."""
    frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
    frontend_dir = os.path.abspath(frontend_dir)
    env = os.environ.copy()

    # Write Vite output to a log file for debugging
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    vite_log = open(os.path.join(log_dir, "vite_debug.log"), "w")

    if sys.platform == "win32":
        # On Windows, use cmd /c to ensure cwd is set correctly before
        # invoking the vite batch wrapper.
        vite_rel = r"node_modules\.bin\vite.cmd"
        cmd_str = f'cd /d "{frontend_dir}" && {vite_rel} --host 127.0.0.1 --port {FRONTEND_PORT} --strictPort'
        return subprocess.Popen(
            cmd_str,
            shell=True,
            env=env,
            stdout=vite_log,
            stderr=vite_log,
        )
    else:
        cmd = ["npx", "vite", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"]
        return subprocess.Popen(
            cmd,
            cwd=frontend_dir,
            env=env,
            stdout=vite_log,
            stderr=vite_log,
        )


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _kill_port_occupants(port: int) -> None:
    """Forcefully kill any process currently listening on *port*.

    Stale daemon threads from prior test runs can hold the port and cause
    mysterious 403 errors on WebSocket handshake.  This helper ensures a clean
    slate before binding.  Works on both Windows and Unix.
    """
    if sys.platform == "win32":
        # Use netstat + taskkill as a reliable fallback
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                # Match lines like "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    12345"
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1].strip()
                    if pid.isdigit() and int(pid) != os.getpid():
                        logger.info("Killing stale process PID %s on port %d", pid, port)
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", pid],
                            capture_output=True, timeout=10,
                        )
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid in result.stdout.strip().splitlines():
                pid = pid.strip()
                if pid.isdigit() and int(pid) != os.getpid():
                    os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass
    # Give OS time to release the socket
    time.sleep(2)


@pytest.fixture(scope="module")
def mock_backend():
    """Start a mock FastAPI backend on port 8000 for the duration of the module."""
    _kill_port_occupants(BACKEND_PORT)

    if _port_in_use(BACKEND_PORT):
        pytest.skip(f"Port {BACKEND_PORT} already in use — cannot start mock backend")

    thread = _start_mock_backend_thread(BACKEND_PORT)

    if not _wait_for_port(BACKEND_PORT, timeout=15.0):
        pytest.fail("Mock backend did not start within 15 seconds")

    yield thread
    # Daemon thread will be cleaned up when the test process exits


def _kill_proc_tree(proc: subprocess.Popen):
    """Kill a subprocess and all its children on Windows; plain terminate elsewhere."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


@pytest.fixture(scope="module")
def vite_server(mock_backend):
    """Start the Vite dev server on port 5173; requires mock_backend to be up first."""
    _kill_port_occupants(FRONTEND_PORT)

    if _port_in_use(FRONTEND_PORT):
        # Vite is already running (user has it open) — just use it
        yield None
        return

    proc = _start_vite_dev_server()
    if not _wait_for_port(FRONTEND_PORT, timeout=30.0):
        _kill_proc_tree(proc)
        pytest.fail(
            f"Vite dev server did not start within 30 seconds (pid={proc.pid})"
        )

    yield proc
    _kill_proc_tree(proc)


@pytest.fixture(scope="module")
def browser_context(vite_server):
    """Launch a Playwright Chromium browser and return a context.

    Yields (browser, context) tuple. Closes both after the module.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=True,
    )

    yield browser, context

    context.close()
    browser.close()
    pw.stop()


# ── Helper functions ──────────────────────────────────────────────────────────


def _engine_mode(engine: str) -> str:
    """Return the mode string the frontend sends for the given engine."""
    if engine in ("xdotool", "omni_accessibility", "desktop_hybrid"):
        return "desktop"
    if engine == "ydotool":
        return "ydotool"
    return "browser"


def _collect_console_errors(page, metrics: FrontendStressMetrics):
    """Attach a console listener that captures errors and CORS messages."""

    def _on_console(msg):
        if msg.type == "error":
            text = msg.text
            metrics.js_errors.append(text)
            if "cors" in text.lower() or "cross-origin" in text.lower():
                metrics.cors_errors.append(text)

    page.on("console", _on_console)


def _collect_network_errors(page, metrics: FrontendStressMetrics):
    """Attach a response listener that records HTTP 500 errors."""

    def _on_response(response):
        if response.status >= 500:
            metrics.http_500_errors.append(
                f"{response.status} {response.url}"
            )

    page.on("response", _on_response)


def _collect_page_crashes(page, metrics: FrontendStressMetrics):
    """Increment crash counter if the page crashes."""

    def _on_crash():
        metrics.page_crashes += 1

    page.on("crash", _on_crash)


def _filter_real_js_errors(metrics: FrontendStressMetrics) -> List[str]:
    """Return only meaningful JS errors, filtering out benign network noise."""
    return [
        e for e in metrics.js_errors
        if "websocket" not in e.lower()
        and "failed to fetch" not in e.lower()
        and "net::err" not in e.lower()
        and "404" not in e.lower()
        and "failed to load resource" not in e.lower()
    ]


def _submit_task_on_main_page(
    page,
    engine: str,
    task: str,
    metrics: FrontendStressMetrics,
    timeout_ms: int = 10_000,
) -> bool:
    """Select engine, fill task, submit, wait for agent steps, then stop.

    Uses the main App page (/) which has the ControlPanel component.
    The frontend keeps agentRunning=true even after agent_finished, so we
    must click Stop to reset the UI for the next iteration.
    Returns True if the submission cycle completed normally.
    """
    try:
        # Ensure WebSocket is connected before submitting (the browser's
        # useWebSocket hook connects to ws://localhost:8000/ws and the header
        # shows "Connected" when the WS is open).
        page.wait_for_function(
            """() => {
                const el = document.querySelector('.header-status');
                return el && el.textContent.includes('Connected');
            }""",
            timeout=timeout_ms,
        )

        # Select engine from the dropdown (3rd .model-select)
        engine_select = page.locator("select.model-select").nth(2)
        engine_select.select_option(engine)

        # Fill the task textarea
        task_input = page.locator("textarea.task-input")
        task_input.fill(task)

        # Click Start Agent button
        start_btn = page.locator("button.btn.btn-primary")
        start_btn.click()

        # Wait for Start button to become disabled (React re-rendered:
        # agentRunning=true, steps cleared). This prevents a race where
        # old action-items from the previous iteration are still visible.
        page.wait_for_function(
            """() => {
                const btn = document.querySelector('button.btn.btn-primary');
                return btn && btn.disabled;
            }""",
            timeout=timeout_ms,
        )

        # Now wait for at least one NEW step to appear
        page.wait_for_function(
            """() => {
                const items = document.querySelectorAll('.action-item');
                return items.length >= 1;
            }""",
            timeout=timeout_ms,
        )

        # Click Stop to reset agentRunning and re-enable the Start button
        stop_btn = page.locator("button.btn.btn-danger")
        stop_btn.click()

        # Wait for Start button to be enabled again
        page.wait_for_function(
            """() => {
                const btn = document.querySelector('button.btn.btn-primary');
                return btn && !btn.disabled;
            }""",
            timeout=timeout_ms,
        )

        metrics.successful_submissions += 1
        return True

    except Exception as exc:
        metrics.errors.append(f"Submission error ({engine}): {exc}")
        # Try to recover by clicking Stop if available
        try:
            stop_btn = page.locator("button.btn.btn-danger")
            if stop_btn.is_enabled():
                stop_btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CLASSES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.stress
@pytest.mark.phase7
class TestFrontendLoadAndRender:
    """Verify the frontend loads without errors."""

    def test_main_page_loads(self, browser_context):
        """The main page at / loads and renders key UI elements."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Header renders
            assert page.locator("header.header").count() > 0

            # The provider select is visible
            assert page.locator("select.model-select").count() >= 1

            # Task textarea is visible
            assert page.locator("textarea.task-input").count() == 1

            # Start button exists
            assert page.locator("button.btn.btn-primary").count() == 1
        finally:
            page.close()

        real_errors = _filter_real_js_errors(metrics)
        assert len(real_errors) == 0, f"JS console errors on load: {real_errors}"

    def test_workbench_page_loads(self, browser_context):
        """The /workbench page loads and renders the sidebar and screen area."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/workbench", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Workbench header
            assert page.locator("header.wb-header").count() > 0

            # Sidebar with config sections
            assert page.locator("aside.wb-sidebar").count() == 1

            # Task textarea
            assert page.locator("textarea.wb-textarea").count() == 1

            # Start button
            assert page.locator("button.wb-btn.wb-btn-primary").count() == 1
        finally:
            page.close()

    def test_all_six_engines_listed(self, browser_context):
        """The engine dropdown on the main page lists all 6 engines."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Engine select is the third .model-select
            engine_select = page.locator("select.model-select").nth(2)
            options = engine_select.locator("option").all_text_contents()
            option_values = engine_select.locator("option").evaluate_all(
                "els => els.map(e => e.value)"
            )

            for eng in ALL_ENGINES:
                assert eng in option_values, \
                    f"Engine {eng!r} not in dropdown: {option_values}"
        finally:
            page.close()

    def test_no_cors_on_api_health(self, browser_context):
        """Fetching /api/health from the frontend does not produce a CORS error."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Manually fetch /api/health from within the page context
            result = page.evaluate(
                """async () => {
                    const r = await fetch('/api/health');
                    return { status: r.status, body: await r.json() };
                }"""
            )
            assert result["status"] == 200
            assert result["body"]["status"] == "ok"
        finally:
            page.close()

        assert len(metrics.cors_errors) == 0, \
            f"CORS errors: {metrics.cors_errors}"


@pytest.mark.stress
@pytest.mark.phase7
class TestEngineSelectionStress:
    """Rapidly select each engine 20 times and submit tasks."""

    @pytest.mark.parametrize("engine", ALL_ENGINES)
    def test_20_submissions_per_engine(self, browser_context, engine):
        """Submit task 20 times for the given engine — all succeed, no errors."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)
        _collect_network_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for run in range(RUNS_PER_ENGINE):
                metrics.total_submissions += 1
                _submit_task_on_main_page(
                    page, engine, SEARCH_PROMPT, metrics, timeout_ms=10_000,
                )
        finally:
            page.close()

        assert metrics.page_crashes == 0, "Frontend crashed during stress run"
        assert len(metrics.cors_errors) == 0, \
            f"CORS errors: {metrics.cors_errors}"
        assert len(metrics.http_500_errors) == 0, \
            f"HTTP 500 errors: {metrics.http_500_errors}"
        assert metrics.successful_submissions == RUNS_PER_ENGINE, \
            f"Only {metrics.successful_submissions}/{RUNS_PER_ENGINE} succeeded: {metrics.errors[:5]}"


@pytest.mark.stress
@pytest.mark.phase7
class TestNoFrontendCrash:
    """Verify the frontend doesn't crash under rapid interaction."""

    def test_rapid_engine_switching(self, browser_context):
        """Switch engines rapidly 60 times — no crash, no JS errors."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            engine_select = page.locator("select.model-select").nth(2)
            for i in range(60):
                eng = ALL_ENGINES[i % len(ALL_ENGINES)]
                engine_select.select_option(eng)
        finally:
            page.close()

        assert metrics.page_crashes == 0
        real_errors = _filter_real_js_errors(metrics)
        assert len(real_errors) == 0, f"JS errors during engine switching: {real_errors}"

    def test_rapid_task_typing(self, browser_context):
        """Type a long task prompt 20 times — no crash."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            task_input = page.locator("textarea.task-input")
            for _ in range(20):
                task_input.fill(SEARCH_PROMPT * 5)  # Long prompt
                task_input.fill("")  # Clear
        finally:
            page.close()

        assert metrics.page_crashes == 0


@pytest.mark.stress
@pytest.mark.phase7
class TestNoCORSErrors:
    """Verify no CORS errors across the test suite."""

    def test_api_calls_no_cors(self, browser_context):
        """Multiple API calls from within the page produce no CORS errors."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            endpoints = [
                "/api/health",
                "/api/container/status",
                "/api/keys/status",
                "/api/agent-service/health",
            ]

            for ep in endpoints:
                result = page.evaluate(
                    f"""async () => {{
                        try {{
                            const r = await fetch('{ep}');
                            return {{ status: r.status, ok: r.ok }};
                        }} catch (e) {{
                            return {{ error: e.message }};
                        }}
                    }}"""
                )
                assert result.get("ok", False) or result.get("status", 0) < 500, \
                    f"Failed fetching {ep}: {result}"
        finally:
            page.close()

        assert len(metrics.cors_errors) == 0, \
            f"CORS errors: {metrics.cors_errors}"


@pytest.mark.stress
@pytest.mark.phase7
class TestNoBackend500:
    """Verify the mock backend never returns HTTP 500."""

    def test_20_start_stop_cycles_no_500(self, browser_context):
        """20 agent start/stop cycles — no 500 errors."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_network_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for _ in range(20):
                # Start agent via API evaluation (bypass UI)
                result = page.evaluate(
                    """async () => {
                        const r = await fetch('/api/agent/start', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                task: 'test task',
                                api_key: 'test-key-0000',
                                model: 'gemini-3-flash-preview',
                                max_steps: 5,
                                mode: 'browser',
                                engine: 'playwright',
                                provider: 'google',
                            }),
                        });
                        return { status: r.status, body: await r.json() };
                    }"""
                )
                assert result["status"] < 500, \
                    f"Backend returned {result['status']}: {result['body']}"
                assert "session_id" in result["body"]

                # Stop it
                sid = result["body"]["session_id"]
                stop_result = page.evaluate(
                    f"""async () => {{
                        const r = await fetch('/api/agent/stop/{sid}', {{
                            method: 'POST',
                        }});
                        return {{ status: r.status, body: await r.json() }};
                    }}"""
                )
                assert stop_result["status"] < 500
        finally:
            page.close()

        assert len(metrics.http_500_errors) == 0


@pytest.mark.stress
@pytest.mark.phase7
class TestOutputJSONValidation:
    """Validate the JSON responses from agent start/status endpoints."""

    def test_start_response_has_session_id(self, browser_context):
        """Every /api/agent/start response contains a valid session_id."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for engine in ALL_ENGINES:
                mode = _engine_mode(engine)
                result = page.evaluate(
                    f"""async () => {{
                        const r = await fetch('/api/agent/start', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{
                                task: 'test',
                                api_key: 'key-0000',
                                model: 'gemini-3-flash-preview',
                                max_steps: 5,
                                mode: '{mode}',
                                engine: '{engine}',
                                provider: 'google',
                            }}),
                        }});
                        return await r.json();
                    }}"""
                )
                assert "session_id" in result, \
                    f"Missing session_id for engine {engine}: {result}"
                assert result["status"] == "running"
                assert result["engine"] == engine
        finally:
            page.close()

    def test_container_status_json_shape(self, browser_context):
        """GET /api/container/status returns {running: bool, agent_service: bool}."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for _ in range(20):
                result = page.evaluate(
                    """async () => {
                        const r = await fetch('/api/container/status');
                        return await r.json();
                    }"""
                )
                assert "running" in result
                assert isinstance(result["running"], bool)
        finally:
            page.close()

    def test_keys_status_json_shape(self, browser_context):
        """GET /api/keys/status returns {keys: [...]} with provider info."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            result = page.evaluate(
                """async () => {
                    const r = await fetch('/api/keys/status');
                    return await r.json();
                }"""
            )
            assert "keys" in result
            assert len(result["keys"]) >= 2
            for key_info in result["keys"]:
                assert "provider" in key_info
                assert "available" in key_info
                assert "source" in key_info
        finally:
            page.close()


@pytest.mark.stress
@pytest.mark.phase7
class TestWebSocketResilience:
    """Verify WebSocket connection and message handling."""

    def test_ws_connects_on_load(self, browser_context):
        """The WebSocket connects automatically when the page loads."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Wait for the WS status indicator to show 'Connected'
            # The header shows "Connected" or "Disconnected" text
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.header-status');
                    return el && el.textContent.includes('Connected');
                }""",
                timeout=10_000,
            )
        finally:
            page.close()

    def test_ws_receives_agent_events(self, browser_context):
        """Starting an agent produces step and agent_finished WS events visible in the UI."""
        _, context = browser_context
        page = context.new_page()

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Wait for WS connection
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.header-status');
                    return el && el.textContent.includes('Connected');
                }""",
                timeout=10_000,
            )

            # Start an agent via API
            page.evaluate(
                """async () => {
                    await fetch('/api/agent/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            task: 'test',
                            api_key: 'key-0000',
                            model: 'gemini-3-flash-preview',
                            max_steps: 5,
                            mode: 'browser',
                            engine: 'playwright',
                            provider: 'google',
                        }),
                    });
                }"""
            )

            # Wait for steps to appear in the action list
            # The ControlPanel renders steps in .action-list
            page.wait_for_function(
                """() => {
                    const items = document.querySelectorAll('.action-item');
                    return items.length >= 1;
                }""",
                timeout=10_000,
            )

            # Verify at least one step rendered
            action_items = page.locator(".action-item").count()
            assert action_items >= 1
        finally:
            page.close()


@pytest.mark.stress
@pytest.mark.phase7
class TestProviderSwitching:
    """Verify switching between Google and Anthropic providers."""

    def test_provider_switch_updates_model(self, browser_context):
        """Switching provider updates the model dropdown — 20 cycles."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Provider select is the first .model-select
            provider_select = page.locator("select.model-select").nth(0)
            # Model select is the second .model-select
            model_select = page.locator("select.model-select").nth(1)

            for i in range(20):
                # Toggle provider
                new_provider = "anthropic" if i % 2 == 0 else "google"
                provider_select.select_option(new_provider)

                # Allow React to re-render
                page.wait_for_timeout(50)

                # Check that model dropdown updated
                model_value = model_select.input_value()
                if new_provider == "anthropic":
                    assert "claude" in model_value, \
                        f"Expected Claude model, got {model_value}"
                else:
                    assert "gemini" in model_value, \
                        f"Expected Gemini model, got {model_value}"
        finally:
            page.close()

        assert metrics.page_crashes == 0
        real_errors = _filter_real_js_errors(metrics)
        assert len(real_errors) == 0


@pytest.mark.stress
@pytest.mark.phase7
class TestWorkbenchStress:
    """Stress test the /workbench page interactions."""

    def test_workbench_engine_submit_cycle(self, browser_context):
        """Submit task from workbench page for each engine — verify no crash."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_console_errors(page, metrics)
        _collect_network_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/workbench", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # On workbench, engine select is .wb-select (3rd one after provider, model)
            engine_select = page.locator("select.wb-select").nth(2)
            task_input = page.locator("textarea.wb-textarea")
            start_btn = page.locator("button.wb-btn.wb-btn-primary")

            # Browser engines only — workbench separates browser/desktop
            browser_engines = ["playwright_mcp", "playwright"]
            for eng in browser_engines:
                for run in range(5):
                    metrics.total_submissions += 1
                    engine_select.select_option(eng)
                    task_input.fill(SEARCH_PROMPT)
                    start_btn.click()

                    # Wait for the button to become enabled again (agent finished)
                    try:
                        page.wait_for_function(
                            """() => {
                                const btn = document.querySelector('button.wb-btn.wb-btn-primary');
                                return btn && !btn.disabled;
                            }""",
                            timeout=10_000,
                        )
                        metrics.successful_submissions += 1
                    except Exception as exc:
                        metrics.errors.append(f"Workbench submit failed ({eng} run {run}): {exc}")
        finally:
            page.close()

        assert metrics.page_crashes == 0
        assert len(metrics.http_500_errors) == 0
        assert metrics.successful_submissions > 0


@pytest.mark.stress
@pytest.mark.phase7
class TestValidationErrors:
    """Verify front-end validation displays error messages."""

    def test_empty_task_shows_error(self, browser_context):
        """Submitting without a task shows an error — no crash, no 500."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_network_errors(page, metrics)
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Click Start without filling any task (API key is env-sourced so not needed)
            start_btn = page.locator("button.btn.btn-primary")
            start_btn.click()

            # The frontend should show "Task description is required" inline
            page.wait_for_function(
                """() => {
                    // UI shows error text in a <p> or container
                    const body = document.body.textContent;
                    return body.includes('Task') && body.includes('required');
                }""",
                timeout=5_000,
            )
        finally:
            page.close()

        assert metrics.page_crashes == 0
        assert len(metrics.http_500_errors) == 0

    def test_rapid_start_clicks_no_crash(self, browser_context):
        """Clicking Start rapidly 20 times without task — no crash."""
        _, context = browser_context
        page = context.new_page()
        metrics = FrontendStressMetrics()
        _collect_page_crashes(page, metrics)

        try:
            page.goto(f"{FRONTEND_URL}/", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            start_btn = page.locator("button.btn.btn-primary")
            for _ in range(20):
                start_btn.click(force=True)  # force=True ignores disabled state
        finally:
            page.close()

        assert metrics.page_crashes == 0
