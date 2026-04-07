# CUA Workbench — Usage & Features Guide

A comprehensive guide to setting up, configuring, and using the Computer Using Agent (CUA) Workbench.

---

## Table of Contents

- [What is CUA Workbench?](#what-is-cua-workbench)
- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Installation & Setup](#installation--setup)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Using the Web Interface](#using-the-web-interface)
- [Automation Engines](#automation-engines)
- [Supported Models](#supported-models)
- [API Reference](#api-reference)
- [WebSocket Events](#websocket-events)
- [Advanced Features](#advanced-features)
- [Troubleshooting](#troubleshooting)

---

## What is CUA Workbench?

CUA Workbench is a local-first environment for building, testing, and observing AI agents that automate computer tasks. It combines:

- A **React web interface** for control and monitoring
- A **FastAPI backend** orchestrating sessions and lifecycle
- A **Dockerized Ubuntu desktop** sandbox with visible automation
- Three **automation engines** for different task types

**Key advantages:**

- Run agents in a **visible sandbox** (VNC) — not blind automation
- Compare **three execution styles** from one UI
- Keep model and engine choices **explicit** (no auto-switching)
- Observe **every action** through logs, screenshots, and noVNC
- Support for **Google Gemini** and **Anthropic Claude** models

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  Frontend (React 19 + Vite 6)                            │
│  Workbench UI · Model/engine selection · Live logs/VNC   │
│  Port: 5173 (dev)                                        │
└────────────┬─────────────────────────────────────────────┘
             │ HTTP + WebSocket
             ▼
┌──────────────────────────────────────────────────────────┐
│  Backend (FastAPI + Uvicorn)                              │
│  REST API (/api/*) · WebSocket (/ws) · Session mgmt      │
│  Docker lifecycle · Model router (Gemini/Claude)         │
│  Port: 8000                                              │
└────────────┬─────────────────────────────────────────────┘
             │ HTTP + Docker exec
             ▼
┌──────────────────────────────────────────────────────────┐
│  Ubuntu 24.04 Docker Container                           │
│  XFCE4 desktop · Xvfb :99 · Agent Service (9222)        │
│  Playwright MCP (8931) · x11vnc (5900) · noVNC (6080)   │
│  Chromium browser · Accessibility stack                  │
└──────────────────────────────────────────────────────────┘
```

### Port Map

| Port | Service | Access |
|------|---------|--------|
| 5173 | Frontend dev server | `http://localhost:5173` |
| 8000 | Backend API | `http://localhost:8000` |
| 5900 | VNC server | Container internal |
| 6080 | noVNC web UI | Proxied via backend `/vnc` |
| 8931 | Playwright MCP server | Container internal |
| 9222 | Agent Service API | Container internal |

---

## Prerequisites

- **Docker** with a running daemon
- **Python 3.10+**
- **Node.js 18+**
- Linux, macOS, or Windows (WSL2 recommended on Windows)

---

## Installation & Setup

### Quick Setup (Recommended)

**Linux / macOS:**

```bash
bash setup.sh
```

**Windows (PowerShell as Administrator):**

```batch
setup.bat
```

Both scripts will:

1. Verify Docker, Python, and Node.js are installed
2. Build the Docker image via `docker compose build`
3. Create a Python virtual environment and install dependencies
4. Install frontend dependencies with `npm install`

### Manual Setup

```bash
# 1. Build Docker image
docker compose build

# 2. Create Python environment
python3 -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate.bat     # Windows

# 3. Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Install frontend dependencies
cd frontend && npm install && cd ..
```

### Clean Rebuild

```bash
bash setup.sh --clean    # Linux/macOS
setup.bat --clean         # Windows
```

This removes all containers, images, and volumes associated with the compose stack before rebuilding.

---

## Configuration

### Environment Variables

Create a `.env` file in the repository root:

```env
# ── API Keys (resolution order: UI input > .env > system env) ──
GOOGLE_API_KEY=your-google-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key

# ── Backend ──
GEMINI_MODEL=gemini-3-flash-preview
CONTAINER_NAME=cua-environment
AGENT_SERVICE_HOST=127.0.0.1
AGENT_SERVICE_PORT=9222
AGENT_MODE=browser                 # "browser" or "desktop"

# ── Playwright MCP ──
PLAYWRIGHT_MCP_HOST=localhost
PLAYWRIGHT_MCP_PORT=8931
PLAYWRIGHT_MCP_AUTOSTART=0         # "1" to auto-start on container launch

# ── Display & Viewport ──
SCREEN_WIDTH=1440
SCREEN_HEIGHT=900
SCREENSHOT_FORMAT=png

# ── Agent Tuning ──
MAX_STEPS=50
STEP_TIMEOUT=30.0
ACTION_DELAY_MS=500
SCREENSHOT_INTERVAL_SEC=1.0
DEBUG=0                            # "1" for debug logging
```

### API Key Resolution

Keys are resolved in this order:

1. **UI Input** — Key entered directly in the workbench control panel
2. **.env File** — Values from the `.env` file in the repo root
3. **System Environment** — Shell environment variables

The frontend shows the active source via the **Key Statuses** indicator (backed by `GET /api/key-statuses`).

### Model Allowlist

Edit `backend/allowed_models.json` to add or remove models. Both the frontend and backend read this file at startup — changes take effect on restart.

```json
{
  "models": [
    {
      "provider": "google",
      "model_id": "gemini-3-flash-preview",
      "display_name": "Gemini 3 Flash Preview",
      "supports_computer_use": true,
      "supports_playwright_mcp": true,
      "supports_accessibility": true,
      "notes": "Fast, lightweight Gemini CU model."
    }
  ]
}
```

---

## Running the Application

### 1. Start the Docker Container

```bash
docker compose up -d --build
```

The container automatically starts Xvfb, XFCE4 desktop, AT-SPI accessibility bridge, VNC servers, the agent service (port 9222), and the Playwright MCP server (port 8931).

```bash
# Check status
docker compose ps

# View logs
docker compose logs -f cua-environment
```

### 2. Start the Backend

```bash
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate.bat     # Windows

python -m backend.main
```

The API is now available at `http://localhost:8000`.

### 3. Start the Frontend

```bash
cd frontend
npm run dev
```

The Vite dev server starts at `http://localhost:5173` and proxies `/api`, `/ws`, and `/vnc` to the backend.

### 4. Open the Workbench

Navigate to **http://localhost:5173** in your browser.

---

## Using the Web Interface

### Header Bar

Displays:

- **CUA — Computer Using Agent** branding
- **Container Status** — Running / Stopped
- **WebSocket Status** — Connected / Disconnected / Agent Running

### Control Panel (Left)

#### API Configuration

| Setting | Description |
|---------|-------------|
| **Provider** | Google Gemini or Anthropic Claude |
| **Model** | Available models for the selected provider |
| **API Key Source** | UI input, .env file, or system environment |
| **API Key** | Manual key entry (disabled when using .env or env source) |

#### Engine Selection

| Setting | Description |
|---------|-------------|
| **Engine** | Playwright MCP, Omni Accessibility, or Computer Use |
| **Execution Target** | `local` (host machine) or `docker` (container) |

Default targets:

- `playwright_mcp` → **local**
- `omni_accessibility` → **docker**
- `computer_use` → **docker**

#### Task Configuration

| Setting | Description |
|---------|-------------|
| **Task** | Plain English description of what the agent should do |
| **Max Steps** | Hard cap on agent steps (max 200) |

#### Container Controls

- **Start Container** / **Stop Container**
- **Build Image** — Rebuild the Docker image

#### Agent Controls

- **Start Agent** (also `Ctrl+Enter`) — Launch a session with the current config
- **Stop Agent** — Halt the running session
- **Action History** — View / clear previous steps

### Screen View (Right)

Two display modes:

- **VNC (Interactive)** — Embedded noVNC iframe showing the live desktop. Full mouse, keyboard, and clipboard interaction.
- **Screenshot Fallback** — Static screenshot when VNC is unavailable. Button to switch to VNC mode.

### Log Panel (Bottom)

Real-time log stream from the agent loop:

```
09:15:32 [info]  Agent starting — task: Search for OpenAI on Google
09:15:33 [info]  Model: gemini-3-flash-preview | Engine: playwright_mcp
09:15:40 [step]  Step 1: browser_navigate → "https://www.google.com"
```

- Auto-scrolls to latest entry
- Shows timestamp, level (info/warning/error/debug), and message
- **Clear** button to reset the log view
- Last 200 entries kept in buffer

---

## Automation Engines

### 1. Playwright MCP — Semantic Browser Automation

**Best for:** Web tasks (search, form filling, navigation)

Uses the **accessibility tree snapshot** (text) instead of screenshots. The model references DOM elements by **refs** (e.g., `[ref=S12]`) — no pixel coordinates needed.

**Available actions:**

| Category | Actions |
|----------|---------|
| Navigation | `browser_navigate`, `browser_navigate_back`, `browser_tabs` |
| Interaction | `browser_click`, `browser_hover`, `browser_type`, `browser_fill_form`, `browser_select_option` |
| Observation | `browser_snapshot`, `browser_take_screenshot`, `browser_evaluate` |
| Execution | `browser_run_code`, `browser_wait_for` |

**Configuration:**

- Execution target: **local** (STDIO on host) or **docker** (HTTP in container)
- Auto-discovers tools from the MCP server at startup via `tools/list`

**Example task:**

> Search for "OpenAI" on Google and click the first result

---

### 2. Omni Accessibility — Cross-Platform Desktop Automation

**Best for:** Desktop apps (file manager, system settings, office apps)

Uses the platform's native **accessibility API** (AT-SPI2 on Linux, UIAutomation on Windows, JXA on macOS). The model receives a semantic element list with roles, names, states, and bounding boxes.

**Available actions:**

| Category | Actions |
|----------|---------|
| Discovery | `get_accessibility_tree`, `get_snapshot`, `find_by_role`, `find_by_text` |
| Interaction | `click`, `type`, `scroll`, `focus`, `set_value` |
| System | `open_app`, `open_terminal`, `run_command` |

**Example task:**

> Open the file manager, navigate to /tmp, and list all files

---

### 3. Computer Use — Native CU Protocol

**Best for:** Complex browser + desktop workflows with safety gates

Uses the native **computer_use tool protocol** recognized by Gemini 3 and Claude 4.6 models. The model outputs structured CU actions; the backend can request **safety confirmation** before executing dangerous actions.

**Execution environments:** `browser` (Playwright) or `desktop` (xdotool + scrot)

**Available actions:**

| Category | Actions |
|----------|---------|
| Browser | `open_web_browser`, `go_back`, `go_forward`, `search`, `navigate`, `click_at`, `hover_at`, `type_text_at`, `key_combination`, `scroll_document`, `drag_and_drop`, `wait_5_seconds` |
| Desktop | `run_command`, `open_system_application` |

**Coordinate systems:**

- **Gemini:** 0–999 normalized (auto-denormalized to viewport)
- **Claude:** Real pixel coordinates

**Example task:**

> Take a screenshot, then click at coordinates (500, 300)

---

## Supported Models

### Google Gemini

| Model ID | Display Name | CU | MCP | Accessibility |
|----------|-------------|-----|-----|---------------|
| `gemini-3-flash-preview` | Gemini 3 Flash Preview | ✅ | ✅ | ✅ |
| `gemini-3.1-pro-preview` | Gemini 3.1 Pro Preview | ✅ | ✅ | ✅ |

**Get an API key:** [Google AI Studio](https://aistudio.google.com/) → Get API Key

### Anthropic Claude

| Model ID | Display Name | CU | MCP | Accessibility |
|----------|-------------|-----|-----|---------------|
| `claude-sonnet-4-6` | Claude Sonnet 4.6 | ✅ | ✅ | ✅ |
| `claude-opus-4-6` | Claude Opus 4.6 | ✅ | ✅ | ✅ |

**Get an API key:** [console.anthropic.com](https://console.anthropic.com/) → Create API Key

---

## API Reference

### Health & Status

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check — returns `{"status": "ok"}` |
| GET | `/api/models` | List allowlisted models with capability flags |
| GET | `/api/engines` | List supported engines |
| GET | `/api/key-statuses` | API key availability by provider (source, masked key) |

### Container Lifecycle

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/container/status` | Container and agent service state |
| POST | `/api/container/start` | Start the Docker container |
| POST | `/api/container/stop` | Stop the Docker container |
| POST | `/api/container/build` | Rebuild the Docker image |

### Agent Session

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/agent/start` | Start a new agent session |
| POST | `/api/agent/stop/{session_id}` | Stop a running session |
| GET | `/api/agent/status/{session_id}` | Get session status and step count |

**Start session payload:**

```json
{
  "task": "Search for OpenAI on Google",
  "api_key": "optional-key-from-ui",
  "model": "gemini-3-flash-preview",
  "provider": "google",
  "engine": "playwright_mcp",
  "execution_target": "local",
  "mode": "browser",
  "max_steps": 50
}
```

**Validation rules:**

- Rate limit: 10 sessions per 60 seconds
- Max concurrent sessions: 3
- Max steps hard cap: 200
- Engine must be in the supported engines list
- Model must be in the allowlist for the selected provider

### WebRTC (Optional)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/webrtc/offer` | Negotiate WebRTC connection for video streaming |

Requires optional dependencies: `pip install aiortc av`

---

## WebSocket Events

Connect to `ws://localhost:5173/ws` for real-time events.

### screenshot

```json
{
  "event": "screenshot",
  "screenshot": "base64-encoded-png-data"
}
```

### log

```json
{
  "event": "log",
  "log": {
    "timestamp": "2026-04-07T09:15:30.123456Z",
    "level": "info",
    "message": "Agent starting — task: ..."
  }
}
```

### step

```json
{
  "event": "step",
  "step": {
    "step_number": 1,
    "action": "browser_navigate",
    "target": "",
    "text": "https://www.google.com",
    "coordinates": null,
    "reasoning": "Navigate to Google to search",
    "result": "Success"
  }
}
```

### agent_finished

```json
{
  "event": "agent_finished",
  "session_id": "...",
  "status": "completed",
  "message": "Task complete"
}
```

### Heartbeat

Client sends `{"type": "ping"}` every 15 seconds; server responds with `pong`.

---

## Advanced Features

### Action History & Recovery

The agent maintains a **sliding window of the last 15 actions** to support recovery:

- **Error tracking** — Consecutive error tolerance (max 3) triggers recovery strategies
- **Duplicate detection** — Catches repeated failing actions and injects recovery hints
- **Stuck detection** — If the model repeats the same action >2 times with identical coordinates, the loop forces reconsideration

### Engine Capabilities Registry

`backend/engine_capabilities.json` declares what each engine supports:

- **allowed_actions** — Validated before dispatch
- **categories** — Grouped action types (navigation, interaction, observation)
- **limitations** — What the engine cannot do
- **environment_requirements** — Required system packages

Every action is validated against these capabilities **before** being sent to the container.

### In-Container Agent Service

A lightweight FastAPI service runs inside the Docker container on port 9222:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check |
| POST | `/action` | Execute a single action (click, type, scroll, etc.) |
| GET | `/screenshot?mode=browser\|desktop` | Capture a screenshot |
| POST | `/agent-service/mode` | Switch between browser and desktop mode |

### Dynamic Tool Discovery (MCP)

On startup, the agent loop calls `tools/list` on the Playwright MCP server to dynamically discover available tools. The **system prompt is built from this list**, so the model always knows exactly what tools are available.

### Accessibility Tree Snapshots

For Playwright MCP and Omni Accessibility, the model receives a **text accessibility tree** instead of a screenshot:

```
[ref=S0] Document (role: document)
  └─[ref=S1] Main (role: main)
      ├─[ref=S2] SearchBox (role: searchbox, name="Search")
      └─[ref=S4] Button (role: button, name="Search")
```

The model uses element **refs** to target actions, avoiding pixel coordinates and reducing vision hallucinations.

### WebRTC Video Streaming (Optional)

For low-latency video feedback, install optional dependencies:

```bash
pip install aiortc av
```

WebRTC endpoints become available for real-time video from the container.

---

## Troubleshooting

### Container Won't Start

```bash
# Check Docker daemon
docker info

# Rebuild and start
docker compose build
docker compose up -d --build

# View container logs
docker compose logs -f cua-environment
```

### Frontend Doesn't Connect to Backend

1. Ensure the backend is running: `python -m backend.main`
2. Check that the Vite proxy is active (`npm run dev` in `frontend/`)
3. Verify CORS origins in `backend/api/server.py` include your frontend URL

### Agent Starts But Performs No Actions

- **Playwright MCP 403 Forbidden** — Set `PLAYWRIGHT_MCP_HOST=localhost` (not `127.0.0.1`)
- **MCP not responding** — Check container logs: `docker compose logs cua-environment | grep -i mcp`
- **AT-SPI not initialized** — Verify D-Bus is running inside the container; restart if needed

### Agent Times Out

Increase `STEP_TIMEOUT` in `.env`:

```env
STEP_TIMEOUT=60.0
```

### Screenshots Not Appearing

```bash
# Check agent service
docker compose exec cua-environment curl http://localhost:9222/screenshot

# Check virtual display
docker compose exec cua-environment xdpyinfo -display :99

# Check VNC
docker compose logs cua-environment | grep -i vnc
```

### Model Returns "Unsupported Action"

1. Verify the model is in `backend/allowed_models.json` with the correct capability flags
2. Check `backend/engine_capabilities.json` — the action must be in `allowed_actions` for the selected engine

### API Key Not Found

1. Check the resolution order: UI input → `.env` file → system environment
2. Use `GET /api/key-statuses` to see what the backend detects
3. Verify `.env` syntax: `GOOGLE_API_KEY=sk-...` (no quotes needed)
