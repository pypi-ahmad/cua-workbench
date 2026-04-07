# CUA Workbench Usage Guide

This document explains how to set up and use CUA Workbench as it exists in this repository today.

CUA Workbench is a local operator console for running AI agents against a visible computer sandbox. The repository combines:

- A React frontend with a home view and a more detailed workbench view
- A FastAPI backend for session orchestration, validation, and streaming
- A Dockerized Ubuntu desktop with XFCE, noVNC, an internal agent service, and Playwright MCP

## Table of Contents

1. [Purpose](#purpose)
2. [Who This App Is For](#who-this-app-is-for)
3. [Before You Start](#before-you-start)
4. [Setup and Prerequisites](#setup-and-prerequisites)
5. [How to Start the App Locally](#how-to-start-the-app-locally)
6. [Main User Workflow](#main-user-workflow)
7. [Feature Guide](#feature-guide)
8. [Input Expectations](#input-expectations)
9. [Output and Result Behavior](#output-and-result-behavior)
10. [Configuration Reference](#configuration-reference)
11. [API and Realtime Surface](#api-and-realtime-surface)
12. [Troubleshooting](#troubleshooting)
13. [Limitations and Important Notes](#limitations-and-important-notes)

## Purpose

CUA Workbench is for running browser and desktop automation sessions with an LLM while keeping the execution environment visible and inspectable.

In the current implementation, you can:

- Select a provider, model, engine, and execution target from the UI
- Start an agent session from a plain-text task description
- Watch screenshots or an interactive noVNC desktop view
- Inspect logs and per-step action history in real time
- Export a completed session as JSON from the UI

The app does not present itself as a hosted product. It is a local development and experimentation workbench.

## Who This App Is For

This repository is best suited for:

- Engineers building or testing agent workflows
- QA or automation engineers comparing browser-semantic and desktop-semantic execution
- Model and prompt developers who need to inspect step-by-step agent behavior
- Developers who want a visible Linux sandbox instead of headless automation

It is not currently structured as a multi-user application with accounts, saved workspaces, or persistent server-side history.

## Before You Start

Review these repo-specific realities before you launch anything:

1. Docker is effectively required for the visible sandbox experience. The UI can load without Docker, but container-backed features will be unavailable.
2. You need at least one supported provider key: `GOOGLE_API_KEY` or `ANTHROPIC_API_KEY`, unless you plan to paste the key into the UI each session.
3. The current frontend proxy configuration and the default backend entry point are not aligned:
   - `frontend/vite.config.js` proxies `/api`, `/ws`, and `/vnc` to `http://localhost:8080`
   - `python -m backend.main` starts the backend on port `8000`
   - `start.sh` and `start.bat` also start the backend on `8000`

Because of that mismatch, the most reliable no-edit local flow today is to start the backend on port `8080` when using the frontend as currently configured.

## Setup and Prerequisites

### Required software

| Requirement | What the repo expects |
| --- | --- |
| Docker | Running daemon available to `docker` and `docker compose` |
| Python | `python` or `python3`, version 3.10+ |
| Node.js | Version 18+ |

### One-command setup

Linux or macOS:

```bash
bash setup.sh
```

Windows:

```bat
setup.bat
```

What these scripts actually do:

- Check for Docker, Python, and Node.js
- Check that the Docker daemon is running
- Build the Docker image
- Create `.venv` if needed
- Install `requirements.txt`
- Run `npm install` in `frontend`

Both setup scripts also support destructive cleanup first:

```bash
bash setup.sh --clean
```

```bat
setup.bat --clean
```

### Manual setup

```bash
docker compose build

python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

cd frontend
npm install
cd ..
```

## How to Start the App Locally

### Recommended path for the current repo state

This path matches the current frontend proxy settings without editing source files.

### 1. Start the Docker container

```bash
docker compose up -d --build
```

Optional checks:

```bash
docker compose ps
docker compose logs -f cua-environment
```

### 2. Start the backend on port 8080

Activate the virtual environment first, then run:

```bash
python -m uvicorn backend.api.server:app --host 127.0.0.1 --port 8080
```

Why `8080` is recommended here:

- The frontend proxy currently points to `localhost:8080`
- The default backend entry point still uses `8000`
- Starting uvicorn directly on `8080` avoids changing code just to make local development work

### 3. Start the frontend

```bash
cd frontend
npm run dev
```

`frontend/vite.config.js` requests port `3000`.

Open the URL Vite prints in the terminal. In the current codebase, the intended frontend URL is `http://localhost:3000`, but if that port is already occupied, Vite may choose another port.

### Alternative launcher scripts

The repository also includes:

- `start.sh`
- `start.bat`

Those scripts currently:

- Start the backend on `http://localhost:8000`
- Start the frontend and print `http://localhost:5173`
- Do not start the Docker container for you

Use them only if you understand the current port mismatch and have aligned the frontend proxy with the backend port you want to use.

## Main User Workflow

This is the primary end-user flow supported by the current UI.

### 1. Open the app

The frontend has two routes:

- `/` - the home page with the control panel, screen, and logs
- `/workbench` - the expanded three-pane workbench view

### 2. Confirm backend and container status

The header and workbench status pills show whether:

- The WebSocket is connected
- The Docker container is running
- The internal agent service is ready
- VNC is open without a password

If the container is stopped, use `Start Container` from the header or let agent start trigger an automatic container start.

### 3. Choose provider and model

The UI loads provider and model options from backend endpoints, not from hardcoded frontend constants.

Supported providers in the current repo:

- Google Gemini
- Anthropic Claude

Supported models come from `backend/allowed_models.json`.

### 4. Choose an API key source

The UI exposes three key-source modes:

- `Enter key`
- `From .env file`
- `Environment variable`

If the selected provider key is already available from `.env` or the process environment, the corresponding button is enabled and shows a masked key preview.

### 5. Choose engine and run location

Available engines are loaded from `GET /api/engines`.

Current engine choices:

- `Browser (Semantic)` -> `playwright_mcp`
- `Desktop (Accessibility)` -> `omni_accessibility`
- `Computer Use (Native)` -> `computer_use`

Run location values exposed in the UI:

- `This machine`
- `Docker container`

Default behavior in the frontend:

- `playwright_mcp` defaults to `local`
- `omni_accessibility` defaults to `docker`
- `computer_use` defaults to `docker`

### 6. Enter a task

Provide a plain-language task in the text area.

The UI also exposes sample task chips, currently including examples like:

- `Search Google for 'latest AI news'`
- `Open the file manager and list files in /tmp`
- `Take a screenshot of the desktop`
- `Go to wikipedia.org and find the main page featured article`

### 7. Start the agent

You can start from either view using:

- The `Start Agent (Ctrl+Enter)` button
- `Ctrl+Enter` in the task field

Before start, the frontend runs a pre-flight check through `GET /api/preflight`. Warnings are displayed, but they do not block session start.

### 8. Watch the run

During execution, the app can show:

- Screenshots over WebSocket
- An interactive noVNC view, when available
- Live logs
- Action history or timeline entries

### 9. Stop, inspect, or export

After or during a run, you can:

- Stop the session with `Stop`
- Clear logs and steps in the workbench view
- Download logs as `.txt` from the workbench log panel
- Export a session as `.json` from the home view or workbench view

## Feature Guide

### Header and top-level status

The home page header shows:

- Health dot derived from `GET /api/health/detailed`
- `Container Running` or `Container Stopped`
- `Agent Service Ready` or `Agent Service Down`
- `Start Container` or `Stop Container`
- `Connected`, `Disconnected`, or `Agent Running`
- `VNC open` warning when `VNC_PASSWORD` is unset

### Home page

The `/` route is composed of:

- `Header`
- `ControlPanel`
- `ScreenView`
- `LogPanel`

#### API Configuration section

This section includes:

- Provider selector
- API key source buttons
- API key input when `Enter key` is selected
- Model selector
- Engine selector
- A short engine help line
- `Advanced Settings`
- `Open Workbench ->` link

Behavior worth knowing:

- If the backend is unreachable, model loading fails and the UI shows backend-offline messaging
- Models unsupported by the selected engine are disabled in the engine dropdown logic
- API key validation runs on blur when the typed key has length >= 8

#### Advanced Settings on the home page

The home view exposes `Run location` inside the advanced section. `Max steps` is shown near the task section as a numeric input.

#### Task section

The task section includes:

- Multi-line task input
- Sample task buttons when the task box is empty
- `Max steps` numeric input
- Optional pre-flight warnings block
- `Start Agent (Ctrl+Enter)` button
- `Stop` button
- JSON export button after a run has produced steps

#### Action History section

The home page shows a compact step list with:

- Step number
- Action badge
- Coordinates when present
- Text preview when present
- Reasoning text when present
- Error text for failed steps

When the agent is running, a progress bar is shown. When the run is complete, the home view also shows an estimated total cost based on the selected model prefix and the number of steps.

### Workbench page

The `/workbench` route is the more detailed operator view.

#### Left sidebar

The sidebar exposes:

- `Run Mode` toggle: `Browser` or `Desktop`
- Provider selector
- Model selector
- `API Key Source`
- API key field when `Enter key` is active
- `Advanced Settings`
- Task text area
- `Start Agent (Ctrl+Enter)`
- `Stop`
- `Clear`

The `Run Mode` toggle changes the default engine:

- `Browser` -> `playwright_mcp`
- `Desktop` -> `computer_use`

#### Live Screen panel

`ScreenView` has three real user-visible states:

1. Loading state: `Waiting for agent service to start...`
2. Interactive state: embedded noVNC iframe with `Interactive` badge
3. Fallback state: screenshot image with `Screenshot` badge and `Interactive View` button

If there is no screenshot and the container is not ready, the empty state reads:

- `No screen capture available`
- `Start the container to see the live view`

#### Timeline panel

The workbench timeline shows:

- Step number
- Action icon
- Action name
- Target preview when present
- Text preview when present
- Timestamp

Clicking a timeline row expands:

- Reasoning
- Coordinates
- Error message, if any
- Raw action JSON

#### Logs panel

The workbench logs panel has two tabs:

- `Logs`
- `Container`

`Logs` supports:

- Per-level filters for `info`, `warning`, `error`, and `debug`
- `Download` to save logs as `.txt`
- `Export` to save session JSON
- `Clear`

`Container` supports:

- On-demand fetch of the last 200 lines through `GET /api/container/logs?lines=200`
- `Refresh`

### Automation engines

#### `playwright_mcp`

Intended for browser-centric tasks.

Current repo behavior:

- Can run with `local` or `docker` execution target
- Uses Playwright MCP tooling
- The backend discovers available MCP tools dynamically from the server using `tools/list`

#### `omni_accessibility`

Intended for accessibility-tree-driven desktop control.

Current repo behavior:

- Defaults to `docker` in the frontend
- Uses the accessibility engine path rather than Playwright MCP
- Is exposed in the UI as `Desktop (Accessibility)`

#### `computer_use`

Intended for native computer-use-capable models.

Current repo behavior:

- Exposed in the UI as `Computer Use (Native)`
- Backend rejects `execution_target=local`
- Uses model-native computer-use execution rather than the standard action router

## Input Expectations

### Task text

- Required
- Plain text
- Maximum request size is constrained by the backend request model to 10,000 characters

### API keys

- UI key entry is optional if `.env` or system environment already provides the key
- The backend resolves keys in this exact order:
  1. UI input
  2. `.env`
  3. process environment

### Provider and model

- Provider must be `google` or `anthropic`
- Model must be allowlisted in `backend/allowed_models.json`

### Max steps

- UI input accepts `1` through `200`
- Backend hard-caps the value at `200`

### Run location

- UI exposes `local` and `docker`
- `computer_use` with `local` is rejected by the backend with a 400 error

### Things this UI does not currently ask for

There is no implemented user flow for:

- Account sign-in
- Onboarding wizard
- File upload
- Saved projects or saved workflows
- Server-side persisted results

## Output and Result Behavior

### Session state and completion

Session state is in memory only.

If the backend process stops, active session state is lost.

The backend enforces:

- Rate limit: 10 session starts per 60 seconds
- Maximum concurrent sessions: 3
- Idle timeout reaper: 30 minutes of inactivity

### Realtime events shown in the frontend

The current frontend WebSocket hook handles these event types:

- `screenshot`
- `screenshot_stream`
- `log`
- `step`
- `agent_finished`
- `pong`

The frontend stores only the latest 200 log records in client state.

### Export behavior

Session export is client-side JSON generation. The exported file includes:

```json
{
  "exported_at": "ISO timestamp",
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

Home view exports omit `runMode` because that value is only tracked in the workbench page.

Filename pattern:

- Session export: `CUA_session_YYYYMMDD_HHMMSS.json`
- Log download: `CUA_logs_YYYYMMDD_HHMMSS.txt`

### Result visibility

Completed work is exposed through:

- Action history on the home page
- Timeline on the workbench page
- Log streams in both views
- Latest screenshot image

There is no dedicated summary page or persisted report view in the current frontend.

## Configuration Reference

This section lists configuration that is actually wired into the current codebase.

### API key environment variables

| Variable | Used for |
| --- | --- |
| `GOOGLE_API_KEY` | Google provider sessions |
| `ANTHROPIC_API_KEY` | Anthropic provider sessions |

### Environment variables read by `backend.config.Config.from_env()`

| Variable | Default | Notes |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Default model used when code falls back to config |
| `CONTAINER_NAME` | `cua-environment` | Container name used by backend helpers |
| `AGENT_SERVICE_HOST` | `127.0.0.1` | Backend-side agent service host |
| `AGENT_SERVICE_PORT` | `9222` | Backend-side agent service port |
| `AGENT_MODE` | `browser` | Default agent mode in config |
| `PLAYWRIGHT_MCP_HOST` | `localhost` | Important for host-header-sensitive MCP calls |
| `PLAYWRIGHT_MCP_PORT` | `8931` | Playwright MCP port |
| `PLAYWRIGHT_MCP_PATH` | `/mcp` | MCP HTTP endpoint path |
| `PLAYWRIGHT_MCP_AUTOSTART` | `0` | Boolean-like string |
| `PLAYWRIGHT_MCP_COMMAND` | `npx` | Command used for local MCP startup |
| `PLAYWRIGHT_MCP_ARGS` | `-y @playwright/mcp@latest` | Arguments for local MCP startup |
| `PLAYWRIGHT_MCP_DOCKER_TRANSPORT` | `http` | Transport mode string |
| `SCREEN_WIDTH` | `1440` | Screen width used by backend and container |
| `SCREEN_HEIGHT` | `900` | Screen height used by backend and container |
| `MAX_STEPS` | `50` | Default step budget |
| `STEP_TIMEOUT` | `30.0` | Per-step timeout |
| `GEMINI_RETRY_ATTEMPTS` | `3` | Shared retry count used by both provider clients |
| `DEBUG` | `0` | Enables backend debug mode |
| `VNC_PASSWORD` | empty | Empty means VNC is not password protected |

### Defaults present in config but not currently loaded from env

The config class also defines defaults such as `host`, `port`, `action_delay_ms`, `gemini_retry_delay`, `ws_screenshot_interval`, and `screenshot_format`.

As of the current code, `Config.from_env()` does not load those values from environment variables. Documenting them as configurable env vars would be misleading.

### Model allowlist

The model dropdown and backend validation both depend on `backend/allowed_models.json`.

Current allowlisted models:

| Provider | Model ID | Notes |
| --- | --- | --- |
| Google | `gemini-3-flash-preview` | Supports all three engine capability flags in the allowlist |
| Google | `gemini-3.1-pro-preview` | Supports all three engine capability flags in the allowlist |
| Anthropic | `claude-sonnet-4-6` | Includes `cu_tool_version` and `cu_betas` metadata |
| Anthropic | `claude-opus-4-6` | Includes `cu_tool_version` and `cu_betas` metadata |

## API and Realtime Surface

This is the backend surface actually exposed by `backend/api/server.py`.

### Core REST endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Basic liveness probe |
| `GET` | `/api/health/detailed` | Component health and VNC-protection signal |
| `GET` | `/api/models` | Allowlisted models |
| `GET` | `/api/engines` | Engine list for the UI |
| `GET` | `/api/keys/status` | Key availability and source |
| `POST` | `/api/keys/validate` | Provider key validation |
| `GET` | `/api/preflight` | Pre-flight readiness checks |
| `GET` | `/api/container/status` | Container and agent-service status |
| `POST` | `/api/container/start` | Start container |
| `POST` | `/api/container/stop` | Stop active sessions and stop container |
| `POST` | `/api/container/build` | Build Docker image |
| `GET` | `/api/container/logs` | Fetch recent container logs |
| `GET` | `/api/agent-service/health` | Agent service health probe |
| `POST` | `/api/agent-service/mode` | Switch agent-service mode |
| `GET` | `/api/screenshot` | Get current screenshot |
| `POST` | `/api/agent/start` | Start agent session |
| `POST` | `/api/agent/stop/{session_id}` | Stop running session |
| `GET` | `/api/agent/status/{session_id}` | Session status |
| `GET` | `/api/agent/history/{session_id}` | Step history without screenshots |
| `POST` | `/api/agent/safety-confirm` | Safety-confirmation response endpoint |
| `POST` | `/webrtc/offer` | Optional WebRTC negotiation endpoint |
| `GET` | `/vnc/{path:path}` | noVNC reverse proxy |

### WebSocket endpoint

The backend exposes one WebSocket endpoint:

```text
/ws
```

The frontend connects to same-origin `/ws` and sends a ping every 15 seconds.

## Troubleshooting

### The frontend loads, but API calls fail immediately

Most likely cause in the current repo: frontend and backend ports are not aligned.

Current state:

- Frontend proxy points to `localhost:8080`
- `python -m backend.main` serves on `8000`

Use one of these fixes:

1. Start the backend on `8080` with uvicorn:

```bash
python -m uvicorn backend.api.server:app --host 127.0.0.1 --port 8080
```

2. Or edit `frontend/vite.config.js` to proxy to `8000` if you specifically want to keep using `python -m backend.main`.

### The backend starts, but the UI still shows `Backend Offline`

Check:

```bash
curl http://127.0.0.1:8080/api/health
```

If you are running the backend on `8000` instead, test that port instead and make sure the frontend proxy matches it.

### The screen shows `Waiting for agent service to start...`

This means the container is up, but the agent service is not healthy yet.

Check:

```bash
docker compose ps
docker compose logs -f cua-environment
```

### The screen stays blank or shows `No screen capture available`

Confirm that:

- The container is running
- The backend is reachable from the frontend
- The agent service has started

You can also hit:

```bash
curl http://127.0.0.1:8080/api/screenshot
```

### The UI says no key is available

The backend only resolves keys from:

1. UI input
2. `.env` in the repository root
3. process environment

Use:

```bash
curl http://127.0.0.1:8080/api/keys/status
```

### `Computer Use` fails before doing anything

The backend explicitly rejects `computer_use` with `execution_target=local`.

Use `Docker container` as the run location.

### Playwright MCP calls fail with host-header-related errors

The codebase includes a specific safeguard for `PLAYWRIGHT_MCP_HOST=localhost`. If you set it to `127.0.0.1`, some MCP flows can fail due to host-header expectations.

### Container logs are empty in the workbench

The workbench fetches container logs only when you open the `Container` tab or click `Refresh`. It is not a streaming container-log viewer.

### WebRTC does not work

`/webrtc/offer` exists, but it depends on optional packages not installed by default.

Install them manually:

```bash
python -m pip install aiortc av
```

## Limitations and Important Notes

These are important current-repo limitations, not future plans.

### No persistence layer

- Session state is in memory only
- Restarting the backend clears active sessions
- There is no database-backed history or result archive

### No auth or onboarding flow

- No sign-in or user accounts
- No onboarding wizard
- No permission model between users

### No upload workflow

There is no implemented file-upload UI or upload-specific backend flow in the current app.

### Current launcher mismatch

The repository's convenience launcher scripts and frontend proxy settings do not currently line up on the same backend port. The docs above call that out explicitly instead of assuming an idealized setup.

### Safety confirmation is only partially surfaced

The backend exposes a safety-confirmation endpoint and the Computer Use engine emits safety-related log data, but the current frontend WebSocket hook does not handle a `safety_confirmation` event and no approval dialog is rendered in the UI.

In the current Computer Use path, the engine-side callback conservatively returns `False` rather than silently proceeding.

### Some config values are defaults, not live env controls

Do not assume every field in `backend/config.py` can be changed with an environment variable. This guide lists only configuration that is actually wired up today, and it explicitly calls out defaults that are not env-backed.
