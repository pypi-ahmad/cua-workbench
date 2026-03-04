#!/bin/bash

set -e

export DISPLAY=:99
export SCREEN_WIDTH=${SCREEN_WIDTH:-1440}
export SCREEN_HEIGHT=${SCREEN_HEIGHT:-900}
export SCREEN_DEPTH=${SCREEN_DEPTH:-24}
export PATH="$PATH:/usr/bin:/usr/local/bin"
export PYTHONPATH=/app

echo "=== CUA Container Starting (XFCE4 Mode) ==="

# ─────────────────────────────────────────────
# 1. DBus (system + session)
# ─────────────────────────────────────────────
mkdir -p /var/run/dbus
dbus-daemon --system --fork 2>/dev/null || true
eval $(dbus-launch --sh-syntax)
export DBUS_SESSION_BUS_ADDRESS
echo "[DBus] Session bus: $DBUS_SESSION_BUS_ADDRESS"

# ─────────────────────────────────────────────
# 2. Xvfb (virtual framebuffer)
# ─────────────────────────────────────────────
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} \
    -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Wait for Xvfb to be ready (poll for DISPLAY)
for i in $(seq 1 20); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "[Xvfb] Display :99 ready"
        break
    fi
    sleep 0.25
done

if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi

# Verify X server is reachable (critical for xdotool)
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "ERROR: X server on :99 not reachable"
    exit 1
fi

# ─────────────────────────────────────────────────
# 3a. AT-SPI accessibility bridge (BEFORE desktop — apps must see these vars)
# ─────────────────────────────────────────────────
export NO_AT_BRIDGE=0
export GTK_MODULES=gail:atk-bridge
export QT_ACCESSIBILITY=1
export ACCESSIBILITY_ENABLED=1

# Enable toolkit accessibility (gsettings needs dbus, already running)
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

# Start AT-SPI registry daemon (required for accessibility tree queries)
/usr/libexec/at-spi2-registryd --dbus-name=org.a11y.atspi.Registry &
ATSPI_PID=$!
sleep 0.5

# Quick import check (non-fatal)
if python3 -c "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; Atspi.init(); print('AT-SPI OK')" 2>/dev/null; then
    echo "[A11y] AT-SPI registry daemon running and bindings verified"
else
    echo "[A11y] WARNING: AT-SPI init check failed — accessibility engine may not work"
    echo "[A11y] Attempting start via dbus activation..."
    dbus-send --session --dest=org.a11y.Bus --print-reply /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null || true
fi

# ─────────────────────────────────────────────
# 3b. XFCE4 Desktop + Window Manager
# ─────────────────────────────────────────────
echo "[Desktop] Starting XFCE4..."
startxfce4 &

# Wait for the window manager to be fully operational
for i in $(seq 1 30); do
    if xdotool getactivewindow >/dev/null 2>&1; then
        echo "[Desktop] Window manager ready"
        break
    fi
    sleep 0.5
done

# Give AT-SPI time to register desktop apps (XFCE panels, file manager, etc.)
sleep 1

# Verify AT-SPI sees desktop applications
A11Y_APPS=$(python3 -c "
import gi; gi.require_version('Atspi', '2.0')
from gi.repository import Atspi; Atspi.init()
d=Atspi.get_desktop(0); print(d.get_child_count())
" 2>/dev/null || echo "0")
echo "[A11y] AT-SPI registered applications: ${A11Y_APPS}"
if [ "$A11Y_APPS" = "0" ]; then
    echo "[A11y] WARNING: No applications registered with AT-SPI after desktop start"
fi

# ─────────────────────────────────────────────
# 4. x11vnc
# ─────────────────────────────────────────────
echo "[VNC] Starting x11vnc..."
if [ -n "$VNC_PASSWORD" ]; then
    mkdir -p /root/.vnc
    x11vnc -storepasswd "$VNC_PASSWORD" /root/.vnc/passwd
    x11vnc -display :99 -forever -rfbauth /root/.vnc/passwd -shared -rfbport 5900 -bg -o /var/log/x11vnc.log
else
    echo "[VNC] WARNING: No VNC_PASSWORD set — VNC access is unauthenticated"
    x11vnc -display :99 -forever -nopw -shared -rfbport 5900 -bg -o /var/log/x11vnc.log
fi

# ─────────────────────────────────────────────
# 5. noVNC (Web access)
# ─────────────────────────────────────────────
echo "[noVNC] Starting websockify..."
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# ─────────────────────────────────────────────
# 5b. Browser bootstrap — default browser + pre-warm Chrome profile
# ─────────────────────────────────────────────
echo "[Browser] Configuring default browser..."
# Set Google Chrome as the default web browser for xdg-open
if command -v google-chrome >/dev/null 2>&1; then
    xdg-settings set default-web-browser google-chrome.desktop 2>/dev/null || true
    # Ensure Chrome profile directory exists (seeded at build time)
    mkdir -p /tmp/chrome-profile/Default
    echo "[Browser] ✓ Chrome set as default browser (profile seeded at build time)"
elif command -v firefox >/dev/null 2>&1; then
    xdg-settings set default-web-browser firefox.desktop 2>/dev/null || true
    echo "[Browser] ✓ Firefox set as default browser"
else
    echo "[Browser] WARNING: No browser found for xdg-settings"
fi

# ─────────────────────────────────────────────
# 6. Playwright MCP server (a11y-tree browser control)
# ─────────────────────────────────────────────
MCP_PORT=${PLAYWRIGHT_MCP_PORT:-8931}
MCP_LOG="/var/log/mcp-server.log"
echo "[MCP] Starting Playwright MCP server on port ${MCP_PORT} (HTTP transport)..."

# --port:         activates HTTP/SSE transport (default without --port is stdio)
# --host 0.0.0.0: listen on all interfaces so the host can reach the port
# --no-sandbox:   Chrome refuses to run as root without it (Docker runs as root)
# --headless:     avoids competing for X11 display with agent_service's browser
MCP_ARGS="--port ${MCP_PORT} --host 0.0.0.0 --no-sandbox --headless"

if command -v playwright-mcp >/dev/null 2>&1; then
    playwright-mcp ${MCP_ARGS} 2>"${MCP_LOG}" &
    MCP_PID=$!
else
    # Fallback to npx with explicit package
    npx @playwright/mcp@latest ${MCP_ARGS} 2>"${MCP_LOG}" &
    MCP_PID=$!
fi

# Wait for MCP server — check with a proper JSON-RPC initialize probe
MCP_READY=false
for i in $(seq 1 30); do
    # First try a simple TCP connect check
    if curl -sf -o /dev/null -w '' http://localhost:${MCP_PORT} 2>/dev/null; then
        MCP_READY=true
        echo "[MCP] Playwright MCP server responding on port ${MCP_PORT}"
        break
    fi
    # Also try JSON-RPC initialize (MCP servers may not respond to plain GET)
    HTTP_CODE=$(curl -sf -o /dev/null -w '%{http_code}' \
        -X POST http://localhost:${MCP_PORT}/mcp \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
        2>/dev/null) || true
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
        MCP_READY=true
        echo "[MCP] Playwright MCP server ready (JSON-RPC) on port ${MCP_PORT}"
        break
    fi
    sleep 0.5
done

if [ "$MCP_READY" = "false" ]; then
    echo "[MCP] WARNING: MCP server not yet responding after 15s"
    if [ -f "${MCP_LOG}" ]; then
        echo "[MCP] Server stderr:"
        tail -20 "${MCP_LOG}"
    fi
    # Check for port-bind failure in logs
    if grep -qi 'EADDRINUSE\|address already in use\|bind.*failed' "${MCP_LOG}" 2>/dev/null; then
        echo "[MCP] ERROR: Port ${MCP_PORT} already in use — cannot bind MCP server"
    fi
    # Check if process is still alive
    if ! kill -0 $MCP_PID 2>/dev/null; then
        echo "[MCP] ERROR: MCP server process (PID $MCP_PID) died"
        echo "[MCP] Attempting restart..."
        npx @playwright/mcp@latest ${MCP_ARGS} 2>"${MCP_LOG}" &
        MCP_PID=$!
        sleep 3
        if kill -0 $MCP_PID 2>/dev/null; then
            echo "[MCP] Restarted MCP server (PID $MCP_PID)"
        else
            echo "[MCP] ERROR: MCP server restart also failed"
            tail -20 "${MCP_LOG}" 2>/dev/null
        fi
    fi
fi

# ─────────────────────────────────────────────
# 8. Pre-flight verification
# ─────────────────────────────────────────────
echo "[Verify] Running pre-flight checks..."

# X server check
if xdotool getmouselocation >/dev/null 2>&1; then
    echo "[Verify] ✓ X server + xdotool operational"
else
    echo "[Verify] ✗ xdotool cannot reach X server on DISPLAY=$DISPLAY"
fi

# ─────────────────────────────────────────────
# 8. Agent service
# ─────────────────────────────────────────────
# ── Hard-check desktop tool binaries ─────────────────────────────────
command -v xdotool  || echo "ERROR: xdotool missing from PATH"
command -v wmctrl   || echo "ERROR: wmctrl missing from PATH"
command -v xclip    || echo "ERROR: xclip missing from PATH"

echo "[Agent] Starting internal agent service..."
PYTHONPATH=/app /opt/venv/bin/python /app/docker/agent_service.py &
AGENT_PID=$!

echo "=== XFCE4 Desktop Ready ==="
echo "Access via: http://localhost:6080"
echo "MCP server: http://localhost:${MCP_PORT}"

trap "kill $XVFB_PID $AGENT_PID $MCP_PID $YDOTOOL_PID $ATSPI_PID 2>/dev/null; exit 0" SIGTERM SIGINT

wait $AGENT_PID
