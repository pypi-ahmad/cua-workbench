#!/usr/bin/env bash
# setup.sh — One-command setup for CUA
# Usage: bash setup.sh

set -euo pipefail

YELLOW='\033[1;33m'
GREEN='\033[1;32m'
RED='\033[1;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Check prerequisites ──────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is required. Install: https://docs.docker.com/get-docker/"
command -v python3 >/dev/null 2>&1 || error "Python 3 is required."
command -v node >/dev/null 2>&1 || error "Node.js is required."

info "All prerequisites met."

# ── Build Docker image ────────────────────────────────────────────────────────
info "Building CUA Docker image..."
docker build -t cua-ubuntu:latest -f docker/Dockerfile .
info "Docker image built."

# ── Install Python deps ──────────────────────────────────────────────────────
info "Installing Python dependencies..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
info "Python dependencies installed."

# ── Install frontend deps ────────────────────────────────────────────────────
info "Installing frontend dependencies..."
cd frontend
npm install
cd ..
info "Frontend dependencies installed."

info ""
info "=== Setup complete! ==="
info ""
info "To run the system:"
info "  1. Start backend:  source .venv/bin/activate && python -m backend.main"
info "  2. Start frontend: cd frontend && npm run dev"
info "  3. Open http://localhost:3000"
info ""
info "The Docker container will start automatically when you start the agent."
