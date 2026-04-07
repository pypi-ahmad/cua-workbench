# CUA Workbench — Usage Guide

A complete reference for setting up, configuring, and operating CUA Workbench.

---

## Table of Contents

1. [What is CUA Workbench?](#1-what-is-cua-workbench)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
5. [Configuration](#5-configuration)
6. [Running the Stack](#6-running-the-stack)
7. [The Web Interface](#7-the-web-interface)
8. [Automation Engines](#8-automation-engines)
9. [Models & Providers](#9-models--providers)
10. [Session Lifecycle](#10-session-lifecycle)
11. [REST API Reference](#11-rest-api-reference)
12. [WebSocket Events](#12-websocket-events)
13. [Advanced Topics](#13-advanced-topics)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What is CUA Workbench?

CUA Workbench is a local environment for running AI agents that automate real computer tasks inside a visible sandbox. It connects a React control panel, a FastAPI orchestration backend, and a Dockerized Ubuntu 24.04 desktop so you can direct an agent, watch every action it takes, and inspect a complete log of its reasoning.

**What makes it distinct:**

- The desktop is always **visible** through an embedded noVNC pane — not headless.
- **Three execution engines** are shipped and can be selected per session without rebuilding.
- Engine, model, and provider selection is **explicit**. The backend validates the combination and rejects invalid ones; it never silently substitutes an alternative.
- Full session observability: live screenshots, structured per-step logs, and WebSocket event streaming are built in from the start.
- Both Google Gemini and Anthropic Claude are supported, routed through a strict model allowlist.

**Typical use cases:**

| Use case | Example task |
|---|---|
| Web research automation | "Search for the top 5 AI papers published this week and summarize each one" |
| UI testing in a clean environment | "Open the file manager, navigate to /tmp, and verify test fixtures exist" |
| Comparing engine and model behavior | Run the same task on Playwright MCP vs. Computer Use and compare logs |
| Verifying in-container state | Execute shell commands or inspect files through the Desktop accessibility engine |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser  →  Frontend (React 19 + Vite)  :3000                  │
│               Control panel · Screen view · Logs · Timeline      │
└──────────────────┬───────────────────────────────────────────────┘
                   │  HTTP /api/*  +  WebSocket /ws
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Backend (FastAPI + Uvicorn)  :8000                              │
│  Validation · Session orchestration · Docker lifecycle           │
│  Model router (Gemini / Claude) · WebSocket broadcast            │
│  noVNC reverse proxy (/vnc) · WebRTC (optional)                  │
└──────────────────┬───────────────────────────────────────────────┘
                   │  Docker API  +  HTTP to container services
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Ubuntu 24.04 Container  (cua-environment)                       │
│  Xvfb :99 · XFCE4 desktop · AT-SPI2 accessibility bridge        │
│  x11vnc :5900 · noVNC :6080 · Chromium browser                  │
│  Agent Service  :9222  (actions, screenshots, health)            │
│  Playwright MCP :8931  (semantic browser tools)                  │
└──────────────────────────────────────────────────────────────────┘
```

The frontend **never talks to the container directly**. All container traffic — screenshots, noVNC, action results — flows through the backend, keeping the browser on a single origin.

### Port map

| Port | Service | Bound to |
|------|---------|----------|
| `3000` | Vite dev server | localhost |
| `8000` | FastAPI backend | `0.0.0.0` (configurable) |
| `5900` | VNC (x11vnc) | `127.0.0.1` only |
| `6080` | noVNC web | `127.0.0.1` only |
| `8931` | Playwright MCP | `127.0.0.1` only |
| `9222` | Agent Service | `127.0.0.1` only |
| `9223` | Chromium remote debug | `127.0.0.1` only |

---

## 3. Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Docker with a running daemon | 24.x | Docker Desktop on Windows/macOS |
| Python | 3.10 | 3.11+ recommended |
| Node.js | 18 | 20 LTS recommended |
| Disk space | 10 GB free | For the Ubuntu Docker image |

---

## 4. Installation

### Option A — Setup scripts (recommended)

The scripts verify prerequisites, build the image, create a Python virtual environment, and install all dependencies in one pass.

**Linux / macOS:**
```bash
bash setup.sh
```

**Windows (PowerShell):**
```bat
setup.bat
```

### Option B — Manual

```bash
# 1. Build the Docker image (~10 GB, takes several minutes on first run)
docker compose build

# 2. Create a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate.bat       # Windows

# 3. Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Install frontend dependencies
cd frontend && npm install && cd ..
```

### Clean rebuild

Use `--clean` to remove all containers, images, and volumes before rebuilding:

```bash
bash setup.sh --clean    # Linux / macOS
setup.bat --clean         # Windows
```

---

## 5. Configuration

### Environment variables

Create a `.env` file in the repository root. All variables are optional — the app runs with defaults if the file is absent.

```env
# ── API Keys ───────────────────────────────────────────────────────────────
# Resolution order: UI input > .env file > system environment variable
GOOGLE_API_KEY=your-google-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key

# ── Backend ────────────────────────────────────────────────────────────────
HOST=0.0.0.0                   # bind address (use 127.0.0.1 to restrict to localhost)
PORT=8000
DEBUG=0                        # set to 1 for debug logging and hot reload

# ── Container ──────────────────────────────────────────────────────────────
CONTAINER_NAME=cua-environment
AGENT_SERVICE_HOST=127.0.0.1
AGENT_SERVICE_PORT=9222

# ── Playwright MCP ─────────────────────────────────────────────────────────
PLAYWRIGHT_MCP_HOST=localhost
PLAYWRIGHT_MCP_PORT=8931
PLAYWRIGHT_MCP_AUTOSTART=0     # set to 1 to auto-start when container is up

# ── Display ────────────────────────────────────────────────────────────────
SCREEN_WIDTH=1440
SCREEN_HEIGHT=900

# ── Agent tuning ───────────────────────────────────────────────────────────
MAX_STEPS=50                   # default step budget per session
STEP_TIMEOUT=30.0              # seconds before a single step times out
ACTION_DELAY_MS=500            # pause between executed actions

# ── Security ───────────────────────────────────────────────────────────────
VNC_PASSWORD=                  # leave empty to run noVNC without a password
```

> **Security note:** When `VNC_PASSWORD` is unset, noVNC runs without authentication. The UI shows a "⚠ VNC open" badge in the header as a reminder. Set a password in `.env` if the machine is shared.

### Model allowlist

`backend/allowed_models.json` is the single source of truth for which models are available. Both the backend validator and the frontend dropdown read from it. Add or remove entries here to change what models appear in the UI — changes take effect on next backend restart.

```json
{
  "models": [
    {
      "provider": "google",
      "model_id": "gemini-3-flash-preview",
      "display_name": "Gemini 3 Flash Preview",
      "supports_computer_use": true,
      "supports_playwright_mcp": true,
      "supports_accessibility": true
    }
  ]
}
```

---

## 6. Running the Stack

Three processes run on the host, plus the Docker container:

### Step 1 — Start the container

```bash
docker compose up -d
```

The container boots Xvfb, XFCE4, the AT-SPI accessibility bridge, VNC servers, the agent service (`:9222`), and the Playwright MCP server (`:8931`). Allow 10–15 seconds for all services to initialize.

```bash
# Verify the container is running and healthy
docker compose ps

# Stream container startup logs
docker compose logs -f cua-environment
```

### Step 2 — Start the backend

```bash
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate.bat  # Windows

python -m backend.main
```

The API is now available at `http://localhost:8000`. You'll see:
```
INFO: CUA backend starting — model=gemini-3-flash-preview, agent_service=http://127.0.0.1:9222
INFO: Application startup complete.
INFO: Uvicorn running on http://0.0.0.0:8000
```

### Step 3 — Start the frontend

```bash
cd frontend
npm run dev
```

Navigate to **http://localhost:3000**.

---

## 7. The Web Interface

### Header

The top bar shows live system status at a glance:

| Element | Meaning |
|---|---|
| Health dot (green) | All systems healthy (container up + agent service ready) |
| Health dot (yellow) | Degraded — one component is down |
| Health dot (red) | Unhealthy — critical component down |
| ● Container Running | Docker container is up |
| ✕ Container Stopped | Container is not running; click "Start Container" |
| Agent Service Ready / Down | Whether the in-container agent API is responding |
| ⚠ VNC open | VNC is running without a password |
| Status dot + "Connected" | WebSocket to backend is active |
| Status dot + "Agent Running" | A session is in progress |

### Home page — Control panel

The left panel is the main control surface.

**API Configuration**

| Field | Description |
|---|---|
| Provider | Google Gemini or Anthropic Claude |
| Model | Populated from `GET /api/models`; models unsupported by the current engine are greyed out |
| Key source | **Enter key** — type it directly · **From .env file** — uses the .env key · **Environment variable** — uses the shell env. Buttons are disabled when that source has no key |
| API key input | Visible only in "Enter key" mode; validated on blur with a ✓/✗ indicator |

**Engine and advanced options**

Click **Advanced Settings ▶** to reveal:

| Field | Description |
|---|---|
| Run location | **This machine** (local host) or **Docker container** |
| Max steps | Step budget cap (hard server-side limit: 200) |

The engine selector and its one-line help text are always visible above the accordion.

**Task input**

- Write a plain-English description of the task.
- Click a sample chip to prefill the textarea.
- Press **Ctrl+Enter** or **Start Agent (Ctrl+Enter)** to launch.
- A pre-flight check runs before starting and shows warnings (e.g., "API key not configured") without blocking the session.

**Action history**

Every completed step appears in the scrollable action list below the task section. After the session ends, a cost estimate (`~N steps · est. ~$X.XX`) is shown. Click **📦** to export the full session as JSON.

### Workbench page

Navigate to `/workbench` (or click **Open Workbench →**) for the three-pane expert view.

```
┌────────────────┬──────────────────────┬─────────────────────┐
│  Config        │   Live Screen        │  Timeline           │
│  (sidebar)     │   (noVNC or         │  (step-by-step)     │
│                │    screenshot)       │─────────────────────│
│                │                      │  Logs               │
│                │                      │  (agent / container)│
└────────────────┴──────────────────────┴─────────────────────┘
```

**Sidebar** contains the full config form: run mode toggle (Browser / Desktop), provider, model, API key source, and an **Advanced Settings** accordion for engine, run location, and max steps.

**Live Screen** shows the container desktop. When the container is running but the agent service hasn't finished initializing, a spinner with "Waiting for agent service to start…" is shown. Once ready, the screen defaults to the interactive noVNC pane; a **Screenshot** overlay badge is shown when in screenshot-fallback mode.

**Timeline** lists every step with action type, target, and timestamp. Click any step to expand its reasoning, coordinates, and raw JSON.

**Logs panel** has two tabs:
- **Logs** — real-time agent logs with per-level filter toggles (INFO / WARNING / ERROR / DEBUG). Download as `.txt` or export the full session as `.json`.
- **Container** — fetches the last 200 lines from `docker logs` on demand. Click **Refresh** to update.

---

## 8. Automation Engines

### Engine quick-reference

| Engine | UI name | Best for | Execution target |
|---|---|---|---|
| `playwright_mcp` | Browser (Semantic) | Web tasks — navigation, forms, search | `local` or `docker` |
| `omni_accessibility` | Desktop (Accessibility) | Desktop apps — file manager, settings, office | `docker` (Linux AT-SPI) |
| `computer_use` | Computer Use (Native) | Complex browser + desktop with safety gates | `docker` only |

### Browser (Semantic) — `playwright_mcp`

Uses the **Playwright MCP** server to interact with Chromium via its accessibility tree. The model receives a structured text snapshot of the page DOM instead of a screenshot, then references elements by `[ref=...]` identifiers — no pixel coordinates required for most actions.

**Typical actions:**
`browser_navigate`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_snapshot`, `browser_evaluate`, `browser_wait_for`

**Example task:** *"Search Google for 'latest AI research papers' and open the first result"*

**When to choose this engine:** Any task that lives primarily inside a browser. It's the most reliable engine for web automation because it uses structured DOM references rather than visual coordinates.

### Desktop (Accessibility) — `omni_accessibility`

Uses the platform's native accessibility API — **AT-SPI2** on Linux (inside the container), UIAutomation on Windows (local), JXA on macOS (local). The model receives a tree of accessible elements with roles, names, states, and bounding boxes.

**Typical actions:**
`get_accessibility_tree`, `click`, `type`, `scroll`, `open_app`, `run_command`, `find_by_role`, `find_by_text`

**Example task:** *"Open the file manager, navigate to /tmp, and create a file called test.txt"*

**When to choose this engine:** Tasks that involve native desktop applications (file managers, text editors, system utilities) rather than the browser.

### Computer Use (Native) — `computer_use`

Uses the native **computer-use tool protocol** that Gemini 3 and Claude 4.6 models understand directly. The model emits structured CU actions; the backend can interrupt execution and request a **safety confirmation** before executing sensitive actions.

**Execution sub-modes:** `browser` (Playwright) or `desktop` (xdotool + scrot screenshot-driven)

**Coordinate systems:**
- Gemini models: 0–999 normalized (normalized to viewport at runtime)
- Claude models: real pixel coordinates

**Example task:** *"Take a screenshot of the current desktop and describe what is visible"*

**When to choose this engine:** Tasks that require the model to reason visually and issue arbitrary click/type/keyboard actions without a semantic tree, or when the target model's native CU protocol is preferred over the MCP layer.

> **Note:** The backend rejects `execution_target=local` for the Computer Use engine. Set execution target to **Docker container**.

---

## 9. Models & Providers

### Google Gemini

| Model ID | Display name | CU | MCP | Accessibility |
|---|---|---|---|---|
| `gemini-3-flash-preview` | Gemini 3 Flash Preview | ✅ | ✅ | ✅ |
| `gemini-3.1-pro-preview` | Gemini 3.1 Pro Preview | ✅ | ✅ | ✅ |

Get an API key: [aistudio.google.com](https://aistudio.google.com/) → **Get API Key**

### Anthropic Claude

| Model ID | Display name | CU | MCP | Accessibility |
|---|---|---|---|---|
| `claude-sonnet-4-6` | Claude Sonnet 4.6 | ✅ | ✅ | ✅ |
| `claude-opus-4-6` | Claude Opus 4.6 | ✅ | ✅ | ✅ |

Get an API key: [console.anthropic.com](https://console.anthropic.com/) → **Create API Key**

### Cost estimates

The UI shows a rough per-step cost estimate based on the selected model. These are display-only estimates; actual billing depends on token volume.

| Model | Approx. cost per step |
|---|---|
| Gemini 3 Flash | ~$0.003 |
| Gemini 3.1 Pro | ~$0.020 |
| Claude Sonnet | ~$0.015 |
| Claude Opus | ~$0.075 |
| Claude Haiku | ~$0.003 |

---

## 10. Session Lifecycle

### What happens when you click Start

1. **Pre-flight check** — Backend verifies API key availability, container state, and engine validity. Warnings are shown in the UI but never block the session.
2. **Container start** — Backend calls `start_container()` if the container isn't already running.
3. **Input validation** — Engine, provider, model, and task are validated. Rate limit (10/min) and concurrency limit (3 concurrent sessions) are enforced.
4. **Key resolution** — `resolve_api_key(provider, ui_key)`: UI input → `.env` → system env.
5. **Loop start** — `AgentLoop` begins the perceive → think → act cycle up to `max_steps`.
6. **Step execution:**
   - Capture state (accessibility snapshot or screenshot)
   - Query the model (Gemini or Claude) with action history context
   - Validate the returned action against the engine's capability schema
   - Execute the action via the in-container agent service or local Playwright MCP
   - Emit `step`, `log`, and `screenshot` WebSocket events
7. **Termination** — The loop stops when the model returns `done`/`error`, max steps are reached, the user stops the session, or the session is idle for 30 minutes.
8. **`agent_finished` event** — Broadcast to all connected WebSocket clients with session ID, status, and step count.

### Session state

All session state is **in-memory only**. Restarting the backend clears all active sessions. There is no database or persistent session store.

### Idle timeout

Sessions with no step activity for 30 minutes are stopped automatically. Active sessions running at full speed are not affected.

---

## 11. REST API Reference

All paths are prefixed with `/api`. The backend is at `http://localhost:8000` by default.

### Health & discovery

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness probe. Returns `{"status": "ok"}` |
| `GET` | `/api/health/detailed` | Component-level health: Docker, agent service, API key availability. Returns overall `status` (`healthy` / `degraded` / `unhealthy`) |
| `GET` | `/api/models` | Allowlisted models with capability flags |
| `GET` | `/api/engines` | Available engines with display names and categories |
| `GET` | `/api/keys/status` | API key availability and source for each provider |
| `GET` | `/api/preflight` | Pre-flight checklist for a given engine+provider combination |

### Container lifecycle

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/container/status` | Container running state and agent service health |
| `POST` | `/api/container/start` | Start (and build-if-missing) the Docker container |
| `POST` | `/api/container/stop` | Stop active sessions, then stop the container |
| `POST` | `/api/container/build` | Trigger a Docker image rebuild |
| `GET` | `/api/container/logs?lines=N` | Last N lines of container stdout (default 100, max 500) |

### Agent sessions

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/agent/start` | Start a new session |
| `POST` | `/api/agent/stop/{session_id}` | Stop a running session |
| `GET` | `/api/agent/status/{session_id}` | Current status and step count |
| `GET` | `/api/agent/history/{session_id}` | Full step history (no screenshots) |
| `POST` | `/api/agent/safety-confirm` | Resolve a Computer Use safety confirmation prompt |

**Start session — request body:**
```json
{
  "task": "Search for the latest AI news on Google",
  "api_key": "",
  "model": "gemini-3-flash-preview",
  "provider": "google",
  "engine": "playwright_mcp",
  "execution_target": "local",
  "mode": "browser",
  "max_steps": 50
}
```

Leave `api_key` empty to let the backend resolve the key from `.env` or environment.

**Start session — validation rules:**

| Rule | Limit |
|---|---|
| Rate limit | 10 starts per 60 seconds |
| Concurrent sessions | Max 3 |
| Max steps (hard cap) | 200 |
| Engine | Must be in the supported engine list |
| Model | Must be in `allowed_models.json` for the selected provider |
| `computer_use` + `local` | Rejected — must use `docker` target |

### API key validation

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/keys/validate` | Test a key against the provider (5-second timeout). Returns `{"valid": true/false/null}` |

Request body: `{"provider": "google", "api_key": "AIza..."}`

`null` means the validation call timed out or failed — the key may still be valid.

### Error responses

All error responses use a consistent envelope:
```json
{
  "error": "Human-readable message",
  "detail": "Optional additional context",
  "request_id": "UUID of the originating request"
}
```

Every request and response carries an `X-Request-ID` header for log correlation.

---

## 12. WebSocket Events

Connect to `ws://localhost:3000/ws` (via the Vite proxy) or `ws://localhost:8000/ws` directly.

The client sends a heartbeat every 15 seconds:
```json
{ "type": "ping" }
```

The server responds:
```json
{ "event": "pong" }
```

### Event reference

**`screenshot`** / **`screenshot_stream`** — desktop frame
```json
{
  "event": "screenshot",
  "screenshot": "<base64-encoded PNG>"
}
```

**`log`** — structured log entry
```json
{
  "event": "log",
  "log": {
    "timestamp": "2026-04-07T09:15:30.123456Z",
    "level": "info",
    "message": "Step 3: clicked Search button"
  }
}
```

**`step`** — completed action record
```json
{
  "event": "step",
  "step": {
    "step_number": 3,
    "action": "browser_click",
    "target": "[ref=S4]",
    "reasoning": "The Search button is the correct next action",
    "result": "Success",
    "timestamp": "2026-04-07T09:15:31.000Z"
  }
}
```

**`agent_finished`** — session complete
```json
{
  "event": "agent_finished",
  "session_id": "3f8a2c10-...",
  "status": "completed",
  "steps": 12
}
```

Status values: `completed`, `error`, `stopped`, `max_steps_reached`.

---

## 13. Advanced Topics

### Recovery and stuck detection

The agent loop maintains a sliding window of recent actions and applies three automatic recovery strategies:

| Condition | Response |
|---|---|
| Same action repeated 2–3× (based on engine type) | Inject a recovery hint into the next model prompt |
| Consecutive execution errors ≥ 3 | Stop the session with status `error` |
| Identical evaluation results repeated 2× | Issue an ultimatum: the model must return `done` or `error` on the next step |
| `MAX_STUCK_DETECTIONS` (3) exceeded | Force-terminate the loop |

### Engine capability validation

`backend/engine_capabilities.json` declares the exact set of valid actions for each engine. Every action is validated against this schema **before** being dispatched to the container. An action not in the `allowed_actions` list for the selected engine is rejected at the validation layer, not at execution time.

### Dynamic MCP tool discovery

At session start, the agent loop calls `tools/list` on the Playwright MCP server to get the live tool manifest. The **system prompt is generated from this live list**, so the model always has an accurate and up-to-date catalog of what it can do.

### Computer Use safety confirmations

When the Computer Use engine encounters an action that requires explicit user approval (e.g., a `require_confirmation` safety decision), the backend broadcasts a `safety_confirmation` WebSocket event. The session pauses until the frontend calls `POST /api/agent/safety-confirm` with `{"session_id": "...", "confirm": true}`.

### Session export

After a session completes, the **📦 Export** button (home page and Workbench logs panel) downloads a JSON file containing:
- Session config (provider, model, engine, max steps)
- All step records
- All log entries
- Final screenshot (base64 PNG)

Filename format: `CUA_session_YYYYMMDD_HHMMSS.json`

### WebRTC streaming (optional)

For lower-latency video from the container, install the optional AV dependencies:

```bash
pip install aiortc av
```

Then restart the backend. The `POST /webrtc/offer` endpoint becomes active.

### Running tests

```bash
pip install pytest

# Main suite
pytest tests/ -v --ignore=tests/stress

# Stress tests (separate — resource-intensive)
pytest tests/stress/ -v
```

---

## 14. Troubleshooting

### Container won't start

```bash
# Verify the Docker daemon is running
docker info

# Rebuild and start
docker compose build
docker compose up -d

# Check what the container is doing
docker compose ps
docker compose logs -f cua-environment
```

### Backend fails to bind (port already in use)

If port 8000 is blocked or reserved (common with Hyper-V on Windows), start uvicorn on an alternate port and update the Vite proxy:

```bash
python -m uvicorn backend.api.server:app --host 127.0.0.1 --port 8080
```

In `frontend/vite.config.js`, change all three proxy targets from `8000` to `8080`, then restart the frontend.

### UI shows no screenshots or logs

1. Confirm the backend is running: `curl http://localhost:8000/api/health`
2. Check the browser console for WebSocket errors.
3. Verify the Vite dev server is proxying `/ws` correctly (`npm run dev` must be active, not a static build).

### Agent starts but takes no actions

| Symptom | Likely cause |
|---|---|
| "Playwright MCP 403 Forbidden" | Set `PLAYWRIGHT_MCP_HOST=localhost` (not `127.0.0.1`) |
| "MCP not responding" | Check container logs: `docker compose logs cua-environment \| grep -i mcp` |
| "AT-SPI bus returned no applications" | Desktop is still initializing; wait 15–20s and retry |
| "Agent service not responding" | Run `docker compose exec cua-environment curl http://localhost:9222/health` |

### API key is not found

1. Check resolution order: UI input → `.env` → system env.
2. Call `GET /api/keys/status` to see what the backend detects.
3. In `.env`, use `KEY=value` with no quotes: `GOOGLE_API_KEY=AIzaSy...`
4. Make sure `.env` is in the **repository root**, not inside `backend/` or `frontend/`.

### Computer Use engine is rejected

The backend rejects `execution_target=local` for the `computer_use` engine. Set **Run location** to **Docker container** in the Advanced Settings accordion.

### Step timeout errors

Increase the per-step timeout in `.env`:
```env
STEP_TIMEOUT=60.0
```

### noVNC view is blank or shows "noVNC not available yet"

1. Verify the container is running: `docker compose ps`
2. Check that port 6080 is reachable from the backend host: `curl http://127.0.0.1:6080`
3. Allow 10–20 seconds after `docker compose up` for noVNC to initialize.

### Debug mode

Set `DEBUG=1` in `.env` for verbose logging and uvicorn hot reload:
```env
DEBUG=1
```

---

*For architecture diagrams, component maps, and the full engine compatibility matrix, see the [README](../README.md).*

