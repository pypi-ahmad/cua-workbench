"""FastAPI application — REST endpoints + WebSocket streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import config, get_all_key_statuses, resolve_api_key
from pydantic import BaseModel
from backend.models import (
    AgentAction,
    StartTaskRequest,
    TaskStatusResponse,
)
from backend.agent.loop import AgentLoop
from backend.agent.screenshot import capture_screenshot, check_service_health
from backend.tools.router import SUPPORTED_ENGINES
from backend.utils.docker_manager import (
    build_image,
    get_container_status,
    is_container_running,
    start_container,
    stop_container,
)
from backend.utils.parity_check import validate_tool_parity

import httpx

logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="CUA — Computer Using Agent", version="1.0.0")


# ── B-28: Request-ID middleware ───────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request/response cycle."""

    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


app.add_middleware(RequestIDMiddleware)


# CORS: restrict to local dev origins by default
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-ID"],
)

# Security constants
_MAX_CONCURRENT_SESSIONS = 3
_MAX_STEPS_HARD_CAP = 200
# Engine list is the single source of truth from the router module
_VALID_ENGINES = SUPPORTED_ENGINES
_VALID_PROVIDERS = {"google", "anthropic"}

# ── Allowed models (single source of truth: backend/allowed_models.json) ──────

def _load_allowed_models() -> list[dict]:
    """Load the canonical model allowlist from allowed_models.json."""
    import json as _json
    from pathlib import Path as _Path
    _fpath = _Path(__file__).resolve().parent.parent / "allowed_models.json"
    with open(_fpath, encoding="utf-8") as f:
        data = _json.load(f)
    return data.get("models", [])

_ALLOWED_MODELS: list[dict] = _load_allowed_models()

_VALID_MODELS_BY_PROVIDER: Dict[str, set[str]] = {}
for _m in _ALLOWED_MODELS:
    _VALID_MODELS_BY_PROVIDER.setdefault(_m["provider"], set()).add(_m["model_id"])


# ── Rate limiter (in-memory sliding window) ───────────────────────────────────

class _RateLimiter:
    """Simple sliding-window rate limiter (no external deps)."""
    def __init__(self, max_calls: int, window_seconds: float):
        """Configure the limiter with *max_calls* per *window_seconds*."""
        self._max = max_calls
        self._window = window_seconds
        self._calls: list[float] = []

    def allow(self) -> bool:
        """Return True and record a call if under the rate limit."""
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self._window]
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


_agent_start_limiter = _RateLimiter(max_calls=10, window_seconds=60.0)


def _is_valid_uuid(value: str) -> bool:
    """Return True if *value* is a well-formed UUID."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


@app.on_event("startup")
async def on_startup():
    """Run tool parity check and log configuration on startup."""
    logger.info("CUA backend starting — model=%s, agent_service=%s, mode=%s",
                config.gemini_model, config.agent_service_url, config.agent_mode)

    # Validate tool parity on startup
    try:
        validate_tool_parity()
    except Exception as e:
        logger.warning("Tool parity check failed: %s", e)

    # B-19: Start idle-session reaper
    asyncio.create_task(_reap_idle_sessions())


async def _reap_idle_sessions() -> None:
    """Background task: stop sessions idle for more than the timeout."""
    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.monotonic()
        for sid in list(_session_last_activity.keys()):
            if sid not in _active_loops:
                _session_last_activity.pop(sid, None)
                continue
            elapsed = now - _session_last_activity.get(sid, now)
            if elapsed > _SESSION_IDLE_TIMEOUT:
                logger.info("AUDIT session_timeout — session_id=%s idle=%.0fs", sid, elapsed)
                await _stop_agent(sid)
                _session_last_activity.pop(sid, None)


@app.on_event("shutdown")
async def on_shutdown():
    """Cancel running agents and clean up WebRTC connections."""
    # Cancel all running agent tasks
    for sid in list(_active_tasks.keys()):
        task = _active_tasks.get(sid)
        if task and not task.done():
            task.cancel()
    _active_tasks.clear()
    _active_loops.clear()

    # Cleanup WebRTC connections
    try:
        from backend.streaming.webrtc_server import manager
        await manager.cleanup()
    except ImportError as e:
        logger.debug("WebRTC cleanup skipped (import unavailable): %s", e)
    except Exception as e:
        logger.warning("WebRTC cleanup error: %s", e)

    logger.info("CUA backend shut down")

# ── In-memory state ──────────────────────────────────────────────────────────

_active_loops: dict[str, AgentLoop] = {}
_active_tasks: dict[str, asyncio.Task] = {}
_ws_clients: list[WebSocket] = []

# B-19: Session idle timeout tracking  (last-activity timestamp per session)
_SESSION_IDLE_TIMEOUT = 30 * 60  # 30 minutes
_session_last_activity: dict[str, float] = {}


def _touch_session(session_id: str) -> None:
    """Update the last-activity timestamp for *session_id*."""
    _session_last_activity[session_id] = time.monotonic()


# ── B-14: Standardized error envelope ─────────────────────────────────────────

def _error_response(
    status_code: int,
    message: str,
    *,
    detail: str | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """Return a uniform ``{error, detail?, request_id?}`` JSON error."""
    body: dict = {"error": message}
    if detail:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(status_code=status_code, content=body)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _broadcast(event: str, data: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    msg = json.dumps({"event": event, **data})
    stale: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            stale.append(ws)
    for ws in stale:
        _ws_clients.remove(ws)


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


class WebRTCOffer(BaseModel):
    """SDP offer payload for WebRTC negotiation."""

    sdp: str
    type: str

@app.post("/webrtc/offer")
async def webrtc_offer(offer: WebRTCOffer):
    """Accept a WebRTC SDP offer and return the SDP answer."""
    from backend.streaming.webrtc_server import manager
    try:
        answer = await manager.handle_offer(offer.sdp, offer.type)
        return answer
    except Exception as e:
        logger.error("WebRTC offer failed: %s", e)
        return {"error": str(e)}

@app.get("/api/models")
async def api_models():
    """Return the canonical allowed-models list for frontend dropdowns.

    Source of truth: backend/allowed_models.json
    """
    return {"models": _ALLOWED_MODELS}

@app.get("/api/engines")
async def api_engines():
    """Return available engines for frontend dropdowns."""
    from backend.engine_capabilities import EngineCapabilities

    # B-01: Clean, emoji-free display names for the UI
    _DISPLAY_NAMES = {
        "playwright_mcp": "Browser Automation",
        "omni_accessibility": "Desktop Automation",
        "computer_use": "Full Screen Control",
    }

    caps = EngineCapabilities()
    engines = []
    for name in caps.engine_names:
        schema = caps.get_engine(name)
        if schema is None:
            continue
        cats = set(schema.categories.keys()) if isinstance(schema.categories, dict) else set()
        category = "browser" if ("dom" in cats or "javascript" in cats) else "desktop"
        engines.append({
            "value": name,
            "label": _DISPLAY_NAMES.get(name, schema.display_name),
            "category": category,
            "priority": schema.fallback_priority,
        })

    engines.sort(key=lambda e: e["priority"])
    return {"engines": engines}


@app.get("/api/container/status")
async def container_status():
    """Return Docker container and agent-service status."""
    return await get_container_status()


@app.get("/api/agent-service/health")
async def agent_service_health():
    """Check if the internal Playwright agent service is responding."""
    healthy = await check_service_health()
    return {"healthy": healthy, "url": config.agent_service_url}


@app.post("/api/agent-service/mode")
async def set_agent_mode(body: dict):
    """Switch the agent service between browser and desktop mode at runtime."""
    import httpx
    mode = body.get("mode", "browser")
    if mode not in ("browser", "desktop"):
        return {"error": "mode must be 'browser' or 'desktop'"}
    url = f"{config.agent_service_url}/mode"
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(url, json={"mode": mode})
            return resp.json()
        except Exception as e:
            return {"error": str(e)}


@app.post("/api/container/start")
async def api_start_container():
    """Build-if-needed and start the CUA Docker container."""
    success = await start_container()
    return {"success": success}


@app.post("/api/container/stop")
async def api_stop_container():
    """Stop all running agents then remove the Docker container."""
    for sid in list(_active_tasks.keys()):
        await _stop_agent(sid)
    success = await stop_container()
    return {"success": success}


@app.post("/api/container/build")
async def api_build_image():
    """Trigger a Docker image build."""
    success = await build_image()
    return {"success": success}


@app.get("/api/keys/status")
async def api_keys_status():
    """Return availability and source of API keys for all providers.

    Response example::

        {
          "keys": [
            {"provider": "google",    "available": true, "source": "env",    "masked_key": "AIza...4xQk"},
            {"provider": "anthropic", "available": true, "source": "dotenv", "masked_key": "sk-a...9f2e"}
          ]
        }

    Sources: ``"env"`` = system environment variable, ``"dotenv"`` = .env file,
    ``"none"`` = not configured.
    """
    return {"keys": get_all_key_statuses()}


class ValidateKeyRequest(BaseModel):
    """Body for the key validation endpoint."""
    provider: str
    api_key: str


@app.post("/api/keys/validate")
async def api_validate_key(req: ValidateKeyRequest):
    """Validate an API key by making a minimal test call to the provider.

    Returns ``{"valid": true}`` or ``{"valid": false, "error": "..."}``
    with a 5-second timeout.
    """
    if req.provider not in _VALID_PROVIDERS:
        return {"valid": False, "error": f"Unknown provider: {req.provider}"}
    if not req.api_key or len(req.api_key) < 8:
        return {"valid": False, "error": "Key too short"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if req.provider == "google":
                # Test Google key by listing models
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": req.api_key},
                )
                if resp.status_code == 200:
                    return {"valid": True}
                return {"valid": False, "error": "Invalid Google API key"}
            elif req.provider == "anthropic":
                # Test Anthropic key with a minimal request
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": req.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                # 200 or 400 (invalid request but key is valid) means key works
                if resp.status_code in (200, 400):
                    return {"valid": True}
                if resp.status_code == 401:
                    return {"valid": False, "error": "Invalid Anthropic API key"}
                return {"valid": True}  # Other status codes likely mean key is ok
    except httpx.TimeoutException:
        return {"valid": None, "error": "Validation timed out — could not verify key"}
    except Exception as e:
        logger.debug("Key validation error: %s", e)
        return {"valid": None, "error": "Could not verify key"}

    return {"valid": None, "error": "Unknown provider"}


@app.get("/api/health/detailed")
async def api_health_detailed():
    """Return component-level health status for the system.

    Response::

        {
          "status": "healthy|degraded|unhealthy",
          "components": {
            "docker": {"status": "healthy", "message": "..."},
            "agent_service": {"status": "healthy"|"unhealthy"|"unknown"},
            "api_keys": {"google": true, "anthropic": false}
          },
          "active_sessions": 0
        }
    """
    components = {}

    # Docker container
    try:
        container_info = await get_container_status()
        running = container_info.get("running", False)
        components["docker"] = {
            "status": "healthy" if running else "stopped",
            "running": running,
        }
    except Exception:
        components["docker"] = {"status": "unknown", "running": False}

    # Agent service
    try:
        healthy = await check_service_health()
        components["agent_service"] = {
            "status": "healthy" if healthy else "unhealthy",
        }
    except Exception:
        components["agent_service"] = {"status": "unknown"}

    # API keys
    key_statuses = get_all_key_statuses()
    components["api_keys"] = {
        ks["provider"]: ks["available"] for ks in key_statuses
    }

    # Active sessions
    active_count = sum(1 for t in _active_tasks.values() if not t.done())

    # Overall status
    docker_ok = components["docker"]["status"] == "healthy"
    agent_ok = components["agent_service"]["status"] == "healthy"
    if docker_ok and agent_ok:
        overall = "healthy"
    elif docker_ok or agent_ok:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return {
        "status": overall,
        "components": components,
        "active_sessions": active_count,
        "vnc_protected": bool(config.vnc_password),
    }


@app.get("/api/preflight")
async def api_preflight(engine: str = "playwright_mcp", provider: str = "google"):
    """Run a pre-flight checklist before starting an agent session.

    Returns a list of checks with pass/fail status.
    """
    checks = []

    # 1. API key available
    resolved_key, key_source = resolve_api_key(provider, None)
    checks.append({
        "name": "api_key",
        "label": f"{provider.capitalize()} API key",
        "ok": bool(resolved_key and len(resolved_key) >= 8),
        "message": f"Found ({key_source})" if resolved_key else "Not configured",
    })

    # 2. Container running
    try:
        running = await is_container_running()
        checks.append({
            "name": "container",
            "label": "Docker container",
            "ok": running,
            "message": "Running" if running else "Not running — will be started automatically",
        })
    except Exception:
        checks.append({
            "name": "container",
            "label": "Docker container",
            "ok": False,
            "message": "Could not check container status",
        })

    # 3. Agent service health
    try:
        healthy = await check_service_health()
        checks.append({
            "name": "agent_service",
            "label": "Agent service",
            "ok": healthy,
            "message": "Ready" if healthy else "Not responding",
        })
    except Exception:
        checks.append({
            "name": "agent_service",
            "label": "Agent service",
            "ok": False,
            "message": "Could not check",
        })

    # 4. Engine supported
    engine_ok = engine in _VALID_ENGINES
    checks.append({
        "name": "engine",
        "label": f"Engine: {engine}",
        "ok": engine_ok,
        "message": "Available" if engine_ok else "Unknown engine",
    })

    all_ok = all(c["ok"] for c in checks)
    return {"ready": all_ok, "checks": checks}


@app.get("/api/container/logs")
async def api_container_logs(lines: int = 100):
    """Return recent container logs (last N lines)."""
    import subprocess
    lines = min(max(lines, 1), 500)  # Cap at 500 lines
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), config.container_name],
            capture_output=True, text=True, timeout=5,
        )
        return {
            "logs": result.stdout[-10000:] if result.stdout else "",  # Cap output size
            "stderr": result.stderr[-5000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"logs": "", "stderr": "Timed out reading container logs"}
    except FileNotFoundError:
        return {"logs": "", "stderr": "Docker CLI not found"}
    except Exception as e:
        return {"logs": "", "stderr": str(e)}


@app.get("/api/screenshot")
async def api_screenshot():
    """Get current screenshot as base64."""
    try:
        b64 = await capture_screenshot()
        return {"screenshot": b64}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/agent/start")
async def api_start_agent(req: StartTaskRequest, request: Request):
    """Start a new agent session with input validation."""
    rid = getattr(request.state, "request_id", None)

    # ── Rate limit ────────────────────────────────────────────────────
    if not _agent_start_limiter.allow():
        return _error_response(429, "Too many sessions started — wait a minute and try again.", request_id=rid)

    # ── Validate inputs ───────────────────────────────────────────────
    if req.engine not in _VALID_ENGINES:
        return _error_response(400, f"Invalid engine: {req.engine}", request_id=rid)
    if req.provider not in _VALID_PROVIDERS:
        return _error_response(400, f"Invalid provider: {req.provider}", request_id=rid)
    if req.model not in _VALID_MODELS_BY_PROVIDER.get(req.provider, set()):
        allowed = ", ".join(m["model_id"] for m in _ALLOWED_MODELS)
        return _error_response(400, f"Model '{req.model}' is not allowed. Supported models: {allowed}", request_id=rid)
    if not req.task or not req.task.strip():
        return _error_response(400, "Describe what the agent should do.", request_id=rid)

    # Resolve API key: UI input → .env → system env
    resolved_key, key_source = resolve_api_key(req.provider, req.api_key)
    if not resolved_key or len(resolved_key) < 8:
        return _error_response(400, "API key is required. Provide it in the UI, .env file, or system environment variable.", request_id=rid)

    # Cap max_steps to prevent runaway agents
    req.max_steps = min(req.max_steps, _MAX_STEPS_HARD_CAP)

    # Limit concurrent sessions
    active_count = sum(1 for t in _active_tasks.values() if not t.done())
    if active_count >= _MAX_CONCURRENT_SESSIONS:
        return _error_response(429, f"Maximum {_MAX_CONCURRENT_SESSIONS} concurrent sessions allowed.", request_id=rid)

    # Audit log (mask API key)
    masked_key = resolved_key[:4] + "..." + resolved_key[-4:] if len(resolved_key) > 8 else "****"
    logger.info("AUDIT agent/start — task=%r engine=%s provider=%s model=%s key=%s source=%s target=%s",
                req.task[:80], req.engine, req.provider, req.model, masked_key, key_source,
                req.execution_target)

    # Validate execution_target ("local" | "docker")
    execution_target = req.execution_target if req.execution_target in ("local", "docker") else "local"

    # Guard: computer_use engine only supports Docker target for now
    # Refs:
    #   Gemini CU docs: https://ai.google.dev/gemini-api/docs/computer-use
    #   Anthropic CU docs: https://platform.claude.com/docs/en/docs/agents-and-tools/computer-use
    if req.engine == "computer_use" and execution_target == "local":
        return _error_response(400,
            "Computer Use engine requires the Docker container. "
            "Select 'Docker container' as the run location, or switch to the Browser engine.",
            request_id=rid,
        )

    container_ok = await start_container()
    if not container_ok:
        return _error_response(500, "Failed to start Docker container", request_id=rid)

    loop = AgentLoop(
        task=req.task,
        api_key=resolved_key,
        model=req.model,
        max_steps=req.max_steps,
        mode=req.mode,
        engine=req.engine,
        provider=req.provider,
        execution_target=execution_target,
        on_log=lambda entry: asyncio.ensure_future(
            _broadcast("log", {"log": entry.model_dump()})
        ),
        on_step=lambda step: (
            _touch_session(loop.session_id),  # B-19: reset idle timer
            asyncio.ensure_future(
                _broadcast("step", {
                    "step": step.model_dump(exclude={"screenshot_b64", "raw_model_response"}),
                })
            ),
        )[-1],
        on_screenshot=lambda b64: asyncio.ensure_future(
            _broadcast("screenshot", {"screenshot": b64})
        ),
        on_safety_broadcast=lambda sid, explanation: _broadcast(
            "safety_confirmation",
            {"session_id": sid, "explanation": explanation},
        ),
        safety_events=_safety_events,
        safety_decisions=_safety_decisions,
    )

    _active_loops[loop.session_id] = loop
    _touch_session(loop.session_id)  # B-19: start idle timer

    async def _run_and_notify():
        """Run the agent loop then broadcast a finish event to all WS clients."""
        session = await loop.run()
        await _broadcast("agent_finished", {
            "session_id": loop.session_id,
            "status": session.status.value,
            "steps": len(session.steps),
        })
        # Cleanup bookkeeping so status endpoint reflects completion
        _active_tasks.pop(loop.session_id, None)
        _active_loops.pop(loop.session_id, None)
        _session_last_activity.pop(loop.session_id, None)  # B-19

    task = asyncio.create_task(_run_and_notify())
    _active_tasks[loop.session_id] = task

    logger.info("AUDIT session_started — session_id=%s engine=%s", loop.session_id, req.engine)

    return {
        "session_id": loop.session_id,
        "status": "running",
        "mode": req.mode,
        "engine": req.engine,
        "provider": req.provider,
    }


@app.post("/api/agent/stop/{session_id}")
async def api_stop_agent(session_id: str):
    """Stop a running agent session by ID."""
    if not _is_valid_uuid(session_id):
        return {"error": "Invalid session_id"}
    return await _stop_agent(session_id)


async def _stop_agent(session_id: str) -> dict:
    """Internal helper to cancel an agent loop and its asyncio task."""
    loop = _active_loops.get(session_id)
    if not loop:
        return {"error": "Session not found"}

    loop.request_stop()

    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    _active_tasks.pop(session_id, None)
    _active_loops.pop(session_id, None)
    logger.info("AUDIT session_stopped — session_id=%s", session_id)
    return {"session_id": session_id, "status": "stopped"}


@app.get("/api/agent/status/{session_id}")
async def api_agent_status(session_id: str):
    """Return the current status of an agent session."""
    if not _is_valid_uuid(session_id):
        return {"error": "Invalid session_id"}
    loop = _active_loops.get(session_id)
    if not loop:
        return {"error": "Session not found"}

    session = loop.session
    last_action: Optional[AgentAction] = None
    if session.steps:
        last_action = session.steps[-1].action

    return TaskStatusResponse(
        session_id=session.session_id,
        status=session.status,
        current_step=len(session.steps),
        total_steps=session.max_steps,
        last_action=last_action,
    ).model_dump()


# ── Safety Confirmation for CU Engine ─────────────────────────────────────────

# Maps session_id → asyncio.Event that _run_computer_use_engine can await.
# The companion dict stores the user's decision (True = confirm, False = deny).
_safety_events: Dict[str, asyncio.Event] = {}
_safety_decisions: Dict[str, bool] = {}


class SafetyConfirmRequest(BaseModel):
    """Body for the safety-confirm endpoint."""
    session_id: str
    confirm: bool = False


@app.post("/api/agent/safety-confirm")
async def api_agent_safety_confirm(req: SafetyConfirmRequest):
    """Respond to a CU safety_decision / require_confirmation prompt.

    When the native Computer Use engine encounters a
    ``require_confirmation`` safety decision, it broadcasts a
    ``safety_confirmation`` event via WebSocket.  The frontend displays
    a dialog and calls this endpoint with the user's decision.

    The AgentLoop can check ``_safety_events[session_id]`` to unblock.
    """
    sid = req.session_id
    if not _is_valid_uuid(sid):
        return {"error": "Invalid session_id"}
    if sid not in _active_loops:
        return {"error": "Session not found"}

    # Store the decision and signal the waiting loop
    _safety_decisions[sid] = req.confirm
    evt = _safety_events.get(sid)
    if evt is None:
        evt = asyncio.Event()
        _safety_events[sid] = evt
    evt.set()

    logger.info("AUDIT safety_confirm — session_id=%s confirm=%s", sid, req.confirm)
    return {"session_id": sid, "confirmed": req.confirm}


@app.get("/api/agent/history/{session_id}")
async def api_agent_history(session_id: str):
    """Return the full step history for a session (without screenshots)."""
    if not _is_valid_uuid(session_id):
        return {"error": "Invalid session_id"}
    loop = _active_loops.get(session_id)
    if not loop:
        return {"error": "Session not found"}

    steps = [s.model_dump(exclude={"screenshot_b64"}) for s in loop.session.steps]
    return {"session_id": session_id, "steps": steps}


# ── noVNC Reverse Proxy ───────────────────────────────────────────────────────
# Proxy requests so the frontend never hits Docker-mapped ports directly.

_NOVNC_HTTP = "http://127.0.0.1:6080"
_NOVNC_WS   = "ws://127.0.0.1:6080"


@app.websocket("/vnc/websockify")
async def vnc_ws_proxy(ws: WebSocket):
    """Proxy the noVNC WebSocket to the container's websockify."""
    await ws.accept()
    try:
        import websockets
        async with websockets.connect(
            f"{_NOVNC_WS}/websockify",
            subprotocols=["binary"],
            max_size=2**22,
        ) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive_bytes()
                        await upstream.send(data)
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as exc:
        logger.debug("VNC WebSocket proxy closed: %s", exc)


@app.get("/vnc/{path:path}")
async def vnc_http_proxy(path: str):
    """Proxy noVNC static files from the container's websockify web server."""
    from starlette.responses import Response
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{_NOVNC_HTTP}/{path}")
            content_type = resp.headers.get("content-type", "application/octet-stream")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError):
            return Response(content="noVNC not available yet", status_code=502)


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Accept a WebSocket connection for real-time event streaming."""
    await ws.accept()
    _ws_clients.append(ws)
    logger.info("WebSocket client connected (%d total)", len(_ws_clients))

    streaming_task: asyncio.Task | None = None
    try:
        streaming_task = asyncio.create_task(_stream_screenshots(ws))

        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"event": "pong"}))
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        if streaming_task:
            streaming_task.cancel()


async def _stream_screenshots(ws: WebSocket):
    """Periodically send screenshots to a specific WS client.

    Uses 'desktop' mode (scrot) so the full X11 display is visible,
    including the browser window, desktop background, and taskbar.
    Skips capture attempts when the container is not running to avoid
    spamming warnings.
    """
    from backend.utils.docker_manager import is_container_running

    while True:
        try:
            await asyncio.sleep(config.ws_screenshot_interval)
            # Only attempt capture when the container is actually running
            if not await is_container_running():
                continue
            b64 = await capture_screenshot(mode="desktop")
            await ws.send_text(json.dumps({
                "event": "screenshot_stream",
                "screenshot": b64,
            }))
        except asyncio.CancelledError:
            break
        except WebSocketDisconnect:
            break
        except Exception:
            await asyncio.sleep(2)
