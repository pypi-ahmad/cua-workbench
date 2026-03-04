"""Playwright MCP client — STDIO transport (local) and HTTP transport (Docker).

Playwright MCP: https://github.com/microsoft/playwright-mcp

Communicates with the Playwright MCP server over:
  • STDIO (local): spawns npx @playwright/mcp@latest as a child process
  • HTTP  (Docker): connects to the MCP server inside the Docker container

Uses the ``mcp`` Python SDK's ``stdio_client`` / ``streamablehttp_client``
to manage the connection.

The MCP server command defaults to ``npx -y @playwright/mcp@latest`` and
can be overridden via ``PLAYWRIGHT_MCP_COMMAND`` / ``PLAYWRIGHT_MCP_ARGS``
environment variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.config import config
from backend.models import ActionType

logger = logging.getLogger(__name__)

# ── Module-level state ────────────────────────────────────────────────────────

_exit_stack: AsyncExitStack | None = None
_mcp_session: ClientSession | None = None
_mcp_init_lock: asyncio.Lock | None = None

# Per-call context for structured logging (set by execute_mcp_action)
_current_step: int = 0
_current_action: str = "unknown"

# Target-aware STDIO transport: "local" runs npx on host, "docker" runs
# npx inside the container via `docker exec -i <container>` so the browser
# appears in VNC and never opens on the host machine.
_mcp_target: str = "local"
_mcp_server_key: Optional[str] = None


def set_mcp_target(target: str) -> None:
    """Set the execution target for subsequent MCP sessions.

    Must be called **once per agent run** before any MCP action.
    When *target* is ``"docker"``, the STDIO session is tunnelled
    through ``docker exec -i <container>`` so Playwright runs inside
    the container (headed, visible in VNC).
    """
    global _mcp_target
    _mcp_target = "docker" if target == "docker" else "local"
    logger.info("MCP target set to '%s'", _mcp_target)


def _build_server_params() -> StdioServerParameters:
    """Build STDIO server params appropriate for the current target."""
    local_args = shlex.split(config.playwright_mcp_args)

    if _mcp_target != "docker":
        return StdioServerParameters(
            command=config.playwright_mcp_command,
            args=local_args,
        )

    # Docker: run MCP *inside* the container via docker exec STDIO.
    # No --port (STDIO mode), no --headless (headed → visible in VNC),
    # --no-sandbox because Chrome in Docker usually runs as root.
    container = config.container_name
    docker_bin = os.environ.get("DOCKER_BIN", "docker")
    return StdioServerParameters(
        command=docker_bin,
        args=[
            "exec", "-i", container,
            config.playwright_mcp_command,
        ] + local_args + ["--no-sandbox"],
        env=os.environ.copy(),
    )


# ── Logging helpers ───────────────────────────────────────────────────────────

def _log_mcp_call(
    method: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    """Emit a structured log entry for every MCP tool call."""
    connected = _mcp_session is not None
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

    When ``_mcp_target`` is ``"docker"``, the child process is
    ``docker exec -i <container> npx …`` so the MCP server (and its
    browser) run inside the container and are visible in VNC.
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

            # Verify server has tools
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            logger.info(
                "MCP STDIO session established — %d tools available: %s",
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

async def _mcp_call(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via the STDIO session.

    Returns ``{"success": bool, "message": str}``.
    Auto-reconnects once if the session has dropped.
    """
    for attempt in range(2):
        try:
            session = await _ensure_mcp_initialized()
            _log_mcp_call(tool_name, "calling")
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                text = _text_from_content(result.content)
                _log_mcp_call(tool_name, "tool_error", error=text)
                return {"success": False, "message": text or "MCP tool returned error"}

            text = _text_from_content(result.content)
            _log_mcp_call(tool_name, "ok")
            return {"success": True, "message": text}

        except Exception as e:
            _log_mcp_call(tool_name, "exception", error=str(e))
            if attempt == 0:
                logger.info("MCP call failed — resetting STDIO session and retrying")
                await _reset_session()
                continue
            return {"success": False, "message": f"MCP call failed after retry: {e}"}

    # Should not reach here, but safety net
    return {"success": False, "message": "MCP call failed: exhausted retries"}


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


# ── Screenshot via MCP ─────────────────────────────────────────────────────────

async def capture_mcp_screenshot() -> str:
    """Capture a PNG screenshot from the MCP-controlled browser.

    Calls the MCP ``browser_take_screenshot`` tool and extracts the base64
    image from the response content array.

    Returns:
        Base64-encoded PNG string.
    """
    session = await _ensure_mcp_initialized()
    result = await session.call_tool("browser_take_screenshot", {})

    for item in result.content:
        # ImageContent has .data (base64) and .mimeType
        if hasattr(item, "data") and hasattr(item, "mimeType"):
            if item.data:
                return item.data

    raise RuntimeError(
        "MCP browser_take_screenshot did not return image data; "
        f"content types: {[type(c).__name__ for c in result.content]}"
    )


# ── Core MCP Actions ──────────────────────────────────────────────────────────

async def mcp_navigate(url: str) -> dict:
    """Navigate the MCP-controlled browser to *url*."""
    return await _mcp_call("browser_navigate", {"url": url})


async def mcp_click(element: str) -> dict:
    """Click an element by accessibility ref or text content fallback."""
    ref_like = re.fullmatch(r"[A-Za-z]\d+", (element or "").strip())
    if ref_like:
        return await _mcp_call("browser_click", {"element": element, "ref": element.strip()})

    safe_target = json.dumps((element or "").strip())
    click_function = (
        "() => {"
        f"const needle = {safe_target}.toLowerCase();"
        "const nodes = Array.from(document.querySelectorAll(\"a,button,[role='button'],input,textarea,select,label,*\"));"
        "const pick = nodes.find(el => ((el.innerText||el.textContent||el.value||'').trim().toLowerCase().includes(needle)));"
        "if (!pick) return 'not_found';"
        "pick.click();"
        "return 'clicked';"
        "}"
    )
    fallback = await _mcp_call(
        "browser_evaluate",
        {"function": click_function},
    )
    if fallback.get("success") and "not_found" not in fallback.get("message", ""):
        return fallback
    return {"success": False, "message": f"Unable to click target via MCP: {element}"}


async def mcp_double_click(element: str) -> dict:
    """Double-click an element resolved via accessibility snapshot."""
    ref = await _resolve_ref(element)
    if not ref:
        return {"success": False, "message": f"Unable to resolve element ref for double_click target: {element}"}
    return await _mcp_call("browser_click", {"element": element, "ref": ref, "doubleClick": True})


async def mcp_hover(element: str) -> dict:
    """Hover over an element resolved via accessibility snapshot."""
    ref = await _resolve_ref(element)
    if not ref:
        return {"success": False, "message": f"Unable to resolve element ref for hover target: {element}"}
    return await _mcp_call("browser_hover", {"element": element, "ref": ref})


async def mcp_type(element: str, text: str) -> dict:
    """Type *text* into the element resolved via accessibility snapshot.

    Uses ``_resolve_input_ref`` to ensure the resolved ref actually
    points to a fillable element (textbox, combobox, searchbox, etc.)
    instead of a non-input element the model may have mis-targeted.

    If the call still fails with "not an <input>", a reactive self-heal
    takes a fresh snapshot and retries with the first real input ref.
    """
    ref = await _resolve_input_ref(element)
    if not ref:
        return {"success": False, "message": f"Unable to resolve element ref for type target: {element}"}
    result = await _mcp_call("browser_type", {"element": element, "ref": ref, "text": text})
    result = await _self_heal_input(result, element, text, ref)
    return result


async def mcp_fill(element: str, value: str) -> dict:
    """Fill *value* into the element (clears first).

    Uses ``_resolve_input_ref`` to ensure the resolved ref actually
    points to a fillable element (textbox, combobox, searchbox, etc.)
    instead of a non-input element the model may have mis-targeted.

    If the call still fails with "not an <input>", a reactive self-heal
    takes a fresh snapshot and retries with the first real input ref.
    """
    ref = await _resolve_input_ref(element)
    if not ref:
        return {"success": False, "message": f"Unable to resolve element ref for fill target: {element}"}
    result = await _mcp_call("browser_type", {"element": element, "ref": ref, "text": value})
    result = await _self_heal_input(result, element, value, ref)
    return result


async def mcp_select_option(element: str, value: str) -> dict:
    """Select *value* from a <select> element.

    Uses ``_resolve_input_ref`` to find the actual ``<select>`` / combobox.
    """
    ref = await _resolve_input_ref(element)
    if not ref:
        return {"success": False, "message": f"Unable to resolve element ref for select_option target: {element}"}
    return await _mcp_call("browser_select_option", {"element": element, "ref": ref, "values": [value]})


async def mcp_press_key(key: str) -> dict:
    """Press a keyboard key via the MCP server."""
    return await _mcp_call("browser_press_key", {"key": key})


async def mcp_scroll(direction: str) -> dict:
    """Scroll the page up or down by 300px."""
    delta_y = -300 if direction == "up" else 300
    return await _mcp_call("browser_evaluate", {
        "function": f"() => window.scrollBy(0, {delta_y})"
    })


async def mcp_scroll_to(element: str) -> dict:
    """Scroll the matching element into view."""
    ref = await _resolve_ref(element)
    if ref:
        return await _mcp_call("browser_evaluate", {
            "function": "(el) => el?.scrollIntoView({behavior:'smooth', block:'center'})",
            "element": element,
            "ref": ref,
        })
    return await _mcp_call("browser_evaluate", {
        "function": "() => window.scrollBy(0, 500)",
    })


async def mcp_evaluate(expression: str) -> dict:
    """Evaluate a JavaScript expression in the page context."""
    return await _mcp_call("browser_evaluate", {"function": f"() => ({expression})"})


async def mcp_wait_for(selector: str) -> dict:
    """Wait for text or element matching *selector* to appear."""
    return await _mcp_call("browser_wait_for", {"text": selector})


async def mcp_reload() -> dict:
    """Reload the current page."""
    return await _mcp_call("browser_reload", {})


async def mcp_go_back() -> dict:
    """Navigate back one page."""
    return await _mcp_call("browser_go_back", {})


async def mcp_go_forward() -> dict:
    """Navigate forward one page."""
    return await _mcp_call("browser_go_forward", {})


async def mcp_new_tab(url: str = "") -> dict:
    """Open a new browser tab, optionally navigating to *url*."""
    params = {"url": url} if url else {}
    return await _mcp_call("browser_new_tab", params)


async def mcp_close_tab() -> dict:
    """Close the currently active browser tab."""
    return await _mcp_call("browser_close_tab", {})


async def mcp_switch_tab(identifier: str) -> dict:
    """Switch to a tab by index or identifier."""
    try:
        idx = int(identifier)
        return await _mcp_call("browser_tab_list", {"switchTo": idx})
    except ValueError:
        return await _mcp_call("browser_tab_list", {"switchTo": identifier})


async def mcp_get_accessibility_tree() -> dict:
    """Retrieve the full accessibility tree snapshot."""
    return await _mcp_call("browser_snapshot", {})


async def mcp_get_current_url() -> dict:
    """Return the current page URL."""
    return await mcp_evaluate("window.location.href")


async def mcp_get_page_title() -> dict:
    """Return the current page title."""
    return await mcp_evaluate("document.title")


async def mcp_wait(duration: float) -> dict:
    """Sleep for *duration* seconds (capped at 10s)."""
    capped = min(max(duration, 0.1), 10.0)
    await asyncio.sleep(capped)
    return {"success": True, "message": f"Waited {capped:.1f}s"}


# ── Action Handlers ───────────────────────────────────────────────────────────

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

    # Browser context not detected — reset and reinitialize
    logger.warning("Browser context not detected after navigation — resetting STDIO session")
    await _reset_session()
    try:
        await _ensure_mcp_initialized()
    except Exception as reinit_err:
        return {
            "success": False,
            "message": f"Browser validation failed and reinit errored: {reinit_err}",
        }
    return nav_result


async def _h_open_url(text: str, target: str) -> dict:
    """Handler: navigate to the URL in *text* and validate browser context."""
    if not text:
        return {"success": False, "message": "Missing URL in text field"}
    url = text if text.startswith(("http://", "https://")) else f"https://{text}"
    result = await mcp_navigate(url)
    if not result.get("success"):
        return result
    return await _validate_browser_context(result)


async def _h_click(text: str, target: str) -> dict:
    """Handler: click the element identified by *target*."""
    if not target:
        return {"success": False, "message": "Target required for click"}
    return await mcp_click(target)


async def _h_double_click(text: str, target: str) -> dict:
    """Handler: double-click the element identified by *target*."""
    if not target:
        return {"success": False, "message": "Target required for double_click"}
    return await mcp_double_click(target)


async def _h_hover(text: str, target: str) -> dict:
    """Handler: hover over the element identified by *target*."""
    if not target:
        return {"success": False, "message": "Target required for hover"}
    return await mcp_hover(target)


async def _h_type(text: str, target: str) -> dict:
    """Handler: type *text* into the targeted element."""
    if not text:
        return {"success": False, "message": "Text required for type"}
    if not target:
        return {"success": False, "message": "Target required for type in MCP mode"}
    return await mcp_type(target, text)


async def _h_fill(text: str, target: str) -> dict:
    """Handler: fill *text* into the targeted element."""
    if not target:
        return {"success": False, "message": "Target required for fill"}
    return await mcp_fill(target, text)


async def _h_key(text: str, target: str) -> dict:
    """Handler: press the keyboard key specified in *text*."""
    return await mcp_press_key(text)


async def _h_clear_input(text: str, target: str) -> dict:
    """Handler: clear the input field identified by *target*."""
    if not target:
        return {"success": False, "message": "Target required for clear_input"}
    return await mcp_fill(target, "")


async def _h_select_option(text: str, target: str) -> dict:
    """Handler: select *text* from the dropdown identified by *target*."""
    if not target:
        return {"success": False, "message": "Target required for select_option"}
    return await mcp_select_option(target, text)


async def _h_paste(text: str, target: str) -> dict:
    """Handler: paste clipboard contents via Ctrl+V."""
    return await mcp_press_key("Control+v")


async def _h_copy(text: str, target: str) -> dict:
    """Handler: copy selection to clipboard via Ctrl+C."""
    return await mcp_press_key("Control+c")


async def _h_scroll(text: str, target: str) -> dict:
    """Handler: scroll page in the direction specified by *text*."""
    direction = text.lower() if text else "down"
    return await mcp_scroll(direction)


async def _h_scroll_to(text: str, target: str) -> dict:
    """Handler: scroll the element identified by *target* into view."""
    if not target:
        return {"success": False, "message": "Target required for scroll_to"}
    return await mcp_scroll_to(target)


async def _h_get_text(text: str, target: str) -> dict:
    """Handler: extract text content from the *target* selector."""
    if not target:
        return {"success": False, "message": "Target required for get_text"}
    return await mcp_evaluate(f"document.querySelector('{target}')?.textContent?.trim() ?? ''")


async def _h_get_html(text: str, target: str) -> dict:
    """Handler: retrieve outer HTML of the *target* selector."""
    if not target:
        return {"success": False, "message": "Target required for get_html"}
    return await mcp_evaluate(f"document.querySelector('{target}')?.outerHTML ?? ''")


async def _h_get_attribute(text: str, target: str) -> dict:
    """Handler: get DOM attribute *text* from the *target* selector."""
    attr = text or "id"
    if not target:
        return {"success": False, "message": "Target required for get_attribute"}
    return await mcp_evaluate(f"document.querySelector('{target}')?.getAttribute('{attr}') ?? ''")


async def _h_evaluate_js(text: str, target: str) -> dict:
    """Handler: evaluate the JavaScript in *text* on the page."""
    if not text:
        return {"success": False, "message": "JS code required"}
    return await mcp_evaluate(text)


async def _h_evaluate_on_selector(text: str, target: str) -> dict:
    """Handler: run JS in *text* scoped to the DOM node at *target*."""
    if not target or not text:
        return {"success": False, "message": "Target and JS required"}
    script = f"(function(el) {{ {text} }})(document.querySelector('{target}'))"
    return await mcp_evaluate(script)


async def _h_wait(text: str, target: str) -> dict:
    """Handler: pause execution for the duration specified in *text*."""
    duration = 2.0
    try:
        duration = float(text)
    except ValueError:
        pass
    return await mcp_wait(duration)


async def _h_wait_for(text: str, target: str) -> dict:
    """Handler: wait for the element matching *target* to appear."""
    if not target:
        return {"success": False, "message": "Target required for wait_for"}
    return await mcp_wait_for(target)


async def _h_new_tab(text: str, target: str) -> dict:
    """Handler: open a new tab, optionally navigating to *text*."""
    return await mcp_new_tab(text)


async def _h_switch_tab(text: str, target: str) -> dict:
    """Handler: switch to the tab identified by *target* or *text*."""
    return await mcp_switch_tab(target or text)


async def _h_done(text: str, target: str) -> dict:
    """Handler: signal task completion."""
    return {"success": True, "message": "Task completed"}


async def _h_error(text: str, target: str) -> dict:
    """Handler: report an agent error with *text* as the reason."""
    return {"success": False, "message": f"Agent error: {text}"}


async def _h_get_all_text(text: str, target: str) -> dict:
    """Handler: extract all visible text from the page body."""
    return await mcp_evaluate("document.body.innerText")


async def _h_get_links(text: str, target: str) -> dict:
    """Handler: extract all anchor links from the page."""
    return await mcp_evaluate("""Array.from(document.querySelectorAll('a')).map(a => ({text: a.innerText, href: a.href}))""")


async def _h_find_element(text: str, target: str) -> dict:
    """Handler: find an element by taking a snapshot and searching the a11y tree."""
    query = target or text
    if not query:
        return {"success": False, "message": "Target or text required for find_element"}
    snapshot = await _mcp_call("browser_snapshot", {})
    if not snapshot.get("success"):
        return snapshot
    ref = _extract_ref_from_snapshot(snapshot.get("message", ""), query)
    if ref:
        return {"success": True, "message": f"Found element ref={ref} matching '{query}'"}
    return {"success": False, "message": f"No element found matching '{query}'"}


def _create_stub(tool_name: str) -> Callable[[str, str], Awaitable[dict]]:
    """Create a placeholder handler for an unimplemented MCP tool."""
    async def _stub_handler(text: str, target: str) -> dict:
        """Stub: return a not-implemented error."""
        return {"success": False, "message": f"MCP tool '{tool_name}' not yet implemented in client/server"}
    return _stub_handler


# ── Dispatch Table ────────────────────────────────────────────────────────────

MCP_TOOL_HANDLERS: Dict[str, Callable[[str, str], Awaitable[dict]]] = {
    ActionType.OPEN_URL.value: _h_open_url,
    ActionType.RELOAD.value: lambda t, g: mcp_reload(),
    ActionType.GO_BACK.value: lambda t, g: mcp_go_back(),
    ActionType.GO_FORWARD.value: lambda t, g: mcp_go_forward(),
    ActionType.NEW_TAB.value: _h_new_tab,
    ActionType.CLOSE_TAB.value: lambda t, g: mcp_close_tab(),
    ActionType.SWITCH_TAB.value: _h_switch_tab,
    ActionType.CLICK.value: _h_click,
    ActionType.DOUBLE_CLICK.value: _h_double_click,
    ActionType.HOVER.value: _h_hover,
    ActionType.TYPE.value: _h_type,
    ActionType.FILL.value: _h_fill,
    ActionType.KEY.value: _h_key,
    ActionType.HOTKEY.value: _h_key,  # reuse key handler
    ActionType.CLEAR_INPUT.value: _h_clear_input,
    ActionType.SELECT_OPTION.value: _h_select_option,
    ActionType.PASTE.value: _h_paste,
    ActionType.COPY.value: _h_copy,
    ActionType.SCROLL.value: _h_scroll,
    ActionType.SCROLL_TO.value: _h_scroll_to,
    ActionType.SCROLL_INTO_VIEW.value: _h_scroll_to,  # alias
    ActionType.GET_TEXT.value: _h_get_text,
    ActionType.GET_HTML.value: _h_get_html,
    ActionType.GET_ATTRIBUTE.value: _h_get_attribute,
    ActionType.EVALUATE_JS.value: _h_evaluate_js,
    ActionType.EVALUATE_ON_SELECTOR.value: _h_evaluate_on_selector,
    ActionType.WAIT.value: _h_wait,
    ActionType.WAIT_FOR.value: _h_wait_for,
    ActionType.DONE.value: _h_done,
    ActionType.ERROR.value: _h_error,
    ActionType.GET_ACCESSIBILITY_TREE.value: lambda t, g: mcp_get_accessibility_tree(),
    ActionType.GET_SNAPSHOT.value: lambda t, g: mcp_get_accessibility_tree(),
    ActionType.GET_CURRENT_URL.value: lambda t, g: mcp_get_current_url(),
    ActionType.GET_PAGE_TITLE.value: lambda t, g: mcp_get_page_title(),
    ActionType.GET_ALL_TEXT.value: _h_get_all_text,
    ActionType.GET_LINKS.value: _h_get_links,
    ActionType.FIND_ELEMENT.value: _h_find_element,
}

# Ensure complete coverage of ActionType (no missing mappings)
for member in ActionType:
    if member.value not in MCP_TOOL_HANDLERS:
        MCP_TOOL_HANDLERS[member.value] = _create_stub(member.value)


# ── Execution Dispatcher ──────────────────────────────────────────────────────

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
    via Streamable HTTP transport (HTTP/SSE).
    """
    global _docker_exit_stack, _docker_mcp_session

    if _docker_mcp_session is not None:
        return _docker_mcp_session

    async with _get_docker_init_lock():
        if _docker_mcp_session is not None:
            return _docker_mcp_session

        mcp_url = f"http://{config.playwright_mcp_host}:{config.playwright_mcp_port}{config.playwright_mcp_path}"
        logger.info("Connecting to Docker Playwright MCP at %s", mcp_url)

        _docker_exit_stack = AsyncExitStack()
        await _docker_exit_stack.__aenter__()

        try:
            from mcp.client.streamable_http import streamablehttp_client

            read_stream, write_stream, _ = await _docker_exit_stack.enter_async_context(
                streamablehttp_client(mcp_url)
            )
            session = await _docker_exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            logger.info(
                "Docker MCP HTTP session established — %d tools available: %s",
                len(tool_names),
                ", ".join(tool_names[:10])
                + ("..." if len(tool_names) > 10 else ""),
            )

            _docker_mcp_session = session
            return _docker_mcp_session
        except Exception:
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

        except Exception as e:
            _log_mcp_call(tool_name, "exception[docker]", error=str(e))
            if attempt == 0:
                logger.info("Docker MCP call failed — resetting HTTP session and retrying")
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

    Since ``set_mcp_target("docker")`` is called at agent-run start,
    the unified STDIO session already tunnels through
    ``docker exec -i <container>``.  This function simply delegates to
    ``execute_mcp_action`` with an extra safety net for
    ``CancelledError`` / unexpected exceptions.
    """
    try:
        return await execute_mcp_action(action, text, target, step)
    except asyncio.CancelledError:
        logger.error("Docker MCP action '%s' was cancelled (CancelledError)", action)
        return {"success": False, "message": f"Docker MCP action '{action}' was cancelled — check container connectivity"}
    except Exception as exc:
        logger.error("Docker MCP action '%s' raised: %s", action, exc, exc_info=True)
        return {"success": False, "message": f"Docker MCP action '{action}' error: {exc}"}


# ── Execution Dispatcher (Local STDIO) ────────────────────────────────────────

async def execute_mcp_action(
    action: str,
    text: str = "",
    target: str = "",
    step: int = 0,
) -> dict:
    """Dispatcher for MCP actions using the handler table.

    Sets module-level ``_current_step`` / ``_current_action`` so all
    downstream ``_mcp_call`` invocations emit structured logs with the
    correct context.
    """
    global _current_step, _current_action
    _current_step = step
    _current_action = action

    handler = MCP_TOOL_HANDLERS.get(action)
    if handler:
        return await handler(text, target)

    return {"success": False, "message": f"Unsupported action '{action}' in playwright_mcp engine"}


async def check_mcp_health() -> bool:
    """Check if the Playwright MCP server is responsive.

    Uses ``list_tools`` — if the server returns at least one tool,
    it is considered healthy.
    """
    try:
        session = await _ensure_mcp_initialized()
        result = await session.list_tools()
        return len(result.tools) > 0
    except Exception:
        return False


async def close_mcp_session() -> None:
    """Terminate the active STDIO MCP session and kill the child process.

    The ``AsyncExitStack`` will close both the ``ClientSession`` and the
    ``stdio_client`` context, which terminates the child process.
    """
    global _exit_stack, _mcp_session

    if not _exit_stack:
        logger.debug("No active MCP STDIO session to close")
        return

    try:
        await _exit_stack.aclose()
        logger.info("MCP STDIO session closed")
    except Exception as e:
        logger.warning("MCP STDIO session close error: %s", e)
    finally:
        _exit_stack = None
        _mcp_session = None
