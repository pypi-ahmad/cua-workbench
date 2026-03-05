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
    socat \
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


# RUN python3 -m venv --system-site-packages /opt/venv
# ENV PATH="/opt/venv/bin:$PATH"

# COPY requirements.txt /tmp/requirements.txt
# RUN pip install --no-cache-dir playwright -r /tmp/requirements.txt

# Install Playwright browsers + OS deps
RUN playwright


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

# --- Brave Browser (item 3) ---
RUN curl -fsSL https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
    -o /usr/share/keyrings/brave-browser-archive-keyring.gpg

RUN echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
    > /etc/apt/sources.list.d/brave-browser-release.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    brave-browser \
    && rm -rf /var/lib/apt/lists/*

# --- Misc utilities (item 4) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    flameshot \
    xournalpp \
    htop \
    lsof \
    strace \
    && rm -rf /var/lib/apt/lists/*

# --- Firefox via official Mozilla APT repo (item 5, no Snap) ---
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
    | gpg --dearmor -o /etc/apt/keyrings/mozilla.gpg

RUN echo "deb [signed-by=/etc/apt/keyrings/mozilla.gpg] https://packages.mozilla.org/apt mozilla main" \
    > /etc/apt/sources.list.d/mozilla.list \
 && printf "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n" \
    > /etc/apt/preferences.d/mozilla

RUN apt-get update && apt-get install -y --no-install-recommends firefox \
    && rm -rf /var/lib/apt/lists/*

# --- Microsoft Edge (item 6) ---
RUN wget -qO- https://packages.microsoft.com/keys/microsoft.asc \
  | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg

RUN echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main" \
  > /etc/apt/sources.list.d/microsoft-edge.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    microsoft-edge-stable \
    && rm -rf /var/lib/apt/lists/*

# --- Desktop apps and tools (item 8) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    vlc \
    libreoffice \
    libreoffice-gtk3 \
    evince \
    gedit \
    gnome-terminal \
    file \
    unzip \
    p7zip-full \
    jq \
    curl \
    wget \
    git \
    net-tools \
    iputils-ping \
    dnsutils \
    xdg-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
