"""Action alias resolution and per-engine capability validation.

This module eliminates "Unknown action" errors by:
1. Normalizing action aliases (e.g. "press" → "key", "navigate" → "open_url")
2. Validating that the resolved action is supported by the target engine
3. Providing per-engine capability matrices for routing decisions

Usage:
    from backend.tools.action_aliases import resolve_action, validate_engine_support

    resolved = resolve_action("press")           # → "key"
    ok, msg = validate_engine_support("fill", "playwright_mcp")  # → (True, "")
"""

from __future__ import annotations

from backend.engine_capabilities import EngineCapabilities

# ── Singleton capability registry (loaded once from engine_capabilities.json) ──
_capability_registry = EngineCapabilities()

# ── Alias map: variant name → canonical ActionType value ──────────────────────

ACTION_ALIASES: dict[str, str] = {
    # Mouse
    "left_click": "click",
    "click_element": "click",
    "dblclick": "double_click",
    "rightclick": "right_click",
    "context_click": "right_click",
    "mouseover": "hover",
    "mouse_move": "hover",
    "mousemove": "hover",
    "drag_and_drop": "drag",
    "drag_drop": "drag",
    # Keyboard
    "press": "key",
    "press_key": "key",
    "keypress": "key",
    "send_keys": "key",
    "type_text": "type",
    "input_text": "type",
    "input": "type",
    "enter_text": "type",
    "write": "type",
    "fill_form": "fill",
    "set_value": "fill",
    "clear": "clear_input",
    "clear_field": "clear_input",
    "select": "select_option",
    "choose_option": "select_option",
    "combo": "hotkey",
    "shortcut": "hotkey",
    "key_combo": "hotkey",
    "clipboard_paste": "paste",
    "clipboard_copy": "copy",
    # Navigation
    "navigate": "open_url",
    "goto": "open_url",
    "go_to": "open_url",
    "go": "open_url",
    "open": "open_url",
    "visit": "open_url",
    "browse": "open_url",
    "refresh": "reload",
    "back": "go_back",
    "forward": "go_forward",
    # Tabs
    "open_tab": "new_tab",
    "create_tab": "new_tab",
    "tab_close": "close_tab",
    "tab_switch": "switch_tab",
    "change_tab": "switch_tab",
    # Scrolling
    "scroll_up": "scroll",
    "scroll_down": "scroll",
    "scroll_element": "scroll_into_view",
    "scroll_into_view": "scroll_into_view",
    # DOM
    "extract_text": "get_text",
    "read_text": "get_text",
    "get_content": "get_text",
    "locate": "find_element",
    "search_element": "find_element",
    "find": "find_element",
    # JS
    "eval_js": "evaluate_js",
    "execute_js": "evaluate_js",
    "run_js": "evaluate_js",
    "javascript": "evaluate_js",
    # Window
    "activate_window": "focus_window",
    "switch_window": "focus_window",
    "launch": "open_app",
    "launch_app": "open_app",
    "start_app": "open_app",
    "kill_window": "close_window",
    "window_close": "close_window",
    "focus_and_click": "focus_click",
    # Vision
    "capture": "screenshot",
    "capture_screen": "screenshot",
    "capture_region": "screenshot_region",
    "take_screenshot": "screenshot",
    "full_screenshot": "screenshot_full",
    "viewport_screenshot": "screenshot_viewport",
    "element_screenshot": "screenshot_element",
    # Control
    "sleep": "wait",
    "pause": "wait",
    "delay": "wait",
    "wait_for_element": "wait_for",
    "wait_for_selector": "wait_for",
    "await_element": "wait_for",
    # Terminal
    "complete": "done",
    "finish": "done",
    "finished": "done",
    "success": "done",
    "fail": "error",
    "abort": "error",
    # Shell / terminal
    "run_command": "run_command",
    "shell": "run_command",
    "exec": "run_command",
    "execute": "run_command",
    "terminal": "open_terminal",
    # DOM advanced
    "inner_html": "get_html",
    "outer_html": "get_html",
    "get_attr": "get_attribute",
    "bounding_box": "get_bounding_box",
    "bbox": "get_bounding_box",
    "visible_elements": "get_visible_elements",
    "eval_on": "evaluate_on_selector",
    "js_on_selector": "evaluate_on_selector",
    # File
    "set_input_files": "upload_file",
    "file_upload": "upload_file",
    "export_pdf": "export_page_pdf",
    # MCP semantic
    "accessibility_tree": "get_accessibility_tree",
    "snapshot": "get_snapshot",
    "a11y_tree": "get_accessibility_tree",
    # Window management
    "minimize": "window_minimize",
    "maximize": "window_maximize",
    "move_window": "window_move",
    "resize_window": "window_resize",
    "find_window": "search_window",
    "activate_window": "window_activate",
    "focus_cursor": "focus_mouse",
    # Keys
    "key_down": "keydown",
    "key_up": "keyup",
    "slow_type": "type_slow",
    # Browser network/session/meta
    "file_chooser": "handle_file_chooser",
    "session_state": "storage_state",
    "network_intercept": "intercept_request",
    "network_monitor": "monitor_requests",
    "response_body": "get_response_body",
    "assert_present": "assert_element_present",
    "check_text": "verify_text",
    "retry": "retry_last_action",
    "fallback": "fallback_strategy",
    "set_viewport": "set_viewport",
    "zoom": "zoom",
    "block_resource": "block_resource",
}


# ── Per-engine capability matrix (derived from engine_capabilities.json) ──────
# Source of truth is now backend/engine_capabilities.json.
# These frozensets are populated at import time for backward compatibility.

_MCP_ACTIONS: frozenset[str] = _capability_registry.get_engine_actions("playwright_mcp")
_ACCESSIBILITY_ACTIONS: frozenset[str] = _capability_registry.get_engine_actions("omni_accessibility")
_COMPUTER_USE_ACTIONS: frozenset[str] = _capability_registry.get_engine_actions("computer_use")

ENGINE_CAPABILITIES: dict[str, frozenset[str]] = {
    "playwright_mcp": _MCP_ACTIONS,
    "omni_accessibility": _ACCESSIBILITY_ACTIONS,
    "computer_use": _COMPUTER_USE_ACTIONS,
}


def resolve_action(action: str) -> str:
    """Resolve an action string to its canonical ActionType value.

    Returns the canonical action name, or the original string if no alias match.
    Handles case-insensitive matching.
    """
    normalized = action.strip().lower()
    return ACTION_ALIASES.get(normalized, normalized)


def validate_engine_support(action: str, engine: str) -> tuple[bool, str]:
    """Check if an action is supported by the specified engine.

    Delegates to the JSON-driven :class:`EngineCapabilities` registry.
    Falls back to ``_UNSUPPORTED_HINTS`` for human-friendly guidance.

    Args:
        action: Canonical action name (already resolved via resolve_action).
        engine: Engine identifier.

    Returns:
        (is_supported, error_message)
    """
    ok, detail = _capability_registry.validate_action_detailed(engine, action)
    if ok:
        return True, ""

    # Overlay human-friendly hints when available
    hints = _UNSUPPORTED_HINTS.get((action, engine))
    if hints:
        return False, f"{action} not supported in {engine} mode — {hints}"

    return False, detail or f"{action} not available in {engine} mode"


# ── Helpful hints when an action isn't available ──────────────────────────────

_UNSUPPORTED_HINTS: dict[tuple[str, str], str] = {
    # playwright_mcp engine hints
    ("drag", "playwright_mcp"): "use computer_use engine for drag operations",
    ("right_click", "playwright_mcp"): "use computer_use engine for right-click",
    ("middle_click", "playwright_mcp"): "use computer_use engine for middle-click",
    ("screenshot_region", "playwright_mcp"): "use computer_use engine for region screenshots",
    ("run_command", "playwright_mcp"): "use desktop engine for shell commands",
    ("open_terminal", "playwright_mcp"): "use desktop engine for terminal access",
    # omni_accessibility engine hints
    ("evaluate_js", "omni_accessibility"): "no browser JS context — use AT-SPI tree inspection",
    ("get_html", "omni_accessibility"): "no DOM access — use get_accessibility_tree",
    ("query_selector", "omni_accessibility"): "no DOM — use find_element with accessible name/role",
    ("set_cookies", "omni_accessibility"): "cookies require browser engine",
    ("new_context", "omni_accessibility"): "browser context requires browser engine",
    ("upload_file", "omni_accessibility"): "file upload requires browser engine",
}
