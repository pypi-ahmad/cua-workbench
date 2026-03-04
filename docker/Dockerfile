FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV SCREEN_WIDTH=1440
ENV SCREEN_HEIGHT=900
ENV SCREEN_DEPTH=24
ENV AGENT_SERVICE_PORT=9222
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONPATH=/app

# GUI/runtime dependencies + build prerequisites (single layer)
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-terminal \
    xfce4-goodies \
    dbus-x11 \
    x11vnc \
    xvfb \
    x11-utils \
    websockify \
    novnc \
    ffmpeg \
    scrot \
    wmctrl \
    xclip \
    ydotool \
    xdotool \
    fonts-liberation \
    fonts-noto-core \
    fonts-dejavu-core \
    at-spi2-core \
    libatspi2.0-dev \
    gir1.2-atspi-2.0 \
    gir1.2-gtk-3.0 \
    python3-gi \
    python3-gi-cairo \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    curl \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS 20 (required for Playwright MCP via npx)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN node -v && npm -v && npx -v

# Pre-install Playwright MCP server globally (avoids npx download at runtime)
RUN npm install -g @playwright/mcp@latest

# Create virtual environment WITH system site-packages access.
# python3-gi (GObject Introspection) and gir1.2-atspi-2.0 are apt-only
# packages — the venv must see them for the AT-SPI accessibility engine.
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir playwright -r /tmp/requirements.txt

# Install Playwright browsers + OS deps
RUN playwright install chromium firefox
RUN playwright install-deps

# ── Pre-create Chrome profile to suppress first-run dialogs & keyring ────────
# Static profile creation only — NO browser execution during build.
RUN mkdir -p /tmp/chrome-profile/Default \
    && echo '{"browser":{"check_default_browser":false},"distribution":{"skip_first_run_ui":true}}' \
    > /tmp/chrome-profile/Default/Preferences \
    && touch /tmp/chrome-profile/First\ Run

# Setup working directory
WORKDIR /app

# Copy backend for shared tools (action aliases)
COPY backend /app/backend

# Copy agent service (Playwright-based control)
COPY docker/agent_service.py /app/docker/agent_service.py

# Copy entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 5900 = VNC, 6080 = noVNC web UI, 8931 = Playwright MCP, 9222 = Agent Service API
EXPOSE 5900 6080 8931 9222

# Health check — agent_service /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:9222/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
