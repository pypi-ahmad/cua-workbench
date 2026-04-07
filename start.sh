#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# start.sh — One-command launcher for CUA Workbench (Linux / macOS)
#
# Usage:  ./start.sh          Start backend + frontend
#         ./start.sh --stop   Stop background processes
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()  { printf "${GREEN}[CUA]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[CUA]${NC} %s\n" "$*"; }
error() { printf "${RED}[CUA]${NC} %s\n" "$*"; }

# ── Stop mode ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
    info "Stopping CUA Workbench processes..."
    pkill -f "uvicorn backend" 2>/dev/null && info "Backend stopped" || true
    pkill -f "vite" 2>/dev/null && info "Frontend stopped" || true
    exit 0
fi

# ── Pre-flight checks ────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || { error "python3 not found. Install Python 3.10+."; exit 1; }
command -v node    >/dev/null 2>&1 || { error "node not found. Install Node.js 18+."; exit 1; }
command -v docker  >/dev/null 2>&1 || warn "Docker not found — container features will be unavailable."

# Check Python deps
if ! python3 -c "import fastapi" 2>/dev/null; then
    warn "Python dependencies missing. Installing..."
    pip install -r requirements.txt
fi

# Check Node deps
if [ ! -d frontend/node_modules ]; then
    warn "Node modules missing. Installing..."
    (cd frontend && npm install)
fi

# ── .env reminder ─────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    warn "No .env file found. You can provide API keys via the UI or environment variables."
fi

# ── Launch backend ────────────────────────────────────────────────────────────
info "Starting backend on http://localhost:8000 ..."
python3 -m backend.main &
BACKEND_PID=$!

# Wait for backend to be ready (up to 15 seconds)
for i in $(seq 1 15); do
    if curl -s http://localhost:8000/api/health >/dev/null 2>&1; then
        info "Backend ready."
        break
    fi
    sleep 1
done

# ── Launch frontend ───────────────────────────────────────────────────────────
info "Starting frontend on http://localhost:5173 ..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

info "CUA Workbench is running!"
info "  Frontend: http://localhost:5173"
info "  Backend:  http://localhost:8000"
info "  Stop:     ./start.sh --stop  (or Ctrl+C)"

# ── Wait & cleanup ───────────────────────────────────────────────────────────
cleanup() {
    info "Shutting down..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait
