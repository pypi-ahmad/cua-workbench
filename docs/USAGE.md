# CUA Workbench — Usage Guide

CUA Workbench is a local operator console for running AI agents that control a browser or desktop inside a visible sandbox. You describe a task in plain language, choose a provider and model, and the agent executes it step by step while you watch.

This document covers every part of the application as it works today.

---

## Table of Contents

1. [Who This App Is For](#who-this-app-is-for)
2. [Before You Start](#before-you-start)
3. [Setup and Prerequisites](#setup-and-prerequisites)
4. [How to Start the App](#how-to-start-the-app)
5. [Main User Workflow](#main-user-workflow)
6. [Feature Guide](#feature-guide)
   - [Landing Page](#landing-page)
   - [Workbench — Status Header](#workbench--status-header)
   - [Workbench — Configuration Sidebar](#workbench--configuration-sidebar)
   - [Workbench — Live Screen](#workbench--live-screen)
   - [Workbench — Timeline](#workbench--timeline)
   - [Workbench — Logs Panel](#workbench--logs-panel)
7. [Automation Engines](#automation-engines)
8. [Input Expectations](#input-expectations)
9. [Output and Export](#output-and-export)
10. [Session Persistence](#session-persistence)
11. [Safety Confirmation](#safety-confirmation)
12. [Configuration Reference](#configuration-reference)
13. [Backend API Reference](#backend-api-reference)
14. [Troubleshooting](#troubleshooting)
15. [Limitations and Important Notes](#limitations-and-important-notes)

---

## Who This App Is For

CUA Workbench is suitable for:

- Engineers building or testing AI agent workflows
- Developers who need to inspect step-by-step agent behavior against a visible Linux sandbox
- QA or automation engineers comparing browser and desktop execution strategies
- Anyone who wants to run browser or desktop automation without writing code

This is a local single-user workbench. There are no accounts, no server-side history, and no hosted infrastructure.

---

## Before You Start

1. **Docker is required** for the sandbox. The UI will load without it, but the agent will not be able to execute against a real environment.
2. **At least one API key is required**: a Google Gemini key (`GOOGLE_API_KEY`) or an Anthropic Claude key (`ANTHROPIC_API_KEY`). You can provide these in a `.env` file, as a system environment variable, or paste the key directly into the UI each session.
3. **Python 3.10+** and **Node.js 18+** must be installed.
4. The setup script (`setup.sh` or `setup.bat`) handles all first-run preparation including building the Docker image. Run it once before starting the app for the first time.

---

## Setup and Prerequisites

### Required software

| Dependency | Minimum version |
|---|---|
| Docker (with daemon running) | Any recent stable release |
| Python | 3.10+ |
| Node.js | 18+ |

### First-time setup

**Linux / macOS:**

```bash
bash setup.sh
```

**Windows:**

```bat
setup.bat
```

What these scripts do:

- Verify Docker, Python, and Node.js are available and the Docker daemon is running
- Check for at least 10 GB of free disk space before proceeding
- Build the Docker image (`cua-ubuntu:latest`) from the local `docker/Dockerfile`
- Create a Python virtual environment at `.venv` and install `requirements.txt` into it
- Run `npm install` inside `frontend/`

To tear down Docker state and start fresh:

```bash
bash setup.sh --clean
```

```bat
setup.bat --clean
```

`--clean` runs `docker compose down --rmi all -v` and `docker system prune -a --volumes -f`. This is destructive and will remove all Docker images and volumes on your machine.

### Manual setup (without the setup scripts)

```bash
# Build image
docker compose build

# Create and populate virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Install frontend dependencies
cd frontend && npm install && cd ..
```

---

## How to Start the App

### Recommended: use the launch scripts

**Linux / macOS:**

```bash
./start.sh
```

**Windows:**

```bat
start.bat
```

Both scripts:

- Check that Python and Node.js are available
- Auto-install Python and Node dependencies if missing
- Start the backend on **`http://localhost:8000`**
- Start the Vite dev server; the frontend is available at **`http://localhost:3000`**
- Print both URLs in the terminal
- Do **not** start or stop the Docker sandbox — that is controlled from within the app itself

To stop all processes:

```bash
./start.sh --stop
```

```bat
start.bat --stop
```

> **Note:** The launch scripts run `python` / `python3` directly. They do not activate the `.venv` created by `setup.sh`. If you used `setup.sh` and want to use that virtual environment, activate it yourself before running the launch script, or ensure the same packages are available in your default Python installation.

### Manual start

If you prefer to start processes individually:

```bash
# Activate the virtual environment created by setup.sh
source .venv/bin/activate    # Windows: .venv\Scripts\activate.bat

# Backend
python -m backend.main       # Starts on port 8000 by default

# Frontend (separate terminal)
cd frontend && npm run dev   # Starts on port 3000
```

The Vite dev server proxies `/api`, `/ws`, and `/vnc` to `http://localhost:8000`.

---

## Main User Workflow

### 1. Open the app

Navigate to [http://localhost:3000](http://localhost:3000).

The landing page shows a brief product overview. Click **Open Workbench →** to go to the main workspace.

### 2. Start the sandbox (first run)

The header shows the current sandbox status. If the sandbox is offline, click **Start Sandbox**. The backend will start the Docker container and display progress in the backend terminal. Wait for the status to change to **Sandbox Ready**.

If this is your first run after setup, the Docker image should already be built. If not, run `docker compose build` first.

### 3. Choose a provider and model

In the Workbench sidebar:

- Select **Google Gemini** or **Anthropic Claude** from the Provider dropdown
- Select a model from the Model dropdown (models are loaded from the backend at runtime)

### 4. Provide an API key

Choose how to supply your key using the two-button API Key Source toggle:

- **Enter manually** — type or paste a key into the field
- **Saved key ✓** — use a key already available from a `.env` file or system environment variable (the button is disabled if no saved key is found for the selected provider)

A "Get an API key ↗" link is shown below the input for each provider.

Keys entered manually are validated on the server (5-second timeout) when you move focus away from the field. A check mark or cross indicates the validation result.

### 5. Choose a run mode and engine

The **Run Mode** toggle at the top of the sidebar sets the default engine:

- **Browser** — defaults to Browser Automation (Playwright MCP)
- **Desktop** — defaults to Full Screen Control (computer use)

You can override the specific engine under **Advanced Settings**.

### 6. Enter a task

Type a plain-language task description in the Task field. If the field is empty, sample tasks appear as quick-fill chips:

- "Search Google for 'latest AI news'"
- "Open the file manager and list files in /tmp"
- "Take a screenshot of the desktop"
- "Go to wikipedia.org and find the main page featured article"

### 7. Start the agent

Click **Start Agent** or press **Ctrl+Enter**. Before starting, the frontend runs a pre-flight check. Any warnings are shown, but they do not block the session.

If the sandbox was not running when you started, the backend will attempt to start the container automatically before the agent begins.

### 8. Watch the run

While the agent is running:

- The center panel shows either an **interactive noVNC desktop** or a live **screenshot** updated in real time
- The **Timeline** panel shows each action as it executes, with human-readable labels (e.g., "Click", "Type text", "Open URL")
- The **Logs** panel streams backend messages as they arrive

### 9. Stop or let it finish

- Click **Stop** to cancel a running session. A confirmation dialog will appear before the session is cancelled.
- The agent stops automatically when it completes the task, reaches the max-steps limit, or encounters an unrecoverable error.
- When a session ends, a **completion card** appears showing the outcome, step count, and estimated cost.

### 10. Inspect and export

After a session ends:

- Expand any timeline row to see reasoning, coordinates used, and — behind a "Show raw data" toggle — the raw action JSON
- Download logs as `.txt` from the **Logs** panel
- Export the full session as `.json` using the **Export Session** button in the completion card or the Logs panel

---

## Feature Guide

### Landing Page

Route: `/`

The home page is a product overview. It explains the three execution modes and provides a 4-step "How it works" summary. The page header shows live sandbox status and **Start/Stop Sandbox** controls.

To start working, click **Open Workbench →**.

---

### Workbench — Status Header

Route: `/workbench`

The Workbench header shows:

- **Sandbox Ready / Sandbox Offline** — Docker container state. Clicking the colored dot shows overall system health.
- **Automation Ready / Automation Starting…** — internal agent service state (only shown when the sandbox is running)
- **⚠ Desktop view unprotected** — shown when `VNC_PASSWORD` is not set in your configuration
- **Start Sandbox / Stop Sandbox** — buttons to control the container
- **Live / Reconnecting…** — WebSocket connection state
- **Agent Running** — shown only while a session is active
- **Steps counter and cost estimate** — shown in the right side of the header during and after a session

---

### Workbench — Configuration Sidebar

The sidebar collects all inputs needed to start a session.

**Run Mode** — top-level toggle: Browser or Desktop  
Switching run mode automatically sets the default engine.

**Provider** — Google Gemini or Anthropic Claude

**Model** — loaded from `GET /api/models` at runtime. Only models listed in `backend/allowed_models.json` appear. If the backend is unreachable, the dropdown shows "Loading models…" or an error.

**API Key Source** — two-button toggle:
- **Enter manually** — shows a password input. The key is validated on blur (≥ 8 characters triggers a live check).
- **Saved key ✓** — uses a key found in `.env` or the process environment. The button is disabled if no key is available for the selected provider.

**API key link** — "Get a Google/Anthropic API key ↗" opens the provider's key-management page in a new tab.

**Advanced Settings** — collapsed by default. Contains:
- Engine override dropdown (filtered by run mode)
- Engine help text (one-line description of the selected engine)
- Run Location: **This machine** or **Docker container** (not available for all engines)
- Max Steps: integer 1–200, default 50

**Task** — plain-text task description. Supports Ctrl+Enter to start the agent. Sample task chips appear when the field is empty.

**Pre-flight warnings** — non-blocking. Any warnings from `GET /api/preflight` are shown above the Start button.

**Start Agent / Stop / Clear** — primary action buttons.

**Session result card** — appears after a session ends. Shows outcome (completed / failed), step count, estimated cost, and an Export Session button. Dismissable.

**Safety confirmation dialog** — appears mid-run when the agent requires explicit user approval before proceeding (see [Safety Confirmation](#safety-confirmation)).

---

### Workbench — Live Screen

The center panel shows the sandbox screen.

| State | What you see |
|---|---|
| Container not running | "No screen capture available" / "Start the container to see the live view" |
| Container running, service initializing | Spinner: "Waiting for agent service to start…" |
| Container running, VNC available | Interactive embedded desktop via noVNC (default) |
| Screenshot available, VNC unavailable | Agent screenshot image with "Interactive View" button to switch to VNC |

The noVNC view is proxied through the backend at `/vnc/` so the browser only needs access to `localhost:3000`. The VNC view is interactive — you can click and type directly in the desktop.

---

### Workbench — Timeline

Shows each step the agent has taken.

Each row displays:
- Step number
- Action icon
- Human-readable action label (e.g., "Click", "Type text", "Open URL", "Scroll")
- Target element preview (truncated to 20 characters, full text in tooltip)
- Text preview when applicable
- Timestamp (HH:MM:SS)

Click any row to expand it. The expanded view shows:
- Agent reasoning text
- Coordinates, if the action used them
- Error message, if the step failed
- **Show raw data** — a collapsible toggle revealing the raw action JSON

---

### Workbench — Logs Panel

Two tabs:

**Logs tab:**
- Streams real-time log messages from the agent and backend
- Filter by level: `info`, `warning`, `error`, `debug` (all enabled by default)
- **Download** button saves the current log view as a `.txt` file
- **Export** button saves the full session as `.json`
- **Clear** button removes logs and timeline steps
- The client keeps the last 200 log entries in memory

**Container tab:**
- Fetches the last 200 lines from `docker logs cua-environment` on demand
- Click **Refresh** to fetch a new snapshot
- This is not a streaming log view

---

## Automation Engines

Three engines are available. The correct choice depends on the task and the model.

### Browser Automation (`playwright_mcp`)

For web-based tasks.

- Interacts with the browser using Playwright's element-level API (click by element name, not pixel)
- Supports **This machine** or **Docker container** as the run location
- Defaults to **This machine** (the Playwright MCP server runs locally)
- Compatible with all four allowed models

### Desktop Automation (`omni_accessibility`)

For native desktop app interaction.

- Uses the system accessibility tree to identify and interact with UI elements
- Defaults to **Docker container** run location
- Compatible with all four allowed models

### Full Screen Control (`computer_use`)

For tasks that require native screen-level control.

- Uses the model's built-in computer-use capability (screenshot + action coordinates)
- **Requires Docker container** as the run location. Local execution is rejected by the backend.
- All four models in the allowlist support computer use

---

## Input Expectations

| Input | Requirement |
|---|---|
| Task text | Required. Plain text. No maximum length enforced by the UI, but the backend request model accepts up to 10,000 characters. |
| Provider | Must be `google` or `anthropic` |
| Model | Must be in `backend/allowed_models.json`. The dropdown enforces this. |
| API key | Required unless a saved key is available. Minimum 8 characters for validation to run. |
| Max steps | Integer 1–200. The backend hard-caps at 200. |
| Run location | `local` or `docker`. Full Screen Control requires `docker`. |

**Things the UI does not ask for:**
- Account credentials
- File uploads
- Saved workflows or projects

---

## Output and Export

### Session completion

When a session ends (completed, error, or max steps reached), a result card appears showing:

- Outcome badge: **✅ Task completed** or **❌ Task failed**
- Step count
- Estimated cost (rough estimate based on model and step count using preconfigured rates)
- **Export Session** button

### Session export (JSON)

The export is generated client-side. File naming pattern: `CUA_session_YYYYMMDD_HHMMSS.json`

Structure:

```json
{
  "exported_at": "2026-04-07T12:00:00.000Z",
  "config": {
    "provider": "google",
    "model": "gemini-3-flash-preview",
    "engine": "playwright_mcp",
    "executionTarget": "local",
    "maxSteps": 50,
    "runMode": "browser"
  },
  "steps": [],
  "logs": [],
  "final_screenshot": null
}
```

### Log download

Saves the currently visible log entries as a plain-text file. File naming pattern: `CUA_logs_YYYYMMDD_HHMMSS.txt`

### Session state

Session state is held in memory by the backend process. If the backend restarts, active session data is lost. There is no database or persistent storage.

**Backend session limits:**

| Limit | Value |
|---|---|
| Session starts per 60 seconds | 10 |
| Maximum concurrent active sessions | 3 |
| Idle session reaper timeout | 30 minutes |

---

## Session Persistence

The Workbench saves your configuration to `localStorage` as you type. This includes provider, model, run mode, engine, task text, max steps, run location, and key source choice. Screenshots, logs, and step history are never persisted to browser storage.

On the next visit to the Workbench, a **restore prompt** appears showing the previously saved task. You can:

- Click **Restore** to repopulate all fields with the saved configuration
- Click **Discard** to clear the saved state and start fresh

If you dismiss the modal without choosing, the prompt reappears on the next reload.

---

## Safety Confirmation

The Computer Use engine includes a safety-check hook that may pause execution and ask for explicit approval before performing a sensitive action.

When this happens:

1. A warning dialog appears in the Workbench sidebar with a description of the action requiring approval
2. Click **Allow** to permit the action
3. Click **Deny** to block it

If no response is given within **30 seconds**, the action is automatically denied and the agent continues (or stops, depending on what the denied action was part of).

The safety endpoint is `POST /api/agent/safety-confirm`.

---

## Configuration Reference

### API key environment variables

| Variable | Provider |
|---|---|
| `GOOGLE_API_KEY` | Google Gemini |
| `ANTHROPIC_API_KEY` | Anthropic Claude |

The backend resolves keys in this priority order: UI input → `.env` file → system environment variable. These are read by the backend; the frontend only transmits the key if the user chose "Enter manually".

### Environment variables read from `.env` or system environment

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | — | Google provider credential |
| `ANTHROPIC_API_KEY` | — | Anthropic provider credential |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Config default; not used when the UI specifies a model |
| `CONTAINER_NAME` | `cua-environment` | Docker container name |
| `AGENT_SERVICE_HOST` | `127.0.0.1` | Host for the in-container agent service |
| `AGENT_SERVICE_PORT` | `9222` | Port for the in-container agent service |
| `PLAYWRIGHT_MCP_HOST` | `localhost` | Host for the Playwright MCP server. Use `localhost`, not `127.0.0.1` — the MCP service checks the Host header |
| `PLAYWRIGHT_MCP_PORT` | `8931` | Port for Playwright MCP |
| `PLAYWRIGHT_MCP_PATH` | `/mcp` | HTTP endpoint path for Playwright MCP |
| `PLAYWRIGHT_MCP_AUTOSTART` | `0` | Set to `1` to auto-start the MCP server |
| `PLAYWRIGHT_MCP_COMMAND` | `npx` | Command used for local MCP startup |
| `PLAYWRIGHT_MCP_ARGS` | `-y @playwright/mcp@0.0.70` | Arguments for local MCP startup. Pinned to a known-good MCP version; override only when intentionally upgrading. |
| `PLAYWRIGHT_MCP_DOCKER_TRANSPORT` | `http` | Transport mode: `http` (Streamable HTTP) or `stdio` |
| `AGENT_MODE` | `browser` | Backend config default for agent mode |
| `HOST` | `127.0.0.1` | Backend bind host |
| `PORT` | `8000` | Backend bind port |
| `SCREEN_WIDTH` | `1440` | Sandbox screen width |
| `SCREEN_HEIGHT` | `900` | Sandbox screen height |
| `SCREENSHOT_FORMAT` | `png` | Screenshot encoding format |
| `MAX_STEPS` | `50` | Default step budget |
| `ACTION_DELAY_MS` | `100` | Post-action debounce delay in milliseconds |
| `STEP_TIMEOUT` | `30.0` | Per-step timeout in seconds |
| `GEMINI_RETRY_ATTEMPTS` | `3` | Retry count for Gemini API calls |
| `VNC_PASSWORD` | (empty) | Sets a password for the noVNC desktop. When non-empty, the backend writes the value to a 0600 host temp file and bind-mounts it read-only into the container at `/run/secrets/vnc_password` (env var `VNC_PASSWORD_FILE` is set inside the container). The password is **not** passed via `docker run -e`, so it does not appear in `docker inspect` or container env dumps. Empty means the desktop is accessible without a password. |
| `DEBUG` | `0` | Set to `1` for verbose backend logging and Uvicorn auto-reload |

### Model allowlist

`backend/allowed_models.json` is the single source of truth for available models. Both the backend and frontend read it at runtime.

| Provider | Model ID | Display name |
|---|---|---|
| Google | `gemini-3-flash-preview` | Gemini 3 Flash Preview |
| Google | `gemini-3.1-pro-preview` | Gemini 3.1 Pro Preview |
| Anthropic | `claude-sonnet-4-6` | Claude Sonnet 4.6 |
| Anthropic | `claude-opus-4-6` | Claude Opus 4.6 |

All four models support all three engines.

To add a model, edit `backend/allowed_models.json` directly. The backend and frontend will pick up the change on next restart.

### Cost estimate rates

The UI displays a rough estimated cost during and after a session. The rates used (in USD per step):

| Model prefix | Rate per step |
|---|---|
| `gemini-3-flash` | $0.003 |
| `claude-sonnet` | $0.015 |
| `claude-opus` | $0.075 |

These are approximations. Actual costs depend on token counts and provider pricing at the time of the call.

---

## Backend API Reference

All endpoints are served by `backend/api/server.py` (FastAPI). The frontend proxies all API calls through the Vite dev server at port 3000.

### REST endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness probe |
| `GET` | `/api/health/detailed` | Component health, VNC-protection flag, active session count |
| `GET` | `/api/models` | Allowlisted models for dropdown |
| `GET` | `/api/engines` | Available engines for dropdown |
| `GET` | `/api/keys/status` | API key availability and source by provider |
| `POST` | `/api/keys/validate` | Validate an API key against the provider |
| `GET` | `/api/preflight` | Pre-session readiness checks |
| `GET` | `/api/container/status` | Container and agent service status |
| `POST` | `/api/container/start` | Start the Docker sandbox |
| `POST` | `/api/container/stop` | Stop all sessions and the Docker sandbox |
| `POST` | `/api/container/build` | Trigger Docker image build |
| `POST` | `/api/session/ws-token` | Mint a single-use, 30-second WebSocket auth token (returns `{token, ttl_seconds}`) |
| `GET` | `/api/container/logs` | Fetch recent container logs (last N lines, max 500) |
| `GET` | `/api/agent-service/health` | Check agent service readiness |
| `POST` | `/api/agent-service/mode` | Switch agent service between browser and desktop mode |
| `GET` | `/api/screenshot` | Get current screenshot as base64 |
| `POST` | `/api/agent/start` | Start an agent session |
| `POST` | `/api/agent/stop/{session_id}` | Stop a running session |
| `GET` | `/api/agent/status/{session_id}` | Session status |
| `GET` | `/api/agent/history/{session_id}` | Step history (no screenshots) |
| `POST` | `/api/agent/safety-confirm` | Submit Allow or Deny for a safety confirmation prompt |
| `POST` | `/webrtc/offer` | WebRTC SDP negotiation (requires optional `aiortc` package) |
| `GET` | `/vnc/{path:path}` | Reverse proxy for noVNC static assets served by the container's websockify HTTP server |

### WebSockets

| Path | Purpose |
|---|---|
| `/ws` | Real-time event stream (logs, steps, screenshots, safety prompts, finish) |
| `/vnc/websockify` | Reverse proxy for the noVNC WebSocket inside the container |

#### Authentication and origin

Both WebSocket endpoints require:

1. An `Origin` header that matches one of the allowed local dev origins (the same set used by CORS), and
2. A single-use token passed as `?token=<value>`. Tokens are minted by `POST /api/session/ws-token`, expire after 30 seconds, and are consumed on accept. Browsers cannot set custom headers on WS upgrades, so the frontend fetches a fresh token over HTTP and appends it to the WS URL. For `/vnc/websockify`, the token is appended to noVNC's internal `path=` query parameter so the upgrade carries it.

Missing/invalid origin closes with code `1008`; missing/invalid token closes with `4401`.

#### Client → server messages on `/ws`

The frontend sends `ping` every 15 seconds and uses `subscribe`/`unsubscribe` to scope event delivery:

```json
{"type": "ping"}
{"type": "subscribe",   "session_id": "<uuid>"}
{"type": "unsubscribe", "session_id": "<uuid>"}
```

A newly-accepted client has an empty subscription set and receives **only** unscoped/global events. Per-session events (`step`, `log`, `screenshot`, `screenshot_stream`, `agent_finished`, `safety_confirmation`) are delivered only to clients that have subscribed to that session's id. This prevents one tab from seeing another tab's live frames.

#### Server → client events on `/ws`

| Event type | Payload | Notes |
|---|---|---|
| `screenshot` | `{ screenshot: "<base64-png>", session_id }` | Captured during step execution and broadcast to subscribers |
| `screenshot_stream` | `{ screenshot: "<base64>", format: "jpeg"\|"png" }` | Per-client live desktop stream (~every 1.5s); JPEG-q70 to reduce bandwidth |
| `log` | `{ log: { timestamp, level, message }, session_id }` | Agent and backend log messages |
| `step` | `{ step: { step_number, action, timestamp, error? }, session_id }` | One completed action (raw model response and screenshot are stripped) |
| `agent_finished` | `{ session_id, status, steps }` | Session end notification |
| `safety_confirmation` | `{ session_id, explanation }` | Requires user Allow/Deny via `POST /api/agent/safety-confirm` |
| `pong` | `{}` | Response to `ping` |

---

## Troubleshooting

### The UI loads but the sidebar shows "Backend Offline"

The backend is not reachable at `localhost:8000`. Confirm the backend started successfully:

```bash
curl http://localhost:8000/api/health
```

If you started everything with `start.sh` or `start.bat`, check the backend terminal window for errors. Common cause: Python dependencies not installed in the active Python environment.

### The sandbox won't start

Check Docker:

```bash
docker compose ps
docker compose logs -f cua-environment
```

Make sure the Docker daemon is running and that `cua-ubuntu:latest` image exists (`docker image ls | grep cua`). If not, run `bash setup.sh` or `docker compose build`.

### The screen shows "Waiting for agent service to start…"

The container is running but the internal agent service (on port 9222) has not become healthy yet. Wait a few seconds and it will resolve. If it persists, check container logs:

```bash
docker compose logs cua-environment
```

### No models load in the dropdown

The backend is reachable but model loading failed. Check:

```bash
curl http://localhost:8000/api/models
```

Models come exclusively from `backend/allowed_models.json`. If the file is missing or malformed, the endpoint returns an empty list.

### The "Saved key ✓" button is grayed out

No API key is found for the selected provider. Add one to `.env`:

```
GOOGLE_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here
```

Then restart the backend. You can verify:

```bash
curl http://localhost:8000/api/keys/status
```

### Full Screen Control fails immediately

The backend rejects `computer_use` with run location set to "This machine". Set the run location to **Docker container** in Advanced Settings.

### Playwright MCP calls fail with host-related errors

`PLAYWRIGHT_MCP_HOST` must be set to `localhost`, not `127.0.0.1`. The Playwright MCP server validates the `Host` header on incoming requests and rejects raw IP addresses.

### The Container tab in logs shows nothing

The Container log tab fetches on demand. Click **Refresh** to load the current container logs. It is not a live stream.

### WebRTC not working

The `/webrtc/offer` endpoint depends on `aiortc` and `av`, which are not installed by default. When they are missing, the backend returns HTTP 501 with an install hint instead of failing at import time. Install them with:

```bash
pip install aiortc av
```

### Session starts fail with a rate limit error

The backend allows a maximum of 10 session starts per 60-second window. Wait and retry.

### Closing the browser tab while a session is running

The browser will display a "Leave site?" native confirmation dialog. This guard is intentional — navigating away does not stop the backend session, but it will disconnect your WebSocket and you will lose the live view.

To stop the agent cleanly, use the **Stop** button before navigating away.

---

## Limitations and Important Notes

### No server-side persistence

Session state (steps, logs, screenshots) is held in memory in the backend process. Restarting the backend clears all session data. There is no database.

### No authentication or multi-user support

This is a single-user local workbench. There are no accounts, access controls, or audit logs beyond what the backend logs to stdout.

### No file upload

There is no file upload UI or backend flow. The agent can interact with files already present in the Docker sandbox.

### Session state does not survive backend restarts

If the backend restarts mid-session, the frontend will lose the WebSocket connection and the session cannot be resumed. The browser-side localStorage persistence saves configuration only — not running session state.

### Cost estimates are approximate

The per-step cost rates in `shared.js` are rough estimates based on model prefixes. Actual billed amounts depend on token counts and live provider pricing.

### Some `Config` fields are not environment-overridable

Not every field in `backend/config.py` has a corresponding environment variable. This guide lists only variables that are wired in `Config.from_env()`. Do not assume all fields are configurable via `.env`.

### VNC is not password-protected by default

All container ports (5900, 6080, 8931, 9222, 9223) are bound to `127.0.0.1` only, and the backend's `/vnc/websockify` proxy requires both a same-origin `Origin` header and a single-use ws-token. Setting `VNC_PASSWORD` adds a noVNC password layer on top (delivered to the container via a bind-mounted secret file, not via env vars). For local-only use this is generally fine; if you expose the host beyond localhost, set `VNC_PASSWORD` and review the CORS allow-list in `backend/api/server.py`.

