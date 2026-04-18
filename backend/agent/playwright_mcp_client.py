"""Playwright MCP client — STDIO transport (local) and HTTP transport (Docker).

Playwright MCP: https://github.com/microsoft/playwright-mcp

Communicates with the Playwright MCP server over:
  • STDIO (local): spawns npx @playwright/mcp@latest as a child process
  • HTTP  (Docker): connects to the MCP server inside the Docker container

Uses the ``mcp`` Python SDK's ``stdio_client`` / ``streamable_http_client``
to manage the connection.

The MCP server command defaults to ``npx -y @playwright/mcp@latest`` and
can be overridden via ``PLAYWRIGHT_MCP_COMMAND`` / ``PLAYWRIGHT_MCP_ARGS``
environment variables.

Troubleshooting (Docker HTTP)
-----------------------------

**Symptom:** Agent starts but performs 0 actions ("Action History (0)");
VNC desktop and screenshot health endpoints appear alive, yet the agent
never interacts with the browser.

**Root cause:** ``session.initialize()`` sends a JSON-RPC POST to the MCP
server and receives HTTP **403 Forbidden**.  MCP never becomes ready, so
the agent loop has no tool session and silently does nothing.

  • **Host-header mismatch** — connecting to ``http://127.0.0.1:8931/mcp``
    sends ``Host: 127.0.0.1:8931``.  Playwright MCP's built-in
    allowed-host check may only accept ``localhost``.  Fix: set
    ``PLAYWRIGHT_MCP_HOST=localhost`` so the Host header matches.
  • **Source-IP localhost-only** — Docker port-forwarding / NAT can
    make the MCP server see the peer address as non-loopback, so it
    denies the request.  Fix: run backend in the same container or
    network namespace, or pass server flags to allow remote clients.
  • **socat relay** does NOT rewrite HTTP Host headers — it only
    forwards raw TCP, so Host-header rejections are still possible.
  • **Headless MCP** is invisible in VNC (the Chromium window has no
    GUI); this is separate from connectivity issues.

Note: ``curl -I <mcp_url>`` returning 403 can be *normal* (the server
only accepts POST with a JSON-RPC body).  However, 403 on the actual
``initialize`` POST is a hard blocker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.config import config

logger = logging.getLogger(__name__)

# ── Module-level state ────────────────────────────────────────────────────────

_exit_stack: AsyncExitStack | None = None
_mcp_session: ClientSession | None = None
_mcp_init_lock: asyncio.Lock | None = None

# Per-call context for structured logging (set by execute_mcp_action)
_current_step: int = 0
_current_action: str = "unknown"

# Target-aware transport: "local" runs npx on host via STDIO, "docker"
# connects to the container's MCP HTTP server at http://host:port/mcp.
_mcp_target: str = "local"
_mcp_server_key: Optional[str] = None

# Timeout for Docker MCP session initialization (connect + initialize + list_tools)
_DOCKER_MCP_INIT_TIMEOUT: float = 15.0

# ── Disallowed MCP tools (security denylist) ──────────────────────────────────
# Tools filtered out of discovery and rejected at the call boundary.
#
# ``browser_run_code`` allows the LLM to run arbitrary Playwright / Node code
# in the MCP server process.  The server evaluates the code inside a Node
# ``vm`` context which is NOT a security boundary — standard sandbox-escape
# techniques (e.g. ``page.constructor.constructor('return process')()``)
# reach Node's ``child_process`` and give full RCE.  Per upstream guidance
# (``microsoft/playwright-mcp`` security policy): *"Neither Playwright MCP
# nor the underlying Browser can serve as a security boundary."*  With
# execution_target=local this is direct RCE on the operator's host via any
# prompt-injection on a visited page.  We therefore refuse to expose it.
_DISALLOWED_MCP_TOOLS: frozenset[str] = frozenset({"browser_run_code"})


# ── browser_evaluate JS payload denylist (defense-in-depth) ───────────────────
# ``browser_evaluate`` runs LLM-supplied JS in the page context.  Prompt
# injection on a visited page can cause the model to emit a script that
# exfiltrates auth state.  We refuse the most common exfil primitives
# server-side; the model will see a structured error and is expected to
# fall back to legitimate tools (browser_get_text / browser_snapshot).
# This is NOT a sandbox — a determined attacker can string-build around
# any single substring — but it raises the bar meaningfully and gives a
# detection signal.  The upstream MCP server has no such filter today.
_EVALUATE_JS_DENY_PATTERNS: tuple[str, ...] = (
    "document.cookie",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "navigator.credentials",
    "navigator.serviceWorker",
    "fetch(",
    "XMLHttpRequest",
    "new WebSocket",
    "navigator.sendBeacon",
    "import(",
)


def _is_evaluate_js_safe(code: str) -> tuple[bool, str]:
    """Return (allowed, reason).  Pattern check is case-insensitive."""
    if not code:
        return True, ""
    lower = code.lower()
    for pat in _EVALUATE_JS_DENY_PATTERNS:
        if pat.lower() in lower:
            return False, f"browser_evaluate payload contains disallowed token: {pat!r}"
    return True, ""


def _filter_discovered_tools(tools: list[dict]) -> list[dict]:
    """Drop denylisted tools from a discovered-tools list."""
    filtered = [t for t in tools if t.get("name") not in _DISALLOWED_MCP_TOOLS]
    dropped = [t.get("name") for t in tools if t.get("name") in _DISALLOWED_MCP_TOOLS]
    if dropped:
        logger.warning(
            "Filtered disallowed MCP tools from discovery: %s", ", ".join(dropped)
        )
    return filtered


# ── Discovered tools (from MCP server via tools/list) ─────────────────────────
# Populated during session initialization.  Each entry:
#   {"name": str, "description": str, "inputSchema": dict}
# This is the canonical source of truth — the server defines what tools exist,
# the client discovers and adapts.
_discovered_tools: list[dict] = []


def get_discovered_tools() -> list[dict]:
    """Return tools discovered from the Playwright MCP server.

    The list is populated during session initialization (STDIO or Docker
    HTTP) by calling ``session.list_tools()``.  Each entry contains the
    tool ``name``, ``description``, and ``inputSchema`` exactly as the
    server reported them.

    Used by ``prompts.py`` to build the system prompt dynamically — so
    the prompt always reflects what the server actually provides.
    """
    return list(_discovered_tools)


def _hint_for_http_403(mcp_url: str) -> str:
    """Return a targeted troubleshooting hint for HTTP 403 during MCP init.

    Called when the Docker MCP server rejects the JSON-RPC POST with 403
    Forbidden, which silently blocks all agent actions ("Action History (0)").
    """
    return (
        f"HTTP 403 Forbidden from Docker MCP server at {mcp_url}.\n"
        "This blocks session.initialize() — the agent will perform 0 actions.\n"
        "Likely causes and fixes (try in order):\n"
        "  1) Host-header mismatch: if PLAYWRIGHT_MCP_HOST is '127.0.0.1', "
        "change it to 'localhost' so the Host header reads 'localhost:<port>' "
        "which Playwright MCP's allowed-host check accepts.\n"
        "  2) If the backend is not in the same host/network namespace as the "
        "container, use a real HTTP reverse-proxy (e.g. nginx/caddy) to rewrite "
        "the Host header, or relax the server's allowed-host / origin policy.\n"
        "  3) If the server enforces source-IP localhost-only checks, Docker "
        "port-forwarding makes the peer appear non-loopback. Run the backend "
        "in the same container/network namespace, or use supported MCP server "
        "flags to allow remote clients.\n"
        f"  MCP URL: {mcp_url}"
    )


def set_mcp_target(target: str) -> None:
    """Set the execution target for subsequent MCP sessions.

    Must be called **once per agent run** before any MCP action.
    When *target* is ``"docker"``, the MCP client connects to the
    Playwright MCP HTTP server running inside the container
    (``http://{host}:{port}/mcp``) via Streamable HTTP transport.
    When *target* is ``"local"``, the MCP client spawns ``npx``
    as a child process via STDIO transport.
    """
    global _mcp_target
    _mcp_target = "docker" if target == "docker" else "local"
    logger.info("MCP target set to '%s' (transport: %s)",
                _mcp_target, "HTTP" if _mcp_target == "docker" else "STDIO")


def _build_server_params() -> StdioServerParameters:
    """Build STDIO server params for *local* target only.

    Raises ``RuntimeError`` when called with target=docker — Docker runs
    must use the HTTP transport via ``_docker_mcp_call()`` instead of
    tunnelling STDIO through ``docker exec``.
    """
    if _mcp_target == "docker":
        raise RuntimeError(
            "STDIO transport is disabled for target=docker. "
            "Use the HTTP transport (_docker_mcp_call) instead."
        )

    local_args = shlex.split(config.playwright_mcp_args)
    return StdioServerParameters(
        command=config.playwright_mcp_command,
        args=local_args,
    )


# ── Logging helpers ───────────────────────────────────────────────────────────

def _log_mcp_call(
    method: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    """Emit a structured log entry for every MCP tool call."""
    connected = (
        _docker_mcp_session is not None if _mcp_target == "docker"
        else _mcp_session is not None
    )
    log_data = {
        "step": _current_step,
        "action": _current_action,
        "connected": connected,
        "mcp_method": method,
        "status": status,
    }
    if error:
        log_data["error"] = error
        logger.warning(
            "MCP %s → %s: %s | step=%d action=%s connected=%s",
            method, status, error, _current_step, _current_action, connected,
        )
    else:
        logger.info(
            "MCP %s → %s | step=%d action=%s connected=%s",
            method, status, _current_step, _current_action, connected,
        )


# ── Lock helper ───────────────────────────────────────────────────────────────

def _get_init_lock() -> asyncio.Lock:
    """Return or create the MCP initialisation lock."""
    global _mcp_init_lock
    if _mcp_init_lock is None:
        _mcp_init_lock = asyncio.Lock()
    return _mcp_init_lock


# ── STDIO Connection Management ──────────────────────────────────────────────

async def _ensure_mcp_initialized() -> ClientSession:
    """Ensure the STDIO MCP session is connected and initialized.

    Spawns the Playwright MCP server as a child process on first call.
    Subsequent calls return the existing session.  Thread-safe via lock.

    Only used for ``target=local``.  Docker uses HTTP transport via
    ``_ensure_docker_mcp_initialized()``.
    """
    global _exit_stack, _mcp_session, _mcp_server_key

    server_params = _build_server_params()
    new_key = f"{server_params.command}::{server_params.args}"

    # If the target switched (local <-> docker), tear down the old session.
    if _mcp_session is not None and _mcp_server_key != new_key:
        logger.info("MCP target changed (%s → %s) — resetting session", _mcp_server_key, new_key)
        await _reset_session()

    if _mcp_session is not None:
        return _mcp_session

    async with _get_init_lock():
        # Double-check after acquiring lock
        if _mcp_session is not None:
            return _mcp_session

        logger.info(
            "Starting Playwright MCP server via STDIO (%s): %s %s",
            _mcp_target,
            server_params.command,
            " ".join(str(a) for a in server_params.args),
        )

        _exit_stack = AsyncExitStack()
        await _exit_stack.__aenter__()

        try:
            read_stream, write_stream = await _exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await _exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            # Discover tools from the server (MCP tools/list)
            global _discovered_tools
            tools_result = await session.list_tools()
            _discovered_tools = _filter_discovered_tools([
                {
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "inputSchema": getattr(t, "inputSchema", {}) or {},
                }
                for t in tools_result.tools
            ])
            tool_names = [t["name"] for t in _discovered_tools]
            logger.info(
                "MCP STDIO session established — %d tools discovered: %s",
                len(tool_names),
                ", ".join(tool_names[:10])
                + ("..." if len(tool_names) > 10 else ""),
            )

            _mcp_session = session
            _mcp_server_key = new_key
            return _mcp_session
        except Exception:
            # Clean up on failure
            try:
                await _exit_stack.aclose()
            except Exception:
                pass
            _exit_stack = None
            _mcp_session = None
            raise


async def _reset_session() -> None:
    """Tear down the current STDIO session for reconnection."""
    global _exit_stack, _mcp_session

    if _exit_stack:
        try:
            await _exit_stack.aclose()
        except Exception as e:
            logger.warning("STDIO session cleanup error: %s", e)

    _exit_stack = None
    _mcp_session = None


# ── Text extraction helpers ───────────────────────────────────────────────────

def _text_from_content(content: list) -> str:
    """Extract all text parts from a ``CallToolResult.content`` list."""
    parts = []
    for item in content:
        if hasattr(item, "text"):
            parts.append(item.text)
    return "\n".join(parts) if parts else ""


def _mcp_text_from_result(result: dict) -> str:
    """Extract human-readable text from a raw MCP JSON-RPC result dict.

    .. deprecated:: Use ``_text_from_content`` for STDIO results.
       Kept for backward compatibility.
    """
    if not isinstance(result, dict):
        return str(result)[:500]
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
        ]
        if parts:
            return "\n".join(parts)
    return str(result)[:500]


# ── Core MCP call ─────────────────────────────────────────────────────────────

async def _mcp_call_stdio(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via the local STDIO session.

    Returns ``{"success": bool, "message": str}``.
    Auto-reconnects once if the session has dropped.
    """
    for attempt in range(2):
        try:
            session = await _ensure_mcp_initialized()
            _log_mcp_call(tool_name, "calling[stdio]")
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                text = _text_from_content(result.content)
                _log_mcp_call(tool_name, "tool_error[stdio]", error=text)
                return {"success": False, "message": text or "MCP tool returned error"}

            text = _text_from_content(result.content)
            _log_mcp_call(tool_name, "ok[stdio]")
            return {"success": True, "message": text}

        except Exception as e:
            _log_mcp_call(tool_name, "exception[stdio]", error=str(e))
            if attempt == 0:
                logger.info("MCP STDIO call failed — resetting session and retrying")
                await _reset_session()
                continue
            return {"success": False, "message": f"MCP call failed after retry: {e}"}

    return {"success": False, "message": "MCP call failed: exhausted retries"}


async def _mcp_call(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool, routing to the correct transport.

    • ``target=docker`` → HTTP transport via ``_docker_mcp_call()``
    • ``target=local``  → STDIO transport via ``_mcp_call_stdio()``

    Returns ``{"success": bool, "message": str}``.

    Denylisted tools (see ``_DISALLOWED_MCP_TOOLS``) are refused here as
    defense-in-depth — even if an LLM bypasses discovery and requests a
    denied tool name directly, the call never reaches the MCP server.
    """
    if tool_name in _DISALLOWED_MCP_TOOLS:
        logger.warning(
            "Refused disallowed MCP tool call: %s (denylist=%s)",
            tool_name,
            sorted(_DISALLOWED_MCP_TOOLS),
        )
        return {
            "success": False,
            "message": (
                f"Tool '{tool_name}' is disabled for security reasons. "
                f"Use semantic tools (browser_click, browser_type, browser_snapshot) instead."
            ),
        }
    # Defense-in-depth: when the LLM provides ``browser_evaluate`` args
    # directly (the "direct passthrough" path bypasses _build_mcp_args),
    # apply the JS denylist here too.
    if tool_name == "browser_evaluate":
        candidate = ""
        if isinstance(arguments, dict):
            candidate = str(arguments.get("function") or arguments.get("expression") or "")
        ok, reason = _is_evaluate_js_safe(candidate)
        if not ok:
            logger.warning("Refused browser_evaluate at _mcp_call boundary: %s", reason)
            return {
                "success": False,
                "message": (
                    f"{reason}.  Use browser_get_text / browser_snapshot to read "
                    "page content instead — the agent must not access cookies, "
                    "storage, or make outbound requests via evaluate."
                ),
            }
    if _mcp_target == "docker":
        return await _docker_mcp_call(tool_name, arguments)
    return await _mcp_call_stdio(tool_name, arguments)


# ── Accessibility tree ref resolution ─────────────────────────────────────────

def _extract_ref_from_snapshot(snapshot_text: str, target: str) -> str | None:
    """Find an accessibility-tree ref matching *target* in *snapshot_text*.

    Returns the ref string (e.g. ``"S12"``) or ``None`` when no match.
    """
    if not target:
        return None

    # If the caller already provided a valid ref, return it directly.
    direct_ref = re.fullmatch(r"[A-Za-z]\d+", target.strip())
    if direct_ref:
        return target.strip()

    if not snapshot_text:
        return None

    target_lower = target.lower().strip()

    best_ref: str | None = None
    for line in snapshot_text.splitlines():
        if "[ref=" not in line:
            continue
        match = re.search(r"\[ref=([^\]]+)\]", line)
        if not match:
            continue
        ref = match.group(1)
        if best_ref is None:
            best_ref = ref
        if target_lower in line.lower():
            return ref
    return best_ref


async def _resolve_ref(element: str) -> str | None:
    """Take a browser snapshot and resolve *element* to its accessibility ref."""
    if not element:
        return None

    for attempt in range(2):
        snapshot = await _mcp_call("browser_snapshot", {})
        if snapshot.get("success"):
            ref = _extract_ref_from_snapshot(snapshot.get("message", ""), element)
            if ref:
                return ref
        if attempt == 0:
            await asyncio.sleep(0.2)

    return None


# Roles / tags that are considered input-like (fillable / typable).
_INPUT_ROLES = frozenset({
    "textbox", "combobox", "searchbox", "spinbutton",
    "input", "textarea", "select",
})


def _extract_input_ref_from_snapshot(snapshot_text: str, target: str) -> str | None:
    """Resolve *target* to a **fillable** element ref from *snapshot_text*.

    If *target* already looks like a ref (e.g. ``"e22"``), we still
    validate that the matching row in the snapshot corresponds to an
    input-like element (textbox, combobox, searchbox, etc.).  If the
    given ref does not point to a fillable element we scan the snapshot
    for the nearest input and return that instead.

    This prevents the common Gemini mis-targeting bug where the model
    provides a ref for a ``<tr>`` or ``<div>`` instead of the actual
    ``<input>`` / ``<combobox>``.
    """
    if not target or not snapshot_text:
        return None

    target_stripped = target.strip()
    is_ref_pattern = bool(re.fullmatch(r"[A-Za-z]\d+", target_stripped))

    lines = snapshot_text.splitlines()

    # Helper: check whether a snapshot line describes a fillable role.
    def _is_input_line(line: str) -> bool:
        ll = line.lower()
        for role in _INPUT_ROLES:
            if role in ll:
                return True
        return False

    # If the model gave an explicit ref, verify it's actually inputtable.
    if is_ref_pattern:
        for line in lines:
            if f"[ref={target_stripped}]" in line:
                if _is_input_line(line):
                    return target_stripped  # ref is valid and fillable
                # ref exists but not an input — fall through to scan
                break

    # Scan all lines for a fillable element.
    # Prefer a line matching *target* text; otherwise take the first input.
    target_lower = target_stripped.lower()
    first_input_ref: str | None = None
    for line in lines:
        if "[ref=" not in line:
            continue
        match = re.search(r"\[ref=([^\]]+)\]", line)
        if not match:
            continue
        ref = match.group(1)
        if _is_input_line(line):
            if first_input_ref is None:
                first_input_ref = ref
            if target_lower in line.lower():
                return ref

    return first_input_ref


async def _resolve_input_ref(element: str) -> str | None:
    """Take a browser snapshot and resolve *element* to a **fillable** ref.

    Unlike ``_resolve_ref`` which accepts any element type, this variant
    specifically seeks ``textbox``, ``combobox``, ``searchbox``, etc.
    so that fill / type actions target the correct node.
    """
    if not element:
        return None

    for attempt in range(2):
        snapshot = await _mcp_call("browser_snapshot", {})
        if snapshot.get("success"):
            ref = _extract_input_ref_from_snapshot(
                snapshot.get("message", ""), element,
            )
            if ref:
                return ref
        if attempt == 0:
            await asyncio.sleep(0.2)

    # Final fallback: use generic resolution so we don't return None
    # when the snapshot has unusual roles.
    return await _resolve_ref(element)


async def _self_heal_input(
    result: dict,
    element: str,
    text: str,
    original_ref: str,
) -> dict:
    """Reactive self-heal: if a fill/type failed because the ref wasn't an
    input element, take a fresh snapshot, find the real input, click it to
    focus, and retry the type.

    Returns the original *result* unchanged when self-heal is not needed.
    """
    msg = result.get("message", "")
    if result.get("success") or "not an <input>" not in msg.lower():
        return result

    logger.warning(
        "Fill/type target ref=%s is not an input — attempting self-heal",
        original_ref,
    )
    snapshot = await _mcp_call("browser_snapshot", {})
    if not snapshot.get("success"):
        return result

    fallback_ref = _extract_input_ref_from_snapshot(
        snapshot.get("message", ""), element,
    )
    if not fallback_ref or fallback_ref == original_ref:
        return result

    # Click to focus, then type
    await _mcp_call("browser_click", {"element": element, "ref": fallback_ref})
    return await _mcp_call(
        "browser_type",
        {"element": element, "ref": fallback_ref, "text": text},
    )



# ── MCP Action Helpers ─────────────────────────────────────────────────────────

async def mcp_get_accessibility_tree() -> dict:
    """Retrieve the full accessibility tree snapshot."""
    return await _mcp_call("browser_snapshot", {})


async def _validate_browser_context(nav_result: dict) -> dict:
    """Verify browser context exists after navigation.

    Takes a snapshot to confirm at least one page is open.  If the
    snapshot fails, the STDIO session is reset and re-initialised.
    """
    try:
        snapshot = await _mcp_call("browser_snapshot", {})
        if snapshot.get("success"):
            return nav_result  # browser is alive
    except Exception:
        pass

    # Browser context not detected — reset and reinitialize the correct transport
    logger.warning(
        "Browser context not detected after navigation — resetting MCP session (target=%s)",
        _mcp_target,
    )
    if _mcp_target == "docker":
        await _reset_docker_session()
        try:
            await _ensure_docker_mcp_initialized()
        except Exception as reinit_err:
            return {
                "success": False,
                "message": f"Browser validation failed and Docker reinit errored: {reinit_err}",
            }
    else:
        await _reset_session()
        try:
            await _ensure_mcp_initialized()
        except Exception as reinit_err:
            return {
                "success": False,
                "message": f"Browser validation failed and reinit errored: {reinit_err}",
            }
    return nav_result


# Tools that accept an ``element`` + ``ref`` pair (resolved from *target*).
_REF_TOOLS: frozenset[str] = frozenset({
    "browser_click", "browser_hover", "browser_drag",
})

# Tools that need an input-specific ref (textbox / combobox / searchbox).
_INPUT_REF_TOOLS: frozenset[str] = frozenset({
    "browser_type", "browser_select_option",
})


async def _build_mcp_args(
    tool_name: str,
    target: str,
    text: str,
) -> dict:
    """Map the agent's *(target, text)* pair to MCP-tool-specific arguments.

    Returns a dict ready to pass to ``_mcp_call(tool_name, args)``.
    A special ``_fallback_js_click`` key signals the caller to use the
    JavaScript click fallback instead of the MCP tool.
    """
    args: dict = {}

    # ── Navigation ────────────────────────────────────────────────────
    if tool_name == "browser_navigate":
        url = text or target or ""
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        args["url"] = url
        return args

    # ── Click (with JS fallback) ─────────────────────────────────────
    if tool_name == "browser_click":
        element = target or text or ""
        ref = await _resolve_ref(element)
        if ref:
            args["element"] = element
            args["ref"] = ref
        else:
            args["_fallback_js_click"] = True
            args["_element"] = element
        return args

    # ── Element + ref tools (hover, drag) ────────────────────────────
    if tool_name in _REF_TOOLS:
        element = target or ""
        ref = await _resolve_ref(element)
        if not ref:
            return {"_error": f"Unable to resolve element ref for {tool_name}: {element}"}
        args["element"] = element
        args["ref"] = ref
        # Drag needs start/end
        if tool_name == "browser_drag":
            end_element = text or ""
            end_ref = await _resolve_ref(end_element)
            if not end_ref:
                return {"_error": f"Unable to resolve end-element ref for drag: {end_element}"}
            args = {
                "startElement": element,
                "startRef": ref,
                "endElement": end_element,
                "endRef": end_ref,
            }
        return args

    # ── Input-ref tools (type, select_option) ────────────────────────
    if tool_name in _INPUT_REF_TOOLS:
        element = target or ""
        ref = await _resolve_input_ref(element)
        if not ref:
            return {"_error": f"Unable to resolve input ref for {tool_name}: {element}"}
        args["element"] = element
        args["ref"] = ref
        if tool_name == "browser_type":
            args["text"] = text or ""
        elif tool_name == "browser_select_option":
            args["values"] = [text] if text else []
        return args

    # ── Keyboard ─────────────────────────────────────────────────────
    if tool_name == "browser_press_key":
        args["key"] = text or target or ""
        return args

    # ── Wait ─────────────────────────────────────────────────────────
    if tool_name == "browser_wait_for":
        args["text"] = target or text or ""
        return args

    # ── Evaluate JS ──────────────────────────────────────────────────
    if tool_name == "browser_evaluate":
        fn = text or target or ""
        ok, reason = _is_evaluate_js_safe(fn)
        if not ok:
            logger.warning("Refusing browser_evaluate: %s", reason)
            return {"_error": reason}
        if not fn.strip().startswith("("):
            fn = f"() => ({fn})"
        args["function"] = fn
        return args

    # ── Fill form ────────────────────────────────────────────────────
    if tool_name == "browser_fill_form":
        try:
            args["fields"] = json.loads(text) if text else []
        except json.JSONDecodeError:
            return {"_error": f"Invalid JSON for fill_form: {text}"}
        return args

    # ── File upload ──────────────────────────────────────────────────
    if tool_name == "browser_file_upload":
        if text:
            args["paths"] = [p.strip() for p in text.split(",") if p.strip()]
        return args

    # ── Dialog ───────────────────────────────────────────────────────
    if tool_name == "browser_handle_dialog":
        args["accept"] = (text or "accept").strip().lower() != "dismiss"
        if target:
            args["promptText"] = target
        return args

    # ── Resize ───────────────────────────────────────────────────────
    if tool_name == "browser_resize":
        try:
            parts = (text or "1280x720").lower().split("x")
            args["width"] = int(parts[0])
            args["height"] = int(parts[1])
        except (ValueError, IndexError):
            return {"_error": f"Invalid resize dimensions: {text}"}
        return args

    # ── Console / network ────────────────────────────────────────────
    if tool_name == "browser_console_messages":
        if text:
            args["level"] = text
        return args

    if tool_name == "browser_network_requests":
        args["includeStatic"] = (text or "").strip().lower() == "static"
        return args

    # ── Run code ─────────────────────────────────────────────────────
    # browser_run_code is denylisted (see ``_DISALLOWED_MCP_TOOLS``).
    # Kept here only to surface a clear error if an LLM forces the name
    # through the legacy flat-args path.  The call will still be refused
    # at ``_mcp_call``.
    if tool_name == "browser_run_code":
        return {"_error": "browser_run_code is disabled for security reasons"}

    # ── Generic pass-through (snapshot, tabs, navigate_back, close …)
    # For tools with no special parameter mapping the MCP server
    # validates its own schema — just forward text/target if present.
    return args


async def _js_click_fallback(element: str) -> dict:
    """Attempt a click via JavaScript evaluation when ref resolution fails."""
    safe_target = json.dumps((element or "").strip())
    click_function = (
        "() => {"
        f"const needle = {safe_target}.toLowerCase();"
        "const nodes = Array.from(document.querySelectorAll("
        "\"a,button,[role='button'],input,textarea,select,label,*\"));"
        "const pick = nodes.find(el => "
        "((el.innerText||el.textContent||el.value||'').trim()"
        ".toLowerCase().includes(needle)));"
        "if (!pick) return 'not_found';"
        "pick.click();"
        "return 'clicked';"
        "}"
    )
    fallback = await _mcp_call("browser_evaluate", {"function": click_function})
    if fallback.get("success") and "not_found" not in fallback.get("message", ""):
        return fallback
    return {"success": False, "message": f"Unable to click target via MCP: {element}"}


# ── Docker HTTP MCP Transport ─────────────────────────────────────────────────
# Connects to the Playwright MCP server running inside the Docker container
# via Streamable HTTP transport (port 8931 by default).

_docker_exit_stack: AsyncExitStack | None = None
_docker_mcp_session: ClientSession | None = None
_docker_mcp_init_lock: asyncio.Lock | None = None


def _get_docker_init_lock() -> asyncio.Lock:
    """Return or create the Docker MCP initialisation lock."""
    global _docker_mcp_init_lock
    if _docker_mcp_init_lock is None:
        _docker_mcp_init_lock = asyncio.Lock()
    return _docker_mcp_init_lock


async def _ensure_docker_mcp_initialized() -> ClientSession:
    """Ensure the Docker HTTP MCP session is connected and initialized.

    Connects to the Playwright MCP server inside the Docker container
    via Streamable HTTP transport (HTTP/SSE).  The entire init sequence
    (connect → initialize → list_tools) is wrapped in a 15 s timeout
    so we fail fast when the container MCP server is unreachable.
    """
    global _docker_exit_stack, _docker_mcp_session

    if _docker_mcp_session is not None:
        return _docker_mcp_session

    async with _get_docker_init_lock():
        if _docker_mcp_session is not None:
            return _docker_mcp_session

        mcp_url = config.playwright_mcp_endpoint

        if config.playwright_mcp_host == "127.0.0.1":
            logger.warning(
                "PLAYWRIGHT_MCP_HOST is '127.0.0.1' — the HTTP Host header will be "
                "'127.0.0.1:%s' which may be rejected by Playwright MCP's allowed-host "
                "check (403 Forbidden).  Consider using 'localhost' instead.",
                config.playwright_mcp_port,
            )

        logger.info("Connecting to Docker Playwright MCP at %s (timeout=%.0fs)", mcp_url, _DOCKER_MCP_INIT_TIMEOUT)

        _docker_exit_stack = AsyncExitStack()
        await _docker_exit_stack.__aenter__()

        try:
            from mcp.client.streamable_http import streamable_http_client

            read_stream, write_stream, _ = await _docker_exit_stack.enter_async_context(
                streamable_http_client(mcp_url)
            )
            session = await _docker_exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            # Wrap init + list_tools in a single timeout so we fail fast
            try:
                await asyncio.wait_for(session.initialize(), timeout=_DOCKER_MCP_INIT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Docker MCP HTTP init timed out after {_DOCKER_MCP_INIT_TIMEOUT:.0f}s. "
                    f"Is the container MCP server running on {mcp_url} ? "
                    "Hint: 'curl -I' will return 403 — that is expected; the server only accepts POST JSON-RPC."
                )

            try:
                tools_result = await asyncio.wait_for(session.list_tools(), timeout=_DOCKER_MCP_INIT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Docker MCP list_tools timed out after {_DOCKER_MCP_INIT_TIMEOUT:.0f}s. "
                    f"MCP server at {mcp_url} accepted initialize but is not responding to list_tools."
                )

            # Store discovered tools — server is the source of truth
            global _discovered_tools
            _discovered_tools = _filter_discovered_tools([
                {
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "inputSchema": getattr(t, "inputSchema", {}) or {},
                }
                for t in tools_result.tools
            ])
            tool_names = [t["name"] for t in _discovered_tools]
            logger.info(
                "Docker MCP HTTP session established — %d tools discovered: %s",
                len(tool_names),
                ", ".join(tool_names[:10])
                + ("..." if len(tool_names) > 10 else ""),
            )

            _docker_mcp_session = session
            return _docker_mcp_session
        except Exception as exc:
            # Detect HTTP 403 — this is the "stuck / Action History (0)" failure.
            exc_str = str(exc)
            is_403 = "403" in exc_str

            if is_403:
                hint = _hint_for_http_403(mcp_url)
                logger.error(
                    "Docker MCP init received HTTP 403 for %s — "
                    "this BLOCKS all agent actions (Action History 0).\n%s",
                    mcp_url, hint,
                )
                try:
                    await _docker_exit_stack.aclose()
                except Exception:
                    pass
                _docker_exit_stack = None
                _docker_mcp_session = None
                raise PermissionError(hint) from exc

            # Non-403 failure — preserve existing behaviour.
            # NOTE: 403 on 'curl -I' is expected (server only accepts POST
            # JSON-RPC); but 403 on the actual initialize POST is a blocker.
            logger.error(
                "Docker MCP init failed for %s — %s. "
                "(403 on 'curl -I %s' can be normal — server only accepts "
                "POST JSON-RPC.  But 403 during initialize POST is a "
                "hard blocker — see _hint_for_http_403.)",
                mcp_url, exc, mcp_url,
            )
            try:
                await _docker_exit_stack.aclose()
            except Exception:
                pass
            _docker_exit_stack = None
            _docker_mcp_session = None
            raise


async def _reset_docker_session() -> None:
    """Tear down the current Docker HTTP MCP session for reconnection."""
    global _docker_exit_stack, _docker_mcp_session

    if _docker_exit_stack:
        try:
            await _docker_exit_stack.aclose()
        except Exception as e:
            logger.warning("Docker MCP session cleanup error: %s", e)

    _docker_exit_stack = None
    _docker_mcp_session = None


async def _docker_mcp_call(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via the Docker HTTP session.

    Returns ``{"success": bool, "message": str}``.
    Auto-reconnects once if the session has dropped.
    """
    mcp_url = config.playwright_mcp_endpoint
    for attempt in range(2):
        try:
            session = await _ensure_docker_mcp_initialized()
            _log_mcp_call(tool_name, "calling[docker]")
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                text = _text_from_content(result.content)
                _log_mcp_call(tool_name, "tool_error[docker]", error=text)
                return {"success": False, "message": text or "MCP tool returned error"}

            text = _text_from_content(result.content)
            _log_mcp_call(tool_name, "ok[docker]")
            return {"success": True, "message": text}

        except PermissionError:
            # 403 already diagnosed by _ensure_docker_mcp_initialized — surface hint directly.
            hint = _hint_for_http_403(mcp_url)
            _log_mcp_call(tool_name, "http_403[docker]", error=hint)
            return {"success": False, "message": hint}
        except Exception as e:
            _log_mcp_call(tool_name, "exception[docker]", error=str(e))
            is_403 = "403" in str(e)
            if is_403:
                hint = _hint_for_http_403(mcp_url)
                logger.error(
                    "Docker MCP call '%s' received HTTP 403 — "
                    "agent actions will fail.\n%s",
                    tool_name, hint,
                )
                return {"success": False, "message": hint}
            if attempt == 0:
                logger.info(
                    "Docker MCP call failed — resetting HTTP session and retrying. "
                    "URL=%s  If 'curl -I' returns 403, that is expected (use POST).",
                    mcp_url,
                )
                await _reset_docker_session()
                continue
            return {"success": False, "message": f"Docker MCP call failed after retry: {e}"}

    return {"success": False, "message": "Docker MCP call failed: exhausted retries"}


async def execute_mcp_action_docker(
    action: str,
    text: str = "",
    target: str = "",
    step: int = 0,
) -> dict:
    """Execute an MCP action targeting the Docker container.

    Enforces ``_mcp_target = "docker"`` so all downstream ``_mcp_call()``
    invocations use Streamable HTTP transport to the container's MCP
    server, regardless of what the global target was set to previously.
    """
    global _mcp_target
    if _mcp_target != "docker":
        logger.warning(
            "execute_mcp_action_docker called but _mcp_target was '%s' — forcing 'docker'",
            _mcp_target,
        )
        _mcp_target = "docker"
    try:
        return await execute_mcp_action(action, text, target, step)
    except asyncio.CancelledError:
        logger.error("Docker MCP action '%s' was cancelled (CancelledError)", action)
        return {"success": False, "message": f"Docker MCP action '{action}' was cancelled — check container connectivity"}
    except Exception as exc:
        logger.error("Docker MCP action '%s' raised: %s", action, exc, exc_info=True)
        return {"success": False, "message": f"Docker MCP action '{action}' error: {exc}"}


# ── Execution Dispatcher ──────────────────────────────────────────────────────

async def execute_mcp_action(
    action: str,
    text: str = "",
    target: str = "",
    step: int = 0,
) -> dict:
    """Dispatch an MCP action directly to the Playwright MCP server.

    The *action* field is expected to be an MCP tool name
    (``browser_click``, ``browser_navigate``, etc.) or one of the
    agent-control pseudo-actions (``done``, ``error``, ``wait``).
    """
    global _current_step, _current_action
    _current_step = step
    _current_action = action

    # ── Agent-control pseudo-actions ─────────────────────────────────
    if action == "done":
        return {"success": True, "message": "Task completed"}
    if action == "error":
        return {"success": False, "message": f"Agent error: {text}"}
    if action == "wait":
        duration = 2.0
        try:
            duration = float(text)
        except (ValueError, TypeError):
            pass
        capped = min(max(duration, 0.1), 10.0)
        await asyncio.sleep(capped)
        return {"success": True, "message": f"Waited {capped:.1f}s"}

    # ── Build tool-specific arguments ────────────────────────────────
    args = await _build_mcp_args(action, target, text)

    # Argument-build errors
    if "_error" in args:
        return {"success": False, "message": args["_error"]}

    # JS click fallback path
    if args.pop("_fallback_js_click", False):
        return await _js_click_fallback(args.pop("_element", target))

    # ── Call through to MCP server ───────────────────────────────────
    result = await _mcp_call(action, args)

    # Post-call hooks
    if action in _INPUT_REF_TOOLS:
        result = await _self_heal_input(result, target, text, args.get("ref", ""))
    if action == "browser_navigate":
        result = await _validate_browser_context(result)

    return result


async def execute_mcp_action_direct(
    tool_name: str,
    tool_args: dict,
    step: int = 0,
) -> dict:
    """Direct MCP passthrough — LLM provided native tool arguments.

    Bypasses ``_build_mcp_args``, ref resolution, self-heal, and JS
    fallback.  The *tool_args* dict is forwarded verbatim to
    ``session.call_tool(tool_name, tool_args)``.

    Agent-control pseudo-actions (``done``, ``error``, ``wait``) are
    routed through the existing ``execute_mcp_action`` handler.
    """
    global _current_step, _current_action
    _current_step = step
    _current_action = tool_name

    # Pseudo-actions still go through the legacy path
    if tool_name in ("done", "error", "wait"):
        return await execute_mcp_action(
            tool_name,
            text=tool_args.get("text", ""),
            target=tool_args.get("target", ""),
            step=step,
        )

    # Direct call — no arg translation, no ref resolution
    result = await _mcp_call(tool_name, tool_args)

    # Post-call hook: validate browser context after navigation
    if tool_name == "browser_navigate":
        result = await _validate_browser_context(result)

    return result


async def execute_mcp_action_direct_docker(
    tool_name: str,
    tool_args: dict,
    step: int = 0,
) -> dict:
    """Direct MCP passthrough targeting the Docker container.

    Ensures ``_mcp_target`` is ``"docker"`` then delegates to
    ``execute_mcp_action_direct``.
    """
    global _mcp_target
    if _mcp_target != "docker":
        logger.warning(
            "execute_mcp_action_direct_docker called but _mcp_target was '%s' — forcing 'docker'",
            _mcp_target,
        )
        _mcp_target = "docker"
    try:
        return await execute_mcp_action_direct(tool_name, tool_args, step)
    except asyncio.CancelledError:
        logger.error("Docker MCP direct action '%s' was cancelled", tool_name)
        return {"success": False, "message": f"Docker MCP direct action '{tool_name}' was cancelled"}
    except Exception as exc:
        logger.error("Docker MCP direct action '%s' raised: %s", tool_name, exc, exc_info=True)
        return {"success": False, "message": f"Docker MCP direct action '{tool_name}' error: {exc}"}


async def check_mcp_health() -> bool:
    """Check if the Playwright MCP server is responsive.

    Routes to STDIO or HTTP transport depending on ``_mcp_target``.
    Uses ``list_tools`` — if the server returns at least one tool,
    it is considered healthy.
    """
    try:
        if _mcp_target == "docker":
            session = await _ensure_docker_mcp_initialized()
        else:
            session = await _ensure_mcp_initialized()
        result = await session.list_tools()
        return len(result.tools) > 0
    except Exception:
        return False


async def close_mcp_session() -> None:
    """Terminate all active MCP sessions (STDIO and HTTP).

    Closes both the STDIO session (local) and the HTTP session (Docker)
    if they are active.
    """
    global _exit_stack, _mcp_session

    # Close STDIO session
    if _exit_stack:
        try:
            await _exit_stack.aclose()
            logger.info("MCP STDIO session closed")
        except Exception as e:
            logger.warning("MCP STDIO session close error: %s", e)
        finally:
            _exit_stack = None
            _mcp_session = None
    else:
        logger.debug("No active MCP STDIO session to close")

    # Close Docker HTTP session
    await _reset_docker_session()
