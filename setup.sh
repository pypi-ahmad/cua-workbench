#!/usr/bin/env bash
# setup.sh — One-command setup for CUA
#
# Usage:
#   bash setup.sh          # normal setup
#   bash setup.sh --check  # non-destructive prerequisite check
#   bash setup.sh --clean  # DESTRUCTIVE: compose down + prune ALL images/volumes

set -euo pipefail

YELLOW='\033[1;33m'
GREEN='\033[1;32m'
RED='\033[1;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

# ── Check prerequisites ──────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is required. Install: https://docs.docker.com/get-docker/"
command -v python3 >/dev/null 2>&1 || error "Python 3 is required."
command -v node >/dev/null 2>&1 || error "Node.js is required."

docker info >/dev/null 2>&1 || error "Docker daemon is not running. Start Docker and retry."

# ── B-16: Disk-space check ────────────────────────────────────────────────────
MIN_DISK_GB=10
avail_kb=$(df -k . | tail -1 | awk '{print $4}')
avail_gb=$(( avail_kb / 1048576 ))
if (( avail_gb < MIN_DISK_GB )); then
  warn "Low disk space: ${avail_gb}GB available (${MIN_DISK_GB}GB recommended)."
  warn "The Docker image build may fail. Free up space or press Ctrl+C to abort."
  if (( CHECK_ONLY == 0 )); then
    sleep 3
  fi
else
  info "Disk space OK: ${avail_gb}GB available."
fi

info "All prerequisites met."

if (( CHECK_ONLY )); then
  info "Check mode complete."
  exit 0
fi

# ── Optional destructive cleanup ─────────────────────────────────────────────
if [[ "${1:-}" == "--clean" ]]; then
  warn "Running destructive Docker cleanup (--clean): removing compose containers/images/volumes and pruning ALL Docker images/volumes..."
  docker compose down --rmi all -v || true
  docker system prune -a --volumes -f
fi

# ── Build via Compose (source of truth) ──────────────────────────────────────
info "Building Docker image (compose)... this may take several minutes on first run."
if command -v pv >/dev/null 2>&1; then
  docker compose build 2>&1 | pv -l -N "docker-build" > /dev/null
else
  docker compose build --progress=plain 2>&1 | while IFS= read -r line; do
    # Show only key build progress lines
    case "$line" in
      *"Step "*|*"Successfully"*|*"---"*|*"CACHED"*|*"RUN"*|*"COPY"*)
        info "  $line"
        ;;
    esac
  done
fi
info "Docker image built."

# ── Install Python deps ──────────────────────────────────────────────────────
info "Installing Python dependencies..."
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
info "Python dependencies installed."

# ── Install frontend deps ────────────────────────────────────────────────────
info "Installing frontend dependencies..."
pushd frontend >/dev/null
npm install
popd >/dev/null
info "Frontend dependencies installed."

info ""
info "=== Setup complete! ==="
info ""
info "Quick start:"
info "  ./start.sh"
info ""
info "Or run manually:"
info "  1. Start container: docker compose up -d --build"
info "  2. Start backend:   source .venv/bin/activate && python -m backend.main"
info "  3. Start frontend:  cd frontend && npm run dev"
info "  4. Open http://localhost:3000"
info ""