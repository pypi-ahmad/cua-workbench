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

# Runtime dependencies only — every entry here must be justified.
#
# KEPT (each is used by the code):
#   xfce4, xfce4-terminal                  → desktop for agent to control
#   dbus-x11, at-spi2-core, gir1.2-*, python3-gi  → accessibility engine
#   x11vnc, xvfb, websockify, novnc         → visible-desktop streaming
#   x11-utils, scrot, wmctrl, xclip, xdotool → agent_service primitives
#   fonts-*                                  → render sites/apps correctly
#   python3, python3-venv                    → agent runtime
#   curl, ca-certificates                    → node install + probes
#   xdg-utils                                → default-browser registration
#
# REMOVED (unused surface — total ~1 GB):
#   ffmpeg, socat, gnupg, wget, file, unzip, p7zip-full, jq, git,
#   net-tools, iputils-ping, dnsutils, flameshot, xournalpp, htop, lsof,
#   strace, vlc, libreoffice*, evince, gedit, gnome-terminal, python3-pip
#   (pip lives inside /opt/venv), python3-gi-cairo (not imported),
#   libatspi2.0-dev (the -dev headers are build-only; the runtime lib
#   libatspi2.0-0 is pulled in transitively by at-spi2-core).
RUN apt-get update && apt-get install -y --no-install-recommends \
    xfce4 \
    xfce4-terminal \
    dbus-x11 \
    x11vnc \
    xvfb \
    x11-utils \
    websockify \
    novnc \
    scrot \
    wmctrl \
    xclip \
    xdotool \
    fonts-liberation \
    fonts-noto-core \
    fonts-dejavu-core \
    at-spi2-core \
    gir1.2-atspi-2.0 \
    gir1.2-gtk-3.0 \
    python3-gi \
    python3 \
    python3-venv \
    curl \
    ca-certificates \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Install Node.js LTS 20 (required for Playwright MCP via npx)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN node -v && npm -v && npx -v

# Pre-install Playwright MCP server globally (avoids npx download at runtime).
# Pinned for build reproducibility — bumping this version is a deliberate
# act so MCP tool-name / input-schema changes are caught in PR review.
RUN npm install -g @playwright/mcp@0.0.70

# Create application venv and install Python dependencies.
# --system-site-packages is REQUIRED so the venv can import python3-gi /
# Atspi (installed at the distro level) for the accessibility engine.
# Without this, AT-SPI health checks fail with "ModuleNotFoundError: gi"
# and the a11y engine silently degrades.
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir playwright

# Install Playwright Chromium browser + its OS-level dependencies
RUN /opt/venv/bin/python -m playwright install --with-deps chromium
RUN /opt/venv/bin/python -m playwright --version

# --- Google Chrome (item 1) ---
RUN wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

# Ensure Chrome binary exists at the path Playwright 'chrome' channel expects.
# Prefer the real google-chrome if installed, otherwise fall back to the
# Playwright-bundled Chromium so that @playwright/mcp can always find a
# working browser at /opt/google/chrome/chrome.
RUN mkdir -p /opt/google/chrome \
 && if [ -x /usr/bin/google-chrome-stable ]; then \
      ln -sf /usr/bin/google-chrome-stable /opt/google/chrome/chrome; \
    elif [ -x /usr/bin/google-chrome ]; then \
      ln -sf /usr/bin/google-chrome /opt/google/chrome/chrome; \
    else \
      CHROMIUM=$(find /ms-playwright -name chrome -type f -executable 2>/dev/null | head -1); \
      [ -n "$CHROMIUM" ] && ln -sf "$CHROMIUM" /opt/google/chrome/chrome \
        || echo 'WARNING: no Chrome/Chromium binary found for symlink'; \
    fi

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
