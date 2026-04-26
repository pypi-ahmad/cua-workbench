"""Screenshot capture via the internal agent service.

Calls the HTTP API exposed by the agent service running inside the
container. Supports 'browser' (Playwright) and 'desktop' (scrot) modes.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from backend.config import config
from backend.utils.agent_auth import get_auth_headers

logger = logging.getLogger(__name__)

# Reusable async client
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return or create the module-level reusable httpx client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0)
    return _http_client


async def capture_screenshot(
    mode: str = "browser",
    engine: str | None = None,
    session_id: str | None = None,
) -> str:
    """Capture a PNG screenshot and return base64 string.

    When *engine* is ``playwright_mcp``, captures the screenshot from the
    MCP-controlled browser (which is a separate headless instance) so the
    model sees what MCP actually controls.  For all other engines the
    screenshot comes from the in-container agent service as before.

    Args:
        mode: 'browser' or 'desktop'.
        engine: Optional engine name — 'playwright_mcp' triggers MCP route.
        session_id: Optional caller session id — threaded through to the
            in-container ``/screenshot?session_id=<sid>`` so each session
            captures into its own ``cua-<sid>-<rand>.png`` tempfile
            (I-008).  Defaults to ``None`` which the service maps to the
            shared ``default`` prefix (still uniquified by tempfile).

    Returns:
        Base64-encoded PNG string.
    """
    # NOTE: playwright_mcp engine uses accessibility-tree snapshots instead
    # of screenshots.  The snapshot is captured directly in the agent loop
    # (loop.py) and sent as text to the model — no image needed.

    # ── Default: screenshot via agent_service ─────────────────────────────
    url = f"{config.agent_service_url}/screenshot?mode={mode}"
    if session_id:
        url += f"&session_id={session_id}"
    client = _get_client()

    try:
        resp = await client.get(url, headers=get_auth_headers())
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(data["error"])

        b64 = data["screenshot"]
        method = data.get("method", "unknown")
        logger.debug("Screenshot captured via %s (%d chars)", method, len(b64))
        return b64

    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.warning("Agent service unreachable, falling back to docker exec: %s", e)
        return await _fallback_docker_screenshot(session_id=session_id)


async def _fallback_docker_screenshot(session_id: str | None = None) -> str:
    """Fallback: grab screenshot via docker exec + scrot.

    Uses a per-call unique path (``/tmp/cua-<sid>-<uuid>.png``) inside
    the container so concurrent fallbacks across sessions do not race
    on a single shared filename (I-008 / F-014).
    """
    import uuid
    name = config.container_name
    sid = (session_id or "default").replace("/", "_")[:64]
    path = f"/tmp/cua-{sid}-fallback-{uuid.uuid4().hex}.png"

    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", name, "scrot", "-z", "-o", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", name, "import", "-window", "root", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Screenshot capture failed: {stderr.decode().strip()}")

    proc_read = await asyncio.create_subprocess_exec(
        "docker", "exec", name, "cat", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc_read.communicate()

    # Best-effort cleanup of the per-call tempfile inside the container.
    cleanup = await asyncio.create_subprocess_exec(
        "docker", "exec", name, "rm", "-f", path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await cleanup.wait()

    if proc_read.returncode != 0 or not stdout:
        raise RuntimeError(f"Failed to read screenshot: {stderr.decode().strip()}")

    b64 = base64.b64encode(stdout).decode("ascii")
    logger.info("Screenshot via fallback: %d bytes", len(stdout))
    return b64


async def get_screenshot_bytes(session_id: str | None = None) -> bytes:
    """Return raw PNG bytes of the current screen."""
    b64 = await capture_screenshot(session_id=session_id)
    return base64.b64decode(b64)


async def check_service_health() -> bool:
    """Check if the internal agent service is responsive."""
    # /health is intentionally unauthenticated (I-002) so the docker
    # HEALTHCHECK and external orchestrators can probe it.
    url = f"{config.agent_service_url}/health"
    client = _get_client()
    try:
        resp = await client.get(url, timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False
