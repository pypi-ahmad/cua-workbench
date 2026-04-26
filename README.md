# CUA Workbench

CUA Workbench is a local development environment for running computer-using agents inside a visible Linux sandbox. It combines a React frontend, a FastAPI orchestration backend, and a Dockerized Ubuntu desktop so you can start an agent session, watch what it does, inspect step-by-step logs, and compare multiple execution engines without leaving the same interface.

## What the App Does

This repository provides a local workbench for:

- Starting agent sessions from plain-language tasks
- Choosing a provider, model, engine, and execution target per session
- Running browser-semantic, accessibility-driven, or native computer-use flows
- Streaming screenshots, logs, and step records back to the UI in real time
- Viewing the sandbox desktop through noVNC or screenshot fallback
- Exporting session history from the frontend as JSON

The current app is a local operator tool, not a hosted platform. Session state is kept in memory, and the repository does not implement sign-in, persistence, or multi-user collaboration.

## Why It Exists

Most agent demos hide execution behind headless automation or loosely defined runtime behavior. This repository exists to make that behavior inspectable and explicit: the selected engine is the engine that runs, the selected model must be allowlisted, and the sandbox remains visible while the agent works.

That makes the repository useful for engineering evaluation, prompt iteration, runtime debugging, and side-by-side comparison of automation styles.

## Implemented Features

- React frontend with a landing page (`/`) and a full-featured `/workbench` operator view
- FastAPI backend with REST endpoints for health, models, engines, container lifecycle, session control, screenshots, and key validation
- WebSocket streaming for screenshots, logs, steps, and session completion
- Three execution engines: `playwright_mcp`, `omni_accessibility`, and `computer_use`
- Dockerized Ubuntu 24.04 sandbox with XFCE, Xvfb, x11vnc, noVNC, Chromium, Playwright MCP, and an internal agent service
- Provider/model validation against `backend/allowed_models.json`
- API key resolution from UI input, `.env`, or system environment variables
- Optional WebRTC negotiation endpoint when additional packages are installed
- Test coverage across engine routing, loop behavior, model policy, transport, and stress scenarios

## Tech Stack

| Layer | Implementation |
| --- | --- |
| Frontend | React 19, React Router 7, Vite 6 |
| Backend | FastAPI, Uvicorn, Pydantic 2, HTTPX, websockets |
| Model providers | Google GenAI SDK, Anthropic SDK |
| Sandbox | Docker, Ubuntu 24.04, XFCE, Xvfb, x11vnc, noVNC |
| Browser tooling | Playwright MCP (pinned to `@playwright/mcp@0.0.70`), Chromium / Google Chrome |
| Image processing | Pillow, NumPy |

## Project Structure

```text
.
├── backend/
│   ├── agent/                  # agent loop, model clients, prompts, screenshots, Playwright MCP client
│   ├── api/                    # FastAPI server and request handling
│   ├── engines/                # accessibility and Computer Use engines
│   ├── health/                 # engine certification and health helpers
│   ├── streaming/              # WebRTC and video-capture support
│   ├── tools/                  # action routing, schemas, aliases
│   └── utils/                  # Docker lifecycle and utility helpers
├── docker/
│   ├── Dockerfile              # Ubuntu desktop image definition
│   ├── entrypoint.sh           # desktop, DBus, VNC, MCP, and agent-service boot sequence
│   └── agent_service.py        # in-container execution service
├── docs/
│   ├── USAGE.md                # detailed operator guide
│   └── assets/                 # architecture diagram used by docs
├── frontend/
│   ├── src/                    # React app, hooks, API client, pages, and components
│   ├── package.json            # frontend scripts and dependencies
│   └── vite.config.js          # dev-server configuration and proxy rules
├── tests/                      # unit, integration, and stress tests
├── docker-compose.yml          # local sandbox topology and published ports
├── requirements.txt            # Python runtime dependencies
├── setup.sh / setup.bat        # setup scripts
└── start.sh / start.bat        # convenience launchers
```

## Prerequisites

- Docker with a running daemon
- Python 3.10+
- Node.js 18+

The Docker image is large enough that low disk space can cause setup to fail. Both setup scripts check for that condition.

## Installation

### Option 1: setup scripts

Linux or macOS:

```bash
bash setup.sh
```

Windows:

```bat
setup.bat
```

These scripts:

- verify Docker, Python, and Node.js are installed
- verify the Docker daemon is running
- build the Docker image
- create `.venv` if needed
- install `requirements.txt`
- install frontend dependencies with `npm install`

Destructive cleanup is available through `--clean`:

```bash
bash setup.sh --clean
```

```bat
setup.bat --clean
```

### Option 2: manual installation

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

## Configuration

### API keys

At least one provider key is required to start a real session.

```env
GOOGLE_API_KEY=your-google-key
ANTHROPIC_API_KEY=your-anthropic-key
```

The backend resolves keys in this order:

1. key entered in the UI
2. key from `.env` in the repository root
3. key from the process environment

### Environment variables actually loaded by `backend.config.Config.from_env()`

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Default model fallback in backend config |
| `CONTAINER_NAME` | `cua-environment` | Docker container name |
| `AGENT_SERVICE_HOST` | `127.0.0.1` | Agent-service host from the backend's perspective |
| `AGENT_SERVICE_PORT` | `9222` | Agent-service port |
| `AGENT_MODE` | `browser` | Default agent mode |
| `PLAYWRIGHT_MCP_HOST` | `localhost` | Playwright MCP host |
| `PLAYWRIGHT_MCP_PORT` | `8931` | Playwright MCP port |
| `PLAYWRIGHT_MCP_PATH` | `/mcp` | Playwright MCP path |
| `PLAYWRIGHT_MCP_AUTOSTART` | `0` | Boolean-like autostart flag |
| `PLAYWRIGHT_MCP_COMMAND` | `npx` | Local MCP command |
| `PLAYWRIGHT_MCP_ARGS` | `-y @playwright/mcp@0.0.70` | Local MCP arguments (pinned for build reproducibility) |
| `PLAYWRIGHT_MCP_DOCKER_TRANSPORT` | `http` | Docker transport mode |
| `HOST` | `127.0.0.1` | Backend bind host |
| `PORT` | `8000` | Backend bind port |
| `SCREEN_WIDTH` | `1440` | Virtual screen width |
| `SCREEN_HEIGHT` | `900` | Virtual screen height |
| `SCREENSHOT_FORMAT` | `png` | Screenshot encoding format |
| `MAX_STEPS` | `50` | Default step budget |
| `ACTION_DELAY_MS` | `100` | Post-action debounce delay in milliseconds |
| `STEP_TIMEOUT` | `30.0` | Per-step timeout |
| `GEMINI_RETRY_ATTEMPTS` | `3` | Retry count used by provider clients |
| `DEBUG` | `0` | Enables backend debug mode |
| `VNC_PASSWORD` | empty | Plaintext VNC password (overridden by `VNC_PASSWORD_FILE` if set inside the container) |

The sandbox container also accepts `VNC_PASSWORD_FILE` (a path to a secret file the entrypoint reads and then unsets) as a safer alternative to passing `VNC_PASSWORD` through the environment.

### Important configuration note

`backend/config.py` defines additional defaults such as `host`, `port`, `action_delay_ms`, `gemini_retry_delay`, `ws_screenshot_interval`, and `screenshot_format`, but those are not currently loaded from environment variables by `Config.from_env()`. This README does not document them as live env controls.

### Optional WebRTC extras

`requirements.txt` intentionally excludes the WebRTC extras. Install them separately if you need `/webrtc/offer`:

```bash
python -m pip install aiortc av
```

## Running Locally

### Option 1: convenience launchers (recommended)

**Linux / macOS:**

```bash
./start.sh
```

**Windows:**

```bat
start.bat
```

Both launchers start the backend on `http://localhost:8000` and the frontend on `http://localhost:3000`. They do not start or stop the Docker container — use the **Start Sandbox** button in the app or `docker compose up -d` to bring the sandbox up first.

To stop all processes:

```bash
./start.sh --stop
```

### Option 2: manual start

```bash
# 1. Activate the virtual environment created by setup.sh
source .venv/bin/activate    # Windows: .venv\Scripts\activate.bat

# 2. Start the sandbox container
docker compose up -d

# 3. Start the backend (separate terminal)
python -m backend.main       # starts on http://localhost:8000

# 4. Start the frontend (separate terminal)
cd frontend && npm run dev   # starts on http://localhost:3000
```

The Vite dev server at port 3000 proxies `/api`, `/ws`, and `/vnc` to `http://localhost:8000`.

> **Note:** The launch scripts do not activate `.venv`. If you used `setup.sh` to create a virtual environment, activate it before running the launch script, or ensure the same packages are available in your default Python installation.

## Available Commands

### Repository-level commands

| Command | What it does |
| --- | --- |
| `bash setup.sh` | Full setup on Linux/macOS |
| `setup.bat` | Full setup on Windows |
| `bash setup.sh --clean` | Destructive cleanup, rebuild path on Linux/macOS |
| `setup.bat --clean` | Destructive cleanup, rebuild path on Windows |
| `./start.sh` | Convenience launcher for backend + frontend on Linux/macOS |
| `./start.sh --stop` | Stops frontend and backend processes started by `start.sh` |
| `start.bat` | Convenience launcher for backend + frontend on Windows |
| `start.bat --stop` | Stops frontend and backend processes started by `start.bat` |
| `docker compose build` | Build the sandbox image |
| `docker compose up -d --build` | Build and run the sandbox container |

### Frontend commands

Defined in `frontend/package.json`:

| Command | What it does |
| --- | --- |
| `npm run dev` | Start the Vite dev server |
| `npm run build` | Build the production frontend bundle |
| `npm run preview` | Preview the built frontend |

## Usage Overview

The frontend exposes two routes:

- `/` — landing page with product overview, feature cards, and "Open Workbench →" link
- `/workbench` — the primary working surface: provider/model/key config, engine selection, task input, live screen, timeline, and logs

Typical flow:

1. Start the Docker container, backend, and frontend.
2. Open the app and confirm the header shows a healthy or reachable state.
3. Choose a provider, model, key source, engine, and run location.
4. Enter a task and start the session.
5. Watch screenshots, logs, and steps as they stream into the UI.
6. Stop the session, inspect history, or export the run as JSON.

## Usage Guide

For the complete operator reference, see **[docs/USAGE.md](docs/USAGE.md)**. It covers every UI flow, configuration option, engine behavior, WebSocket event, API endpoint, troubleshooting step, and known limitation in detail.

Sections:

- [Who This App Is For](docs/USAGE.md#who-this-app-is-for)
- [Before You Start](docs/USAGE.md#before-you-start)
- [Setup and Prerequisites](docs/USAGE.md#setup-and-prerequisites)
- [How to Start the App](docs/USAGE.md#how-to-start-the-app)
- [Main User Workflow](docs/USAGE.md#main-user-workflow)
- [Feature Guide](docs/USAGE.md#feature-guide)
- [Automation Engines](docs/USAGE.md#automation-engines)
- [Input Expectations](docs/USAGE.md#input-expectations)
- [Output and Export](docs/USAGE.md#output-and-export)
- [Session Persistence](docs/USAGE.md#session-persistence)
- [Safety Confirmation](docs/USAGE.md#safety-confirmation)
- [Configuration Reference](docs/USAGE.md#configuration-reference)
- [Backend API Reference](docs/USAGE.md#backend-api-reference)
- [Troubleshooting](docs/USAGE.md#troubleshooting)
- [Limitations and Important Notes](docs/USAGE.md#limitations-and-important-notes)

## API Overview

The backend exposes implemented endpoints for:

- health checks
- model and engine discovery
- API key status and validation
- Docker container lifecycle
- agent-service mode and health
- screenshot retrieval
- session start, stop, status, history, and safety confirmation
- short-lived WebSocket auth token issuance
- optional WebRTC negotiation
- noVNC websocket and static-asset proxying

Core endpoints include:

| Method | Path |
| --- | --- |
| `GET` | `/api/health` |
| `GET` | `/api/health/detailed` |
| `GET` | `/api/models` |
| `GET` | `/api/engines` |
| `GET` | `/api/keys/status` |
| `POST` | `/api/keys/validate` |
| `GET` | `/api/container/status` |
| `POST` | `/api/container/start` |
| `POST` | `/api/container/stop` |
| `POST` | `/api/container/build` |
| `GET` | `/api/container/logs` |
| `GET` | `/api/agent-service/health` |
| `POST` | `/api/agent-service/mode` |
| `GET` | `/api/preflight` |
| `GET` | `/api/screenshot` |
| `POST` | `/api/agent/start` |
| `POST` | `/api/agent/stop/{session_id}` |
| `GET` | `/api/agent/status/{session_id}` |
| `GET` | `/api/agent/history/{session_id}` |
| `POST` | `/api/agent/safety-confirm` |
| `POST` | `/api/session/ws-token` |
| `POST` | `/webrtc/offer` |
| `WebSocket` | `/ws` |
| `WebSocket` | `/vnc/websockify` |
| `GET` | `/vnc/{path:path}` |

The realtime stream is exposed over `WebSocket /ws`. Both `/ws` and `/vnc/websockify` require a same-origin `Origin` header AND a single-use token obtained from `POST /api/session/ws-token` and passed as `?token=<value>`. After connecting, clients should send a `subscribe` message with their `session_id` so the broadcast layer scopes events to that session only.

## Testing

The repository contains pytest-based tests under `tests/` and `tests/stress/`.

Development dependencies (pytest, pytest-asyncio, pytest-cov, httpx) live in `requirements-dev.txt`:

```bash
python -m pip install -r requirements-dev.txt
```

Run the main suite (excluding the stress tier and any `integration`-marked tests, which require a running Docker container and real provider credentials):

```bash
pytest tests --ignore=tests/stress -q -m "not integration"
```

Run the stress scenarios separately:

```bash
pytest tests/stress -v
```

There is also a standalone backend-oriented harness at `backend/tests/stress_system.py`.

## Build and Deployment Notes

The repository supports local Docker image builds and local development/runtime flows.

What is clearly implemented today:

- local Docker image build through `docker compose build`
- local sandbox startup through `docker compose up -d --build`
- frontend production build through `npm run build`
- GitHub Actions CI pipeline at [`.github/workflows/ci.yml`](.github/workflows/ci.yml) covering: backend pytest (fast suite), frontend build, `pip-audit -r requirements.txt --strict`, `npm audit --omit=dev --audit-level=high`, and Trivy (Dockerfile config scan + filesystem secret scan)

What is not documented here as supported because the repo does not define it:

- a production deployment topology
- infrastructure-as-code for cloud deployment
- packaged releases or published container images

## Limitations and Notes

- Session state is in memory only; restarting the backend clears active sessions.
- `computer_use` rejects `execution_target=local` by design; it requires the Docker sandbox.
- The Docker sandbox is local-first; all published ports are bound to `127.0.0.1` in `docker-compose.yml`, the container runs with `cap_drop: ALL` plus a minimal capability add-list, `no-new-privileges:true`, and `pids_limit: 512`. The Chrome DevTools Protocol port (9223) is intentionally not published.
- WebRTC support is optional and requires `aiortc` and `av` installed separately.
- VNC is unauthenticated by default; set `VNC_PASSWORD` (or mount a secret at `VNC_PASSWORD_FILE`) before exposing the app beyond localhost.
- Test dependencies live in `requirements-dev.txt`; install it before running `pytest`.

## Contributing

Contributions are appropriate here because the repository already includes tests, setup scripts, and structured documentation.

When changing behavior, keep these current design constraints intact:

- engine selection should remain explicit
- model exposure should remain rooted in `backend/allowed_models.json`
- public documentation should not claim behavior that the code does not implement
- docs and tests should be updated when runtime behavior changes

## License

This repository is licensed under the MIT License. See [LICENSE](LICENSE).