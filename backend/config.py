"""Application configuration with environment-based settings."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env file from project root (does NOT override existing system env vars)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)
    logger.debug("Loaded .env from %s", _ENV_FILE)


@dataclass
class Config:
    """Runtime configuration — values come from env vars or runtime overrides."""

    # Gemini
    gemini_model: str = "gemini-3-flash-preview"

    # Docker container
    container_name: str = "cua-environment"
    container_image: str = "cua-ubuntu:latest"

    # Agent service inside container
    agent_service_host: str = "127.0.0.1"
    agent_service_port: int = 9222
    agent_mode: str = "browser"  # "browser" or "desktop"

    # Playwright MCP service
    playwright_mcp_host: str = "localhost"
    playwright_mcp_port: int = 8931
    playwright_mcp_path: str = "/mcp"
    playwright_mcp_autostart: bool = False
    playwright_mcp_command: str = "npx"
    playwright_mcp_args: str = "-y @playwright/mcp@0.0.70"
    playwright_mcp_docker_transport: str = "http"  # "http" (Streamable HTTP) or "stdio"

    # Screenshot
    screen_width: int = 1440
    screen_height: int = 900
    screenshot_format: str = "png"

    # Agent
    max_steps: int = 50
    # Default post-action delay.  Previously 500 ms → a 50-step session
    # burned 25 s on mandatory sleeps.  Most actions (click, type) settle
    # in well under 100 ms; navigate has its own wait_for logic.  Keep a
    # small debounce so coordinate-based clicks don't race X11 event
    # delivery, but stop over-sleeping.
    action_delay_ms: int = 100
    gemini_retry_attempts: int = 3
    gemini_retry_delay: float = 2.0
    step_timeout: float = 30.0

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # WebSocket
    ws_screenshot_interval: float = 1.5

    # VNC (B-31)
    vnc_password: str = ""  # empty = no password (default for dev)

    @property
    def agent_service_url(self) -> str:
        """Full HTTP URL for the in-container agent service."""
        return f"http://{self.agent_service_host}:{self.agent_service_port}"

    @property
    def playwright_mcp_url(self) -> str:
        """Base HTTP URL for the Playwright MCP server."""
        return f"http://{self.playwright_mcp_host}:{self.playwright_mcp_port}"

    @property
    def playwright_mcp_endpoint(self) -> str:
        """Full MCP JSON-RPC endpoint URL."""
        return f"{self.playwright_mcp_url}{self.playwright_mcp_path}"

    @classmethod
    def from_env(cls) -> Config:
        """Create a Config instance from environment variables."""
        return cls(
            gemini_model=os.getenv("GEMINI_MODEL", cls.gemini_model),
            container_name=os.getenv("CONTAINER_NAME", cls.container_name),
            agent_service_host=os.getenv("AGENT_SERVICE_HOST", cls.agent_service_host),
            agent_service_port=int(os.getenv("AGENT_SERVICE_PORT", str(cls.agent_service_port))),
            agent_mode=os.getenv("AGENT_MODE", cls.agent_mode),
            playwright_mcp_host=os.getenv("PLAYWRIGHT_MCP_HOST", cls.playwright_mcp_host),
            playwright_mcp_port=int(os.getenv("PLAYWRIGHT_MCP_PORT", str(cls.playwright_mcp_port))),
            playwright_mcp_path=os.getenv("PLAYWRIGHT_MCP_PATH", cls.playwright_mcp_path),
            playwright_mcp_autostart=os.getenv("PLAYWRIGHT_MCP_AUTOSTART", "0").lower() in ("1", "true", "yes"),
            playwright_mcp_command=os.getenv("PLAYWRIGHT_MCP_COMMAND", cls.playwright_mcp_command),
            playwright_mcp_args=os.getenv("PLAYWRIGHT_MCP_ARGS", cls.playwright_mcp_args),
            playwright_mcp_docker_transport=os.getenv("PLAYWRIGHT_MCP_DOCKER_TRANSPORT", cls.playwright_mcp_docker_transport),
            screen_width=int(os.getenv("SCREEN_WIDTH", str(cls.screen_width))),
            screen_height=int(os.getenv("SCREEN_HEIGHT", str(cls.screen_height))),
            max_steps=int(os.getenv("MAX_STEPS", str(cls.max_steps))),
            step_timeout=float(os.getenv("STEP_TIMEOUT", str(cls.step_timeout))),
            gemini_retry_attempts=int(os.getenv("GEMINI_RETRY_ATTEMPTS", str(cls.gemini_retry_attempts))),
            debug=os.getenv("DEBUG", "").lower() in ("1", "true", "yes"),
            vnc_password=os.getenv("VNC_PASSWORD", cls.vnc_password),
        )


# Singleton
config = Config.from_env()


# ── API Key Resolution ────────────────────────────────────────────────────────

# Maps provider name → env var name for API keys.
_PROVIDER_KEY_ENV_VARS: Dict[str, str] = {
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


@dataclass
class KeyStatus:
    """Resolution status for a single provider's API key."""

    provider: str
    available: bool = False
    source: str = "none"          # "none" | "env" | "dotenv" | "ui"
    masked_key: str = ""


def _mask_key(key: str) -> str:
    """Return a masked version of an API key for safe display."""
    if len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


def _detect_key_source(env_var: str) -> tuple[Optional[str], str]:
    """Detect where an API key comes from.

    Returns ``(key_value, source_label)``.  Source is ``"env"`` for system
    environment variables, ``"dotenv"`` for .env file values, or ``"none"``
    if not found.

    Heuristic: if the .env file contains the variable, we label it ``"dotenv"``.
    If the variable is set but NOT in the .env file, it's a system env var.
    """
    value = os.environ.get(env_var, "").strip()
    if not value:
        return None, "none"

    # Check if .env file defines this variable
    if _ENV_FILE.exists():
        try:
            env_text = _ENV_FILE.read_text(encoding="utf-8")
            for line in env_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                var_name = stripped.split("=", 1)[0].strip()
                if var_name == env_var:
                    return value, "dotenv"
        except OSError:
            pass

    return value, "env"


def resolve_api_key(provider: str, ui_key: Optional[str] = None) -> tuple[Optional[str], str]:
    """Resolve the API key for *provider* using the priority chain.

    Priority: UI input > .env file > system environment variable.

    Returns ``(key, source)`` where *source* is one of
    ``"ui"``, ``"dotenv"``, ``"env"``, or ``"none"``.
    """
    # 1. UI-provided key (highest priority)
    if ui_key and ui_key.strip():
        return ui_key.strip(), "ui"

    # 2. Environment (.env file or system env var)
    env_var = _PROVIDER_KEY_ENV_VARS.get(provider)
    if env_var:
        value, source = _detect_key_source(env_var)
        if value:
            return value, source

    return None, "none"


def get_all_key_statuses() -> list[dict]:
    """Return the availability status of API keys for all providers."""
    statuses: list[dict] = []
    for provider, env_var in _PROVIDER_KEY_ENV_VARS.items():
        value, source = _detect_key_source(env_var)
        status = KeyStatus(
            provider=provider,
            available=bool(value),
            source=source,
            masked_key=_mask_key(value) if value else "",
        )
        statuses.append({
            "provider": status.provider,
            "available": status.available,
            "source": status.source,
            "masked_key": status.masked_key,
        })
    return statuses
