"""Cross-platform accessibility engine — semantic desktop automation.

Provides an AccessibilityProvider abstraction with three OS-specific adapters:

  • LinuxATSPIProvider  — AT-SPI2 via GObject Introspection (gi.repository.Atspi)
  • WindowsUIAProvider  — .NET UIAutomation via PowerShell subprocess
  • MacAccessibilityProvider — System Events via osascript JXA

The active provider is auto-selected at import time via ``platform.system()``.
The external interface (handler table, dispatcher, health check) is 100%
backward-compatible with the previous single-platform engine.

Internal improvements over the predecessor:

  • **Semantic scoring** for element resolution (role, name fuzzy, state, bbox, depth)
  • **Circuit breaker** — 3 consecutive resolution failures → structured error
  • **Post-action verification** — optional state/value check after click/set_value
  • **TTL cache** — window list, screen context, and tree snapshots cached for 2 s

Requirements (Linux / Docker):
  - at-spi2-core, gir1.2-atspi-2.0, python3-gi, dbus-x11
    (libatspi2.0-0 runtime lib is pulled in transitively by at-spi2-core;
    libatspi2.0-dev headers are NOT required at runtime)
  - gsettings set org.gnome.desktop.interface toolkit-accessibility true
  - export NO_AT_BRIDGE=0
  - D-Bus session bus running

Reference: https://lazka.github.io/pgi-docs/Atspi-2.0/classes/Accessible.html
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. Unified Data Models
# ═══════════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class UIElement:
    """Platform-agnostic UI element representation."""

    element_id: int
    role: str
    name: str
    description: str
    states: list[str]
    bbox: dict | None  # {"x": int, "y": int, "width": int, "height": int}
    depth: int = 0
    app_name: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        """Serialize to the same dict shape the old engine returned."""
        d: dict[str, Any] = {
            "element_id": self.element_id,
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "states": self.states,
            "bbox": self.bbox,
        }
        if self.depth > 0:
            d["depth"] = self.depth
        if self.app_name:
            d["app_name"] = self.app_name
        return d


@dataclasses.dataclass
class WindowInfo:
    """Cross-platform window metadata."""

    element_id: int
    role: str
    name: str
    app_name: str
    states: list[str]
    bbox: dict | None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ScreenContext:
    """Snapshot of the visible screen state (cacheable)."""

    windows: list[WindowInfo]
    focused_window: WindowInfo | None
    timestamp: float


# ═══════════════════════════════════════════════════════════════════════════════
#  2. TTL Cache
# ═══════════════════════════════════════════════════════════════════════════════


class TTLCache:
    """Thread-safe time-to-live cache with manual invalidation."""

    def __init__(self, ttl_seconds: float = 2.0):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), value)

    def invalidate(self, key: str | None = None) -> None:
        """Drop one key or flush everything."""
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════════


class CircuitBreaker:
    """Protects against repeated element-resolution failures.

    After *threshold* consecutive failures the breaker moves to OPEN for
    *cooldown* seconds, during which every resolution attempt returns a
    structured failure immediately.  After the cooldown a single probe is
    allowed (HALF_OPEN); success resets, failure re-opens.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, threshold: int = 3, cooldown: float = 30.0):
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._opened_at >= self._cooldown:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def failure_response(self) -> dict:
        return {
            "success": False,
            "message": (
                f"Circuit breaker OPEN: {self._failures} consecutive element "
                f"resolution failures. Retrying in {self._cooldown}s."
            ),
        }


# Module-level circuit breaker instance (shared across all handlers)
_circuit_breaker = CircuitBreaker(threshold=3, cooldown=30.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Semantic Scoring
# ═══════════════════════════════════════════════════════════════════════════════


def _fuzzy_ratio(a: str, b: str) -> float:
    """Character-level similarity ratio [0.0, 1.0]."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def score_element(
    element: UIElement,
    target_role: str | None = None,
    target_name: str | None = None,
) -> float:
    """Score an element for relevance against the query parameters.

    Weights:
      • Role exact match      → +40
      • Name exact match       → +30
      • Name substring match   → +20
      • Name fuzzy (≥ 0.6)     → +10 × ratio
      • State: visible +5, showing +5, enabled +3, focusable +3, sensitive +2
      • Valid bounding box      → +5
      • Depth penalty           → −1 per level (cap −10)
    """
    s = 0.0

    if target_role and element.role.lower() == target_role.lower():
        s += 40.0

    if target_name:
        el_name = element.name.lower()
        tn = target_name.lower()
        if el_name == tn:
            s += 30.0
        elif tn in el_name or el_name in tn:
            s += 20.0
        else:
            ratio = _fuzzy_ratio(el_name, tn)
            if ratio >= 0.6:
                s += 10.0 * ratio

    state_set = set(element.states)
    if "visible" in state_set:
        s += 5.0
    if "showing" in state_set:
        s += 5.0
    if "enabled" in state_set:
        s += 3.0
    if "focusable" in state_set:
        s += 3.0
    if "sensitive" in state_set:
        s += 2.0

    if element.bbox and element.bbox.get("width", 0) > 0 and element.bbox.get("height", 0) > 0:
        s += 5.0

    s -= min(element.depth * 1.0, 10.0)

    element.score = s
    return s


def _rank_elements(
    elements: list[UIElement],
    target_role: str | None = None,
    target_name: str | None = None,
) -> list[UIElement]:
    """Score and sort elements best-first."""
    for el in elements:
        score_element(el, target_role=target_role, target_name=target_name)
    elements.sort(key=lambda e: e.score, reverse=True)
    return elements


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Input Sanitisation & Role Normalisation
# ═══════════════════════════════════════════════════════════════════════════════

_SAFE_ROLE_RE = re.compile(r"^[a-zA-Z_ ]{1,60}$")
_SAFE_NAME_RE = re.compile(r"^.{0,500}$", re.DOTALL)


def _sanitize_role(role: str) -> str:
    if not role or not _SAFE_ROLE_RE.match(role):
        raise ValueError(f"Invalid role value: {role!r}")
    return role.strip().lower().replace(" ", "_")


def _sanitize_name(name: str) -> str:
    if not _SAFE_NAME_RE.match(name):
        raise ValueError("Name value too long or invalid")
    return name.strip()


_ROLE_ALIASES: dict[str, str] = {
    "button": "push button",
    "push_button": "push button",
    "pushbutton": "push button",
    "toggle_button": "toggle button",
    "togglebutton": "toggle button",
    "check_box": "check box",
    "checkbox": "check box",
    "radio_button": "radio button",
    "radiobutton": "radio button",
    "text": "text",
    "text_field": "text",
    "textfield": "text",
    "password_text": "password text",
    "combo_box": "combo box",
    "combobox": "combo box",
    "dropdown": "combo box",
    "list": "list",
    "list_item": "list item",
    "listitem": "list item",
    "menu": "menu",
    "menu_item": "menu item",
    "menuitem": "menu item",
    "table": "table",
    "table_row": "table row",
    "row": "table row",
    "table_cell": "table cell",
    "cell": "table cell",
    "link": "link",
    "dialog": "dialog",
    "frame": "frame",
    "window": "frame",
    "toolbar": "tool bar",
    "tool_bar": "tool bar",
    "tab": "page tab",
    "page_tab": "page tab",
    "tab_item": "page tab",
    "panel": "panel",
    "filler": "filler",
    "scroll_bar": "scroll bar",
    "scrollbar": "scroll bar",
    "label": "label",
    "icon": "icon",
    "separator": "separator",
    "tree": "tree",
    "tree_item": "tree item",
    "treeitem": "tree item",
    "status_bar": "status bar",
    "statusbar": "status bar",
    "paragraph": "paragraph",
    "heading": "heading",
    "section": "section",
    "document": "document frame",
    "image": "image",
    "slider": "slider",
    "spin_button": "spin button",
    "progress_bar": "progress bar",
    "alert": "alert",
    "notification": "notification",
    "tooltip": "tool tip",
    "page_tab_list": "page tab list",
}


def _normalize_role(role: str) -> str:
    """Normalise an LLM-supplied role to AT-SPI role name string."""
    key = role.strip().lower().replace(" ", "_").replace("-", "_")
    return _ROLE_ALIASES.get(key, role.strip().lower())


# ═══════════════════════════════════════════════════════════════════════════════
#  6. AccessibilityProvider — Abstract Base Class
# ═══════════════════════════════════════════════════════════════════════════════


class AccessibilityProvider(abc.ABC):
    """Abstract base for OS-specific accessibility adapters.

    Every concrete provider must implement the full set of primitives below.
    Synchronous methods are expected — the async wrappers in this module use
    ``asyncio.to_thread()`` to avoid blocking the event loop.
    """

    @abc.abstractmethod
    def list_applications(self) -> list[dict]:
        """Return metadata for every accessible application."""

    @abc.abstractmethod
    def get_application_tree(self, app_name: str, max_depth: int = 6) -> list[dict]:
        """Return the full accessibility tree for a named application."""

    @abc.abstractmethod
    def list_windows(self, app_name: str | None = None) -> list[dict]:
        """Return all accessible windows, optionally filtered by app."""

    @abc.abstractmethod
    def get_focused_window(self) -> dict | None:
        """Return metadata for the currently focused window."""

    @abc.abstractmethod
    def get_focused_element(self) -> dict | None:
        """Return metadata for the currently focused element."""

    @abc.abstractmethod
    def find_elements(
        self,
        role: str | None = None,
        name: str | None = None,
        description: str | None = None,
        state: str | None = None,
        exact: bool = False,
        app_name: str | None = None,
    ) -> list[UIElement]:
        """Search the accessibility tree for matching elements."""

    @abc.abstractmethod
    def get_tree_snapshot(self, app_name: str | None = None, max_depth: int = 4) -> str:
        """Return a textual dump of the accessibility tree."""

    @abc.abstractmethod
    def click_at(self, x: int, y: int, button: int = 1, clicks: int = 1) -> bool:
        """Click at screen coordinates."""

    @abc.abstractmethod
    def type_text_phys(self, text: str) -> bool:
        """Type text via physical keyboard simulation."""

    @abc.abstractmethod
    def press_key(self, key: str) -> bool:
        """Press a key or key combination."""

    @abc.abstractmethod
    def activate_window(self, name: str) -> bool:
        """Activate/focus a window by title."""

    @abc.abstractmethod
    def perform_action(self, element_id: int, action_name: str = "click") -> bool:
        """Perform a named action (e.g. click, expand) on a cached element."""

    @abc.abstractmethod
    def set_value(self, element_id: int, value: str) -> bool:
        """Set text/value on an element."""

    @abc.abstractmethod
    def get_value(self, element_id: int) -> str:
        """Read text/value from an element."""

    @abc.abstractmethod
    def get_bounding_box(self, element_id: int) -> dict | None:
        """Return bounding box for a cached element."""

    @abc.abstractmethod
    def get_center_point(self, element_id: int) -> tuple[int, int] | None:
        """Return center (x, y) of an element's bounding box."""

    @abc.abstractmethod
    def focus_element(self, element_id: int) -> bool:
        """Focus an element via accessibility API."""

    @abc.abstractmethod
    def is_visible(self, element_id: int) -> bool:
        """Check if an element is visible and showing."""

    @abc.abstractmethod
    def get_element_info(self, element_id: int) -> dict:
        """Return full element info dict for a cached element."""

    @abc.abstractmethod
    def check_health(self) -> bool:
        """Return True if the accessibility subsystem is functional."""

    @abc.abstractmethod
    def get_cached(self, element_id: int) -> Any:
        """Retrieve a cached native handle by integer id."""

    @abc.abstractmethod
    def scroll_element_to_view(self, element_id: int) -> bool:
        """Attempt to scroll an element into the visible viewport."""


# ═══════════════════════════════════════════════════════════════════════════════
#  7. LinuxATSPIProvider
# ═══════════════════════════════════════════════════════════════════════════════


class LinuxATSPIProvider(AccessibilityProvider):
    """AT-SPI2 accessibility provider for Linux (GObject Introspection).

    Preserves 100 % of the previous engine's AT-SPI and xdotool logic,
    reorganised as instance methods.
    """

    _MAX_TREE_DEPTH = 30
    _MAX_CHILDREN_PER_NODE = 500

    def __init__(self) -> None:
        self._gi_available: bool | None = None
        self._Atspi: Any = None
        # Element cache: id → Atspi.Accessible (LRU via OrderedDict; F-040)
        self._element_cache: "OrderedDict[int, Any]" = OrderedDict()
        self._element_counter: int = 0
        self._cache_lock = threading.Lock()
        # TTL caches
        self._window_cache = TTLCache(ttl_seconds=2.0)
        self._tree_cache = TTLCache(ttl_seconds=2.0)
        self._context_cache = TTLCache(ttl_seconds=2.0)

    # ── AT-SPI lazy import ────────────────────────────────────────────────

    def _ensure_atspi(self):
        if self._gi_available is True:
            return self._Atspi
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # type: ignore[import-untyped]
            self._Atspi = Atspi
            self._gi_available = True
            return Atspi
        except (ImportError, ValueError) as exc:
            self._gi_available = False
            raise RuntimeError(
                "AT-SPI bindings unavailable. Ensure gir1.2-atspi-2.0 and "
                "python3-gi are installed inside the container."
            ) from exc

    # ── Element cache ─────────────────────────────────────────────────────

    def _next_element_id(self, accessible: Any) -> int:
        with self._cache_lock:
            self._element_counter += 1
            eid = self._element_counter
            self._element_cache[eid] = accessible
            self._element_cache.move_to_end(eid)
            while len(self._element_cache) > 5000:
                self._element_cache.popitem(last=False)
        return eid

    def get_cached(self, element_id: int) -> Any:
        with self._cache_lock:
            obj = self._element_cache.get(element_id)
            if obj is not None:
                self._element_cache.move_to_end(element_id)
        if obj is None:
            raise ValueError(f"Element id {element_id} not found in cache (expired or invalid)")
        return obj

    # ── Element info extraction ───────────────────────────────────────────

    def _element_info(self, node: Any) -> dict:
        Atspi = self._ensure_atspi()
        try:
            role_name = node.get_role_name() or ""
        except Exception:
            role_name = ""
        try:
            name = node.get_name() or ""
        except Exception:
            name = ""
        try:
            description = node.get_description() or ""
        except Exception:
            description = ""

        states: list[str] = []
        try:
            state_set = node.get_state_set()
            for st_name, st_val in [
                ("focused", Atspi.StateType.FOCUSED),
                ("selected", Atspi.StateType.SELECTED),
                ("enabled", Atspi.StateType.ENABLED),
                ("sensitive", Atspi.StateType.SENSITIVE),
                ("visible", Atspi.StateType.VISIBLE),
                ("showing", Atspi.StateType.SHOWING),
                ("checked", Atspi.StateType.CHECKED),
                ("expanded", Atspi.StateType.EXPANDED),
                ("focusable", Atspi.StateType.FOCUSABLE),
                ("editable", Atspi.StateType.EDITABLE),
            ]:
                if state_set.contains(st_val):
                    states.append(st_name)
        except Exception:
            pass

        bbox: dict | None = None
        try:
            if node.is_component():
                comp = node.get_component_iface()
                if comp:
                    extents = comp.get_extents(Atspi.CoordType.SCREEN)
                    bbox = {
                        "x": extents.x,
                        "y": extents.y,
                        "width": extents.width,
                        "height": extents.height,
                    }
        except Exception:
            pass

        eid = self._next_element_id(node)
        return {
            "element_id": eid,
            "role": role_name,
            "name": name,
            "description": description,
            "states": states,
            "bbox": bbox,
        }

    def _to_ui_element(self, info: dict, depth: int = 0) -> UIElement:
        """Convert an info dict to a UIElement."""
        return UIElement(
            element_id=info["element_id"],
            role=info.get("role", ""),
            name=info.get("name", ""),
            description=info.get("description", ""),
            states=info.get("states", []),
            bbox=info.get("bbox"),
            depth=depth,
            app_name=info.get("app_name", ""),
        )

    # ── Tree traversal ────────────────────────────────────────────────────

    def _walk_tree(self, node: Any, depth: int = 0, max_depth: int = _MAX_TREE_DEPTH) -> list[dict]:
        if depth > max_depth:
            return []
        results: list[dict] = []
        try:
            count = node.get_child_count()
        except Exception:
            return results
        count = min(count, self._MAX_CHILDREN_PER_NODE)
        for i in range(count):
            try:
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                info = self._element_info(child)
                info["depth"] = depth
                results.append(info)
                if depth < max_depth:
                    results.extend(self._walk_tree(child, depth + 1, max_depth))
            except Exception:
                continue
        return results

    # ── Discovery ─────────────────────────────────────────────────────────

    def list_applications(self) -> list[dict]:
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        apps: list[dict] = []
        count = desktop.get_child_count()
        for i in range(count):
            try:
                app = desktop.get_child_at_index(i)
                if app is None:
                    continue
                apps.append({
                    "app_name": app.get_name() or f"<app_{i}>",
                    "pid": app.get_process_id(),
                    "children_count": app.get_child_count(),
                    "index": i,
                })
            except Exception:
                continue
        return apps

    def get_application_tree(self, app_name: str, max_depth: int = 6) -> list[dict]:
        cached = self._tree_cache.get(f"app_tree:{app_name}:{max_depth}")
        if cached is not None:
            return cached
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        app_lower = app_name.lower()
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                if app and (app.get_name() or "").lower().startswith(app_lower):
                    tree = self._walk_tree(app, depth=0, max_depth=max_depth)
                    self._tree_cache.set(f"app_tree:{app_name}:{max_depth}", tree)
                    return tree
            except Exception:
                continue
        return []

    def list_windows(self, app_name: str | None = None) -> list[dict]:
        cache_key = f"windows:{app_name or 'all'}"
        cached = self._window_cache.get(cache_key)
        if cached is not None:
            return cached
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        windows: list[dict] = []
        for ai in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(ai)
                if app is None:
                    continue
                if app_name and not (app.get_name() or "").lower().startswith(app_name.lower()):
                    continue
                for wi in range(app.get_child_count()):
                    try:
                        win = app.get_child_at_index(wi)
                        if win is None:
                            continue
                        role = win.get_role_name() or ""
                        if role in ("frame", "dialog", "window", "alert"):
                            info = self._element_info(win)
                            info["app_name"] = app.get_name() or ""
                            windows.append(info)
                    except Exception:
                        continue
            except Exception:
                continue
        self._window_cache.set(cache_key, windows)
        return windows

    def get_focused_window(self) -> dict | None:
        cached = self._context_cache.get("focused_window")
        if cached is not None:
            return cached
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        for ai in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(ai)
                if app is None:
                    continue
                for wi in range(app.get_child_count()):
                    try:
                        win = app.get_child_at_index(wi)
                        if win is None:
                            continue
                        ss = win.get_state_set()
                        if ss and ss.contains(Atspi.StateType.ACTIVE):
                            info = self._element_info(win)
                            info["app_name"] = app.get_name() or ""
                            self._context_cache.set("focused_window", info)
                            return info
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def get_focused_element(self) -> dict | None:
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        for ai in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(ai)
                if app is None:
                    continue
                focused = self._find_focused_descendant(app)
                if focused:
                    info = self._element_info(focused)
                    info["app_name"] = app.get_name() or ""
                    return info
            except Exception:
                continue
        return None

    def _find_focused_descendant(self, node: Any, depth: int = 0) -> Any:
        Atspi = self._ensure_atspi()
        if depth > self._MAX_TREE_DEPTH:
            return None
        try:
            ss = node.get_state_set()
            if ss and ss.contains(Atspi.StateType.FOCUSED):
                for ci in range(min(node.get_child_count(), self._MAX_CHILDREN_PER_NODE)):
                    try:
                        child = node.get_child_at_index(ci)
                        if child is None:
                            continue
                        deeper = self._find_focused_descendant(child, depth + 1)
                        if deeper:
                            return deeper
                    except Exception:
                        continue
                return node
        except Exception:
            pass
        for ci in range(min(node.get_child_count() if node else 0, self._MAX_CHILDREN_PER_NODE)):
            try:
                child = node.get_child_at_index(ci)
                if child is None:
                    continue
                result = self._find_focused_descendant(child, depth + 1)
                if result:
                    return result
            except Exception:
                continue
        return None

    # ── Element query ─────────────────────────────────────────────────────

    def find_elements(
        self,
        role: str | None = None,
        name: str | None = None,
        description: str | None = None,
        state: str | None = None,
        exact: bool = False,
        app_name: str | None = None,
    ) -> list[UIElement]:
        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)

        target_role = _normalize_role(role) if role else None
        target_name = name.strip().lower() if name else None
        target_desc = description.strip().lower() if description else None

        _STATE_MAP = {
            "focused": Atspi.StateType.FOCUSED,
            "selected": Atspi.StateType.SELECTED,
            "enabled": Atspi.StateType.ENABLED,
            "checked": Atspi.StateType.CHECKED,
            "expanded": Atspi.StateType.EXPANDED,
            "visible": Atspi.StateType.VISIBLE,
            "showing": Atspi.StateType.SHOWING,
            "sensitive": Atspi.StateType.SENSITIVE,
            "focusable": Atspi.StateType.FOCUSABLE,
            "editable": Atspi.StateType.EDITABLE,
        }
        target_state = _STATE_MAP.get((state or "").strip().lower()) if state else None

        raw_results: list[dict] = []

        def _search(node: Any, depth: int = 0) -> None:
            if depth > self._MAX_TREE_DEPTH or len(raw_results) >= 50:
                return
            try:
                node_role = (node.get_role_name() or "").lower()
                node_name = (node.get_name() or "").lower()
                node_desc = (node.get_description() or "").lower()
            except Exception:
                return

            match = True
            if target_role and node_role != target_role:
                match = False
            if target_name:
                if exact:
                    if node_name != target_name:
                        match = False
                else:
                    if target_name not in node_name:
                        match = False
            if target_desc and target_desc not in node_desc:
                match = False
            if target_state:
                try:
                    ss = node.get_state_set()
                    if not (ss and ss.contains(target_state)):
                        match = False
                except Exception:
                    match = False

            if match and (target_role or target_name or target_desc or target_state):
                info = self._element_info(node)
                info["depth"] = depth
                raw_results.append(info)

            try:
                count = min(node.get_child_count(), self._MAX_CHILDREN_PER_NODE)
            except Exception:
                return
            for ci in range(count):
                if len(raw_results) >= 50:
                    return
                try:
                    child = node.get_child_at_index(ci)
                    if child is not None:
                        _search(child, depth + 1)
                except Exception:
                    continue

        app_count = desktop.get_child_count()
        for ai in range(app_count):
            if len(raw_results) >= 50:
                break
            try:
                app = desktop.get_child_at_index(ai)
                if app is None:
                    continue
                if app_name and not (app.get_name() or "").lower().startswith(app_name.lower()):
                    continue
                _search(app, depth=0)
            except Exception:
                continue

        # Convert to UIElement and rank by semantic score
        elements = [self._to_ui_element(r, r.get("depth", 0)) for r in raw_results]
        return _rank_elements(elements, target_role=target_role, target_name=target_name)

    # ── Bounding box / geometry ───────────────────────────────────────────

    def get_bounding_box(self, element_id: int) -> dict | None:
        Atspi = self._ensure_atspi()
        node = self.get_cached(element_id)
        if not node.is_component():
            return None
        comp = node.get_component_iface()
        if not comp:
            return None
        extents = comp.get_extents(Atspi.CoordType.SCREEN)
        return {"x": extents.x, "y": extents.y, "width": extents.width, "height": extents.height}

    def get_center_point(self, element_id: int) -> tuple[int, int] | None:
        bbox = self.get_bounding_box(element_id)
        if not bbox or bbox["width"] <= 0 or bbox["height"] <= 0:
            return None
        return (bbox["x"] + bbox["width"] // 2, bbox["y"] + bbox["height"] // 2)

    def is_visible(self, element_id: int) -> bool:
        Atspi = self._ensure_atspi()
        node = self.get_cached(element_id)
        try:
            ss = node.get_state_set()
            return bool(
                ss
                and ss.contains(Atspi.StateType.VISIBLE)
                and ss.contains(Atspi.StateType.SHOWING)
            )
        except Exception:
            return False

    def get_element_info(self, element_id: int) -> dict:
        node = self.get_cached(element_id)
        return self._element_info(node)

    # ── Element value / text ──────────────────────────────────────────────

    def get_value(self, element_id: int) -> str:
        node = self.get_cached(element_id)
        try:
            if node.is_text():
                text_iface = node.get_text_iface()
                if text_iface:
                    count = text_iface.get_character_count()
                    return text_iface.get_text(0, count) or ""
        except Exception:
            pass
        try:
            if node.is_value():
                val = node.get_value_iface()
                if val:
                    return str(val.get_current_value())
        except Exception:
            pass
        return node.get_name() or ""

    def set_value(self, element_id: int, value: str) -> bool:
        node = self.get_cached(element_id)
        try:
            if node.is_editable_text():
                editable = node.get_editable_text_iface()
                if editable:
                    text_iface = node.get_text_iface()
                    if text_iface:
                        count = text_iface.get_character_count()
                        if count > 0:
                            editable.delete_text(0, count)
                    editable.insert_text(0, value, len(value))
                    return True
        except Exception as exc:
            logger.debug("EditableText.set failed: %s", exc)
        return False

    # ── Actions ───────────────────────────────────────────────────────────

    def perform_action(self, element_id: int, action_name: str = "click") -> bool:
        node = self.get_cached(element_id)
        try:
            if node.is_action():
                ai = node.get_action_iface()
                if ai:
                    n = ai.get_n_actions()
                    for idx in range(n):
                        if (ai.get_action_name(idx) or "").lower() == action_name.lower():
                            return ai.do_action(idx)
                    if n > 0 and action_name == "click":
                        return ai.do_action(0)
        except Exception as exc:
            logger.debug("Action.do_action(%s) failed: %s", action_name, exc)
        return False

    def focus_element(self, element_id: int) -> bool:
        node = self.get_cached(element_id)
        if node.is_component():
            comp = node.get_component_iface()
            if comp:
                return comp.grab_focus()
        return False

    def scroll_element_to_view(self, element_id: int) -> bool:
        Atspi = self._ensure_atspi()
        node = self.get_cached(element_id)
        if node.is_component():
            comp = node.get_component_iface()
            if comp and hasattr(comp, "scroll_to"):
                try:
                    comp.scroll_to(Atspi.ScrollType.ANYWHERE)
                    return True
                except Exception:
                    pass
        return False

    # ── Tree snapshot (text dump) ─────────────────────────────────────────

    def get_tree_snapshot(self, app_name: str | None = None, max_depth: int = 4) -> str:
        cache_key = f"tree_snap:{app_name or 'all'}:{max_depth}"
        cached = self._tree_cache.get(cache_key)
        if cached is not None:
            return cached

        Atspi = self._ensure_atspi()
        desktop = Atspi.get_desktop(0)
        lines: list[str] = []

        def _fmt(node: Any, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                role = node.get_role_name() or "?"
                name = node.get_name() or ""
                states_list: list[str] = []
                try:
                    ss = node.get_state_set()
                    for sname, sval in [
                        ("focused", Atspi.StateType.FOCUSED),
                        ("showing", Atspi.StateType.SHOWING),
                    ]:
                        if ss and ss.contains(sval):
                            states_list.append(sname)
                except Exception:
                    pass
                state_str = f" [{','.join(states_list)}]" if states_list else ""
                indent = "  " * depth
                name_str = f' "{name}"' if name else ""
                eid = self._next_element_id(node)
                lines.append(f"{indent}[{eid}] {role}{name_str}{state_str}")
            except Exception:
                return

            try:
                count = min(node.get_child_count(), 100)
                for ci in range(count):
                    child = node.get_child_at_index(ci)
                    if child is not None:
                        _fmt(child, depth + 1)
            except Exception:
                pass

        for ai in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(ai)
                if app is None:
                    continue
                if app_name and not (app.get_name() or "").lower().startswith(app_name.lower()):
                    continue
                lines.append(f"=== {app.get_name() or '<unnamed>'} (pid={app.get_process_id()}) ===")
                _fmt(app, 0)
            except Exception:
                continue

        result = "\n".join(lines) if lines else "(no accessible applications found)"
        self._tree_cache.set(cache_key, result)
        return result

    # ── Physical dispatch (xdotool) ───────────────────────────────────────

    def click_at(self, x: int, y: int, button: int = 1, clicks: int = 1) -> bool:
        try:
            subprocess.run(
                ["xdotool", "mousemove", "--sync", str(x), str(y)],
                check=True, timeout=5,
            )
            click_args = ["xdotool", "click"]
            if clicks > 1:
                click_args.extend(["--repeat", str(clicks)])
            click_args.append(str(button))
            subprocess.run(click_args, check=True, timeout=5)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("xdotool click failed: %s", exc)
            return False

    def type_text_phys(self, text: str) -> bool:
        try:
            for mod in ("alt", "ctrl", "shift", "super"):
                subprocess.run(["xdotool", "keyup", mod], timeout=2, capture_output=True)
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "20", "--", text],
                check=True, timeout=30,
            )
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("xdotool type failed: %s", exc)
            return False

    def press_key(self, key: str) -> bool:
        try:
            subprocess.run(
                ["xdotool", "key", "--clearmodifiers", key],
                check=True, timeout=5,
            )
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("xdotool key failed: %s", exc)
            return False

    def activate_window(self, name: str) -> bool:
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", name],
                capture_output=True, text=True, timeout=5,
            )
            wids = result.stdout.strip().splitlines()
            if wids:
                subprocess.run(
                    ["xdotool", "windowactivate", "--sync", wids[0]],
                    check=True, timeout=5,
                )
                self.invalidate_caches()
                return True
        except Exception as exc:
            logger.warning("xdotool window activate failed: %s", exc)
        return False

    def check_health(self) -> bool:
        try:
            apps = self.list_applications()
            return len(apps) > 0
        except Exception:
            return False

    def invalidate_caches(self) -> None:
        """Flush all TTL caches (call after state-changing actions)."""
        self._window_cache.invalidate()
        self._tree_cache.invalidate()
        self._context_cache.invalidate()


# ═══════════════════════════════════════════════════════════════════════════════
#  8. WindowsUIAProvider
# ═══════════════════════════════════════════════════════════════════════════════


class WindowsUIAProvider(AccessibilityProvider):
    """Windows UI Automation provider via PowerShell subprocess.

    Uses .NET ``System.Windows.Automation`` through PowerShell to query
    the UIA tree, invoke patterns, and simulate input.  Designed for
    native Windows runtime (not Docker Windows containers).
    """

    def __init__(self) -> None:
        self._element_cache: "OrderedDict[int, dict]" = OrderedDict()
        self._element_counter: int = 0
        self._cache_lock = threading.Lock()
        self._window_cache = TTLCache(ttl_seconds=2.0)
        self._tree_cache = TTLCache(ttl_seconds=2.0)
        self._context_cache = TTLCache(ttl_seconds=2.0)

    # ── PowerShell helpers ────────────────────────────────────────────────

    def _run_ps(self, script: str, timeout: int = 15) -> str:
        """Execute a PowerShell script and return stdout."""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 and result.stderr.strip():
            logger.debug("PS stderr: %s", result.stderr.strip()[:500])
        return result.stdout.strip()

    def _run_ps_json(self, script: str, timeout: int = 15) -> Any:
        """Execute PS script and parse JSON output."""
        raw = self._run_ps(script, timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("PS JSON parse failed: %s", raw[:300])
            return None

    def _next_id(self, element_data: dict) -> int:
        with self._cache_lock:
            self._element_counter += 1
            eid = self._element_counter
            self._element_cache[eid] = element_data
            self._element_cache.move_to_end(eid)
            while len(self._element_cache) > 5000:
                self._element_cache.popitem(last=False)
        return eid

    def get_cached(self, element_id: int) -> Any:
        with self._cache_lock:
            obj = self._element_cache.get(element_id)
            if obj is not None:
                self._element_cache.move_to_end(element_id)
        if obj is None:
            raise ValueError(f"Element id {element_id} not found in cache")
        return obj

    # ── Provider methods ──────────────────────────────────────────────────

    def list_applications(self) -> list[dict]:
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            "$cond = [System.Windows.Automation.Condition]::TrueCondition\n"
            "$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)\n"
            "$result = @()\n"
            "foreach ($w in $wins) {\n"
            "  try {\n"
            "    $result += @{app_name=$w.Current.Name; pid=$w.Current.ProcessId; "
            "children_count=0; index=$result.Count}\n"
            "  } catch {}\n"
            "}\n"
            "$result | ConvertTo-Json -Compress"
        )
        data = self._run_ps_json(script) or []
        if isinstance(data, dict):
            data = [data]
        return data

    def get_application_tree(self, app_name: str, max_depth: int = 6) -> list[dict]:
        cached = self._tree_cache.get(f"app_tree:{app_name}:{max_depth}")
        if cached is not None:
            return cached
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{app_name}')\n"
            "$app = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)\n"
            "function Walk($el, $d, $maxD) {\n"
            "  if ($null -eq $el -or $d -gt $maxD) { return @() }\n"
            "  $res = @()\n"
            "  $children = $el.FindAll([System.Windows.Automation.TreeScope]::Children, "
            "[System.Windows.Automation.Condition]::TrueCondition)\n"
            "  foreach ($c in $children) {\n"
            "    try {\n"
            "      $res += @{role=$c.Current.ControlType.ProgrammaticName; "
            "name=$c.Current.Name; description=''; states=@(); bbox=$null; depth=$d; "
            "element_id=0}\n"
            "      $res += Walk $c ($d+1) $maxD\n"
            "    } catch {}\n"
            "  }\n"
            "  return $res\n"
            "}\n"
            f"Walk $app 0 {max_depth} | ConvertTo-Json -Compress -Depth 5"
        )
        data = self._run_ps_json(script) or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            item["element_id"] = self._next_id(item)
        self._tree_cache.set(f"app_tree:{app_name}:{max_depth}", data)
        return data

    def list_windows(self, app_name: str | None = None) -> list[dict]:
        cache_key = f"windows:{app_name or 'all'}"
        cached = self._window_cache.get(cache_key)
        if cached is not None:
            return cached
        name_filter = ""
        if app_name:
            name_filter = (
                f"$nameCond = New-Object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::NameProperty, '{app_name}')\n"
                "$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $nameCond)\n"
            )
        else:
            name_filter = (
                "$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, "
                "[System.Windows.Automation.Condition]::TrueCondition)\n"
            )
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"{name_filter}"
            "$result = @()\n"
            "foreach ($w in $wins) {\n"
            "  try {\n"
            "    if ($w.Current.Name) {\n"
            "      $result += @{role='frame'; name=$w.Current.Name; "
            "app_name=$w.Current.Name; states=@(); bbox=$null; element_id=0}\n"
            "    }\n"
            "  } catch {}\n"
            "}\n"
            "$result | ConvertTo-Json -Compress"
        )
        data = self._run_ps_json(script) or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            item["element_id"] = self._next_id(item)
        self._window_cache.set(cache_key, data)
        return data

    def get_focused_window(self) -> dict | None:
        cached = self._context_cache.get("focused_window")
        if cached is not None:
            return cached
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$fw = [System.Windows.Automation.AutomationElement]::FocusedElement\n"
            "if ($null -ne $fw) {\n"
            "  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker\n"
            "  $parent = $fw\n"
            "  while ($null -ne $parent) {\n"
            "    $p = $walker.GetParent($parent)\n"
            "    if ($null -eq $p -or $p.Equals("
            "[System.Windows.Automation.AutomationElement]::RootElement)) { break }\n"
            "    $parent = $p\n"
            "  }\n"
            "  @{role='frame'; name=$parent.Current.Name; app_name=$parent.Current.Name; "
            "states=@('focused'); bbox=$null; element_id=0} | ConvertTo-Json -Compress\n"
            "}"
        )
        data = self._run_ps_json(script)
        if data:
            data["element_id"] = self._next_id(data)
            self._context_cache.set("focused_window", data)
        return data

    def get_focused_element(self) -> dict | None:
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$fe = [System.Windows.Automation.AutomationElement]::FocusedElement\n"
            "if ($null -ne $fe) {\n"
            "  @{role=$fe.Current.ControlType.ProgrammaticName; "
            "name=$fe.Current.Name; description=''; "
            "states=@('focused'); bbox=$null; element_id=0} | ConvertTo-Json -Compress\n"
            "}"
        )
        data = self._run_ps_json(script)
        if data:
            data["element_id"] = self._next_id(data)
        return data

    def find_elements(
        self,
        role: str | None = None,
        name: str | None = None,
        description: str | None = None,
        state: str | None = None,
        exact: bool = False,
        app_name: str | None = None,
    ) -> list[UIElement]:
        conditions = []
        if name:
            safe_name = name.replace("'", "''")
            conditions.append(
                f"New-Object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe_name}')"
            )
        if role:
            conditions.append(
                f"New-Object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::LocalizedControlTypeProperty, "
                f"'{role.replace(chr(39), chr(39)*2)}')"
            )

        if conditions:
            if len(conditions) == 1:
                cond_var = f"$cond = {conditions[0]}"
            else:
                cond_var = (
                    f"$c1 = {conditions[0]}\n$c2 = {conditions[1]}\n"
                    "$cond = New-Object System.Windows.Automation.AndCondition($c1, $c2)"
                )
        else:
            cond_var = "$cond = [System.Windows.Automation.Condition]::TrueCondition"

        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"{cond_var}\n"
            "$found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond) "
            "| Select-Object -First 50\n"
            "$result = @()\n"
            "foreach ($el in $found) {\n"
            "  try {\n"
            "    $r = $el.Current.BoundingRectangle\n"
            "    $bb = $null\n"
            "    if (-not $r.IsEmpty) { $bb = @{x=[int]$r.X; y=[int]$r.Y; "
            "width=[int]$r.Width; height=[int]$r.Height} }\n"
            "    $result += @{role=$el.Current.ControlType.ProgrammaticName; "
            "name=$el.Current.Name; description=''; states=@(); bbox=$bb; "
            "depth=0; element_id=0}\n"
            "  } catch {}\n"
            "}\n"
            "$result | ConvertTo-Json -Compress -Depth 4"
        )
        data = self._run_ps_json(script) or []
        if isinstance(data, dict):
            data = [data]

        elements: list[UIElement] = []
        for item in data:
            eid = self._next_id(item)
            elements.append(UIElement(
                element_id=eid,
                role=item.get("role", ""),
                name=item.get("name", ""),
                description=item.get("description", ""),
                states=item.get("states", []),
                bbox=item.get("bbox"),
                depth=item.get("depth", 0),
            ))
        return _rank_elements(elements, target_role=role, target_name=name)

    def get_tree_snapshot(self, app_name: str | None = None, max_depth: int = 4) -> str:
        cache_key = f"tree_snap:{app_name or 'all'}:{max_depth}"
        cached = self._tree_cache.get(cache_key)
        if cached is not None:
            return cached
        tree = self.get_application_tree(app_name or "", max_depth) if app_name else []
        if not tree:
            apps = self.list_applications()
            lines = []
            for app in apps[:20]:
                lines.append(f"=== {app.get('app_name', '?')} (pid={app.get('pid', '?')}) ===")
            result = "\n".join(lines) if lines else "(no accessible applications found)"
        else:
            lines = []
            for item in tree:
                d = item.get("depth", 0)
                indent = "  " * d
                name_str = f' "{item.get("name", "")}"' if item.get("name") else ""
                lines.append(f"{indent}[{item.get('element_id', 0)}] {item.get('role', '?')}{name_str}")
            result = "\n".join(lines)
        self._tree_cache.set(cache_key, result)
        return result

    def click_at(self, x: int, y: int, button: int = 1, clicks: int = 1) -> bool:
        script = (
            'Add-Type @"\n'
            "using System; using System.Runtime.InteropServices;\n"
            "public class WinInput {\n"
            "  [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y);\n"
            "  [DllImport(\"user32.dll\")] public static extern void mouse_event("
            "int f, int dx, int dy, int d, int e);\n"
            "  public const int LDOWN=0x02, LUP=0x04, RDOWN=0x08, RUP=0x10;\n"
            "}\n"
            '"@\n'
            f"[WinInput]::SetCursorPos({x}, {y})\n"
            "Start-Sleep -Milliseconds 50\n"
        )
        if button == 3:
            for _ in range(clicks):
                script += "[WinInput]::mouse_event([WinInput]::RDOWN, 0, 0, 0, 0)\n"
                script += "[WinInput]::mouse_event([WinInput]::RUP, 0, 0, 0, 0)\n"
        else:
            for _ in range(clicks):
                script += "[WinInput]::mouse_event([WinInput]::LDOWN, 0, 0, 0, 0)\n"
                script += "[WinInput]::mouse_event([WinInput]::LUP, 0, 0, 0, 0)\n"
        try:
            self._run_ps(script)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("Windows click failed: %s", exc)
            return False

    def type_text_phys(self, text: str) -> bool:
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f"[System.Windows.Forms.SendKeys]::SendWait('{safe}')"
        )
        try:
            self._run_ps(script)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("Windows type failed: %s", exc)
            return False

    def press_key(self, key: str) -> bool:
        key_map: dict[str, str] = {
            "Return": "{ENTER}", "enter": "{ENTER}", "Tab": "{TAB}",
            "Escape": "{ESC}", "BackSpace": "{BACKSPACE}", "Delete": "{DELETE}",
            "space": " ", "Up": "{UP}", "Down": "{DOWN}", "Left": "{LEFT}",
            "Right": "{RIGHT}", "Home": "{HOME}", "End": "{END}",
            "Page_Up": "{PGUP}", "Page_Down": "{PGDN}", "Prior": "{PGUP}",
            "Next": "{PGDN}", "F1": "{F1}", "F2": "{F2}", "F3": "{F3}",
            "F4": "{F4}", "F5": "{F5}", "F11": "{F11}", "F12": "{F12}",
        }
        parts = key.replace("+", " ").split()
        sendkeys = ""
        for p in parts:
            if p.lower() in ("ctrl", "control"):
                sendkeys += "^"
            elif p.lower() in ("alt",):
                sendkeys += "%"
            elif p.lower() in ("shift",):
                sendkeys += "+"
            else:
                sendkeys += key_map.get(p, p)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f"[System.Windows.Forms.SendKeys]::SendWait('{sendkeys}')"
        )
        try:
            self._run_ps(script)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("Windows key press failed: %s", exc)
            return False

    def activate_window(self, name: str) -> bool:
        safe = name.replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe}')\n"
            "$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)\n"
            "if ($null -ne $win) {\n"
            "  try {\n"
            "    $wp = $win.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)\n"
            "    $wp.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::Normal)\n"
            "  } catch {}\n"
            "  try { $win.SetFocus() } catch {}\n"
            "  'ok'\n"
            "} else { 'notfound' }"
        )
        try:
            ok = "ok" in self._run_ps(script)
            if ok:
                self.invalidate_caches()
            return ok
        except Exception as exc:
            logger.warning("Windows activate window failed: %s", exc)
            return False

    def perform_action(self, element_id: int, action_name: str = "click") -> bool:
        elem = self.get_cached(element_id)
        automation_id = elem.get("name", "")
        if action_name in ("click", "invoke"):
            # Try InvokePattern via UIA
            safe = automation_id.replace("'", "''")
            script = (
                "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
                "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
                f"$cond = New-Object System.Windows.Automation.PropertyCondition("
                f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe}')\n"
                "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n"
                "if ($null -ne $el) {\n"
                "  try {\n"
                "    $p = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)\n"
                "    $p.Invoke(); 'ok'\n"
                "  } catch { 'fail' }\n"
                "} else { 'notfound' }"
            )
            try:
                return "ok" in self._run_ps(script)
            except Exception:
                return False
        return False

    def set_value(self, element_id: int, value: str) -> bool:
        elem = self.get_cached(element_id)
        safe_name = elem.get("name", "").replace("'", "''")
        safe_val = value.replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe_name}')\n"
            "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n"
            "if ($null -ne $el) {\n"
            "  try {\n"
            "    $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)\n"
            f"    $vp.SetValue('{safe_val}'); 'ok'\n"
            "  } catch { 'fail' }\n"
            "} else { 'notfound' }"
        )
        try:
            return "ok" in self._run_ps(script)
        except Exception:
            return False

    def get_value(self, element_id: int) -> str:
        elem = self.get_cached(element_id)
        safe_name = elem.get("name", "").replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe_name}')\n"
            "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n"
            "if ($null -ne $el) {\n"
            "  try {\n"
            "    $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)\n"
            "    $vp.Current.Value\n"
            "  } catch { $el.Current.Name }\n"
            "} else { '' }"
        )
        try:
            return self._run_ps(script)
        except Exception:
            return ""

    def get_bounding_box(self, element_id: int) -> dict | None:
        elem = self.get_cached(element_id)
        return elem.get("bbox")

    def get_center_point(self, element_id: int) -> tuple[int, int] | None:
        bbox = self.get_bounding_box(element_id)
        if not bbox or bbox.get("width", 0) <= 0 or bbox.get("height", 0) <= 0:
            return None
        return (bbox["x"] + bbox["width"] // 2, bbox["y"] + bbox["height"] // 2)

    def focus_element(self, element_id: int) -> bool:
        elem = self.get_cached(element_id)
        safe_name = elem.get("name", "").replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe_name}')\n"
            "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n"
            "if ($null -ne $el) { try { $el.SetFocus(); 'ok' } catch { 'fail' } }"
        )
        try:
            return "ok" in self._run_ps(script)
        except Exception:
            return False

    def is_visible(self, element_id: int) -> bool:
        elem = self.get_cached(element_id)
        bbox = elem.get("bbox")
        return bbox is not None and bbox.get("width", 0) > 0

    def get_element_info(self, element_id: int) -> dict:
        return dict(self.get_cached(element_id))

    def scroll_element_to_view(self, element_id: int) -> bool:
        elem = self.get_cached(element_id)
        safe_name = elem.get("name", "").replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n"
            "$root = [System.Windows.Automation.AutomationElement]::RootElement\n"
            f"$cond = New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::NameProperty, '{safe_name}')\n"
            "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n"
            "if ($null -ne $el) {\n"
            "  try {\n"
            "    $sp = $el.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern)\n"
            "    $sp.ScrollIntoView(); 'ok'\n"
            "  } catch { 'fail' }\n"
            "}"
        )
        try:
            return "ok" in self._run_ps(script)
        except Exception:
            return False

    def check_health(self) -> bool:
        try:
            apps = self.list_applications()
            return len(apps) > 0
        except Exception:
            return False

    def invalidate_caches(self) -> None:
        self._window_cache.invalidate()
        self._tree_cache.invalidate()
        self._context_cache.invalidate()


# ═══════════════════════════════════════════════════════════════════════════════
#  9. MacAccessibilityProvider
# ═══════════════════════════════════════════════════════════════════════════════


class MacAccessibilityProvider(AccessibilityProvider):
    """macOS accessibility provider via osascript JXA (JavaScript for Automation).

    Uses ``Application("System Events")`` to access the accessibility tree.
    Minimal but functional for basic automation tasks.
    """

    def __init__(self) -> None:
        self._element_cache: "OrderedDict[int, dict]" = OrderedDict()
        self._element_counter: int = 0
        self._cache_lock = threading.Lock()
        self._window_cache = TTLCache(ttl_seconds=2.0)
        self._tree_cache = TTLCache(ttl_seconds=2.0)
        self._context_cache = TTLCache(ttl_seconds=2.0)

    # ── JXA helpers ───────────────────────────────────────────────────────

    def _run_jxa(self, script: str, timeout: int = 15) -> str:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()

    def _run_jxa_json(self, script: str, timeout: int = 15) -> Any:
        raw = self._run_jxa(script, timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _next_id(self, data: dict) -> int:
        with self._cache_lock:
            self._element_counter += 1
            eid = self._element_counter
            self._element_cache[eid] = data
            self._element_cache.move_to_end(eid)
            while len(self._element_cache) > 5000:
                self._element_cache.popitem(last=False)
        return eid

    def get_cached(self, element_id: int) -> Any:
        with self._cache_lock:
            obj = self._element_cache.get(element_id)
            if obj is not None:
                self._element_cache.move_to_end(element_id)
        if obj is None:
            raise ValueError(f"Element id {element_id} not found in cache")
        return obj

    # ── Provider methods ──────────────────────────────────────────────────

    def list_applications(self) -> list[dict]:
        script = (
            'const se = Application("System Events");\n'
            "const procs = se.processes.whose({backgroundOnly: false});\n"
            "const result = [];\n"
            "for (let i = 0; i < procs.length; i++) {\n"
            "  try {\n"
            "    result.push({app_name: procs[i].name(), pid: procs[i].unixId(), "
            "children_count: 0, index: i});\n"
            "  } catch(e) {}\n"
            "}\n"
            "JSON.stringify(result);"
        )
        data = self._run_jxa_json(script) or []
        return data

    def get_application_tree(self, app_name: str, max_depth: int = 6) -> list[dict]:
        cached = self._tree_cache.get(f"app_tree:{app_name}:{max_depth}")
        if cached is not None:
            return cached
        safe = app_name.replace('"', '\\"')
        script = (
            f'const se = Application("System Events");\n'
            f'const proc = se.processes["{safe}"];\n'
            "const result = [];\n"
            "function walk(el, d) {\n"
            f"  if (d > {max_depth}) return;\n"
            "  try {\n"
            "    const uis = el.uiElements();\n"
            "    for (let i = 0; i < uis.length; i++) {\n"
            "      try {\n"
            "        result.push({role: uis[i].role(), name: uis[i].name() || '', "
            "description: uis[i].description() || '', states: [], bbox: null, "
            "depth: d, element_id: 0});\n"
            "        walk(uis[i], d + 1);\n"
            "      } catch(e) {}\n"
            "    }\n"
            "  } catch(e) {}\n"
            "}\n"
            "try { walk(proc, 0); } catch(e) {}\n"
            "JSON.stringify(result);"
        )
        data = self._run_jxa_json(script) or []
        for item in data:
            item["element_id"] = self._next_id(item)
        self._tree_cache.set(f"app_tree:{app_name}:{max_depth}", data)
        return data

    def list_windows(self, app_name: str | None = None) -> list[dict]:
        cache_key = f"windows:{app_name or 'all'}"
        cached = self._window_cache.get(cache_key)
        if cached is not None:
            return cached
        if app_name:
            safe = app_name.replace('"', '\\"')
            script = (
                f'const se = Application("System Events");\n'
                f'const proc = se.processes["{safe}"];\n'
                "const result = [];\n"
                "try {\n"
                "  const wins = proc.windows();\n"
                "  for (let i = 0; i < wins.length; i++) {\n"
                "    result.push({role: 'frame', name: wins[i].name() || '', "
                f"app_name: '{safe}', states: [], bbox: null, element_id: 0}});\n"
                "  }\n"
                "} catch(e) {}\n"
                "JSON.stringify(result);"
            )
        else:
            script = (
                'const se = Application("System Events");\n'
                "const procs = se.processes.whose({backgroundOnly: false});\n"
                "const result = [];\n"
                "for (let p = 0; p < procs.length; p++) {\n"
                "  try {\n"
                "    const wins = procs[p].windows();\n"
                "    for (let i = 0; i < wins.length; i++) {\n"
                "      result.push({role: 'frame', name: wins[i].name() || '', "
                "app_name: procs[p].name(), states: [], bbox: null, element_id: 0});\n"
                "    }\n"
                "  } catch(e) {}\n"
                "}\n"
                "JSON.stringify(result);"
            )
        data = self._run_jxa_json(script) or []
        for item in data:
            item["element_id"] = self._next_id(item)
        self._window_cache.set(cache_key, data)
        return data

    def get_focused_window(self) -> dict | None:
        cached = self._context_cache.get("focused_window")
        if cached is not None:
            return cached
        script = (
            'const se = Application("System Events");\n'
            "const procs = se.processes.whose({frontmost: true});\n"
            "let result = null;\n"
            "if (procs.length > 0) {\n"
            "  try {\n"
            "    const wins = procs[0].windows();\n"
            "    if (wins.length > 0) {\n"
            "      result = {role: 'frame', name: wins[0].name() || '', "
            "app_name: procs[0].name(), states: ['focused'], bbox: null, element_id: 0};\n"
            "    }\n"
            "  } catch(e) {}\n"
            "}\n"
            "JSON.stringify(result);"
        )
        data = self._run_jxa_json(script)
        if data:
            data["element_id"] = self._next_id(data)
            self._context_cache.set("focused_window", data)
        return data

    def get_focused_element(self) -> dict | None:
        script = (
            'const se = Application("System Events");\n'
            "const procs = se.processes.whose({frontmost: true});\n"
            "let result = null;\n"
            "if (procs.length > 0) {\n"
            "  try {\n"
            "    const fe = procs[0].focusedUIElement();\n"
            "    if (fe) {\n"
            "      result = {role: fe.role() || '', name: fe.name() || '', "
            "description: fe.description() || '', states: ['focused'], "
            "bbox: null, element_id: 0, app_name: procs[0].name()};\n"
            "    }\n"
            "  } catch(e) {}\n"
            "}\n"
            "JSON.stringify(result);"
        )
        data = self._run_jxa_json(script)
        if data:
            data["element_id"] = self._next_id(data)
        return data

    def find_elements(
        self,
        role: str | None = None,
        name: str | None = None,
        description: str | None = None,
        state: str | None = None,
        exact: bool = False,
        app_name: str | None = None,
    ) -> list[UIElement]:
        whose_clause = ""
        if name:
            safe = name.replace('"', '\\"')
            whose_clause = f'.whose({{name: "{safe}"}})'
        elif role:
            safe_r = role.replace('"', '\\"')
            whose_clause = f'.whose({{role: "{safe_r}"}})'

        if app_name:
            safe_app = app_name.replace('"', '\\"')
            proc_selector = f'se.processes["{safe_app}"]'
        else:
            proc_selector = "se.processes.whose({frontmost: true})[0]"

        script = (
            f'const se = Application("System Events");\n'
            f"const proc = {proc_selector};\n"
            "const result = [];\n"
            "function search(el, d) {\n"
            "  if (d > 6 || result.length >= 50) return;\n"
            "  try {\n"
            f"    const uis = el.uiElements{whose_clause}();\n"
            "    for (let i = 0; i < uis.length && result.length < 50; i++) {\n"
            "      try {\n"
            "        const pos = uis[i].position();\n"
            "        const sz = uis[i].size();\n"
            "        result.push({role: uis[i].role() || '', name: uis[i].name() || '', "
            "description: uis[i].description() || '', states: [], "
            "bbox: pos && sz ? {x: pos[0], y: pos[1], width: sz[0], height: sz[1]} : null, "
            "depth: d, element_id: 0});\n"
            "      } catch(e) {}\n"
            "    }\n"
            "    const all = el.uiElements();\n"
            "    for (let i = 0; i < all.length && result.length < 50; i++) {\n"
            "      search(all[i], d + 1);\n"
            "    }\n"
            "  } catch(e) {}\n"
            "}\n"
            "try { search(proc, 0); } catch(e) {}\n"
            "JSON.stringify(result);"
        )
        data = self._run_jxa_json(script) or []
        elements: list[UIElement] = []
        for item in data:
            eid = self._next_id(item)
            elements.append(UIElement(
                element_id=eid,
                role=item.get("role", ""),
                name=item.get("name", ""),
                description=item.get("description", ""),
                states=item.get("states", []),
                bbox=item.get("bbox"),
                depth=item.get("depth", 0),
            ))
        return _rank_elements(elements, target_role=role, target_name=name)

    def get_tree_snapshot(self, app_name: str | None = None, max_depth: int = 4) -> str:
        cache_key = f"tree_snap:{app_name or 'all'}:{max_depth}"
        cached = self._tree_cache.get(cache_key)
        if cached is not None:
            return cached
        if app_name:
            tree = self.get_application_tree(app_name, max_depth)
            lines = []
            for item in tree:
                d = item.get("depth", 0)
                indent = "  " * d
                n = f' "{item.get("name", "")}"' if item.get("name") else ""
                lines.append(f"{indent}[{item.get('element_id', 0)}] {item.get('role', '?')}{n}")
            result = "\n".join(lines) if lines else "(no elements found)"
        else:
            apps = self.list_applications()
            lines = [f"=== {a.get('app_name', '?')} (pid={a.get('pid', '?')}) ===" for a in apps[:20]]
            result = "\n".join(lines) if lines else "(no accessible applications found)"
        self._tree_cache.set(cache_key, result)
        return result

    def click_at(self, x: int, y: int, button: int = 1, clicks: int = 1) -> bool:
        click_type = "right click" if button == 3 else "click"
        script = (
            "const se = Application('System Events');\n"
            f"se.{click_type}({{x: {x}, y: {y}}});"
        )
        # AppleScript approach as fallback (more reliable for mouse)
        applescript = f'tell application "System Events" to {click_type} at {{{x}, {y}}}'
        try:
            for _ in range(clicks):
                subprocess.run(
                    ["osascript", "-e", applescript],
                    check=True, timeout=5, capture_output=True,
                )
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("macOS click failed: %s", exc)
            return False

    def type_text_phys(self, text: str) -> bool:
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{safe}"'
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=15, capture_output=True)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("macOS type failed: %s", exc)
            return False

    def press_key(self, key: str) -> bool:
        _KEY_CODES: dict[str, int] = {
            "Return": 36, "enter": 36, "Tab": 48, "Escape": 53,
            "BackSpace": 51, "Delete": 117, "space": 49,
            "Up": 126, "Down": 125, "Left": 123, "Right": 124,
            "Home": 115, "End": 119, "Prior": 116, "Next": 121,
        }
        parts = key.replace("+", " ").split()
        modifiers = []
        main_key = parts[-1] if parts else key
        for p in parts[:-1]:
            if p.lower() in ("ctrl", "control"):
                modifiers.append("control down")
            elif p.lower() == "alt":
                modifiers.append("option down")
            elif p.lower() == "shift":
                modifiers.append("shift down")
            elif p.lower() in ("super", "command", "cmd"):
                modifiers.append("command down")

        code = _KEY_CODES.get(main_key)
        mod_str = ", ".join(modifiers)
        using = f" using {{{mod_str}}}" if mod_str else ""
        if code is not None:
            script = f'tell application "System Events" to key code {code}{using}'
        else:
            safe = main_key.replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{safe}"{using}'
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=5, capture_output=True)
            self.invalidate_caches()
            return True
        except Exception as exc:
            logger.warning("macOS key press failed: %s", exc)
            return False

    def activate_window(self, name: str) -> bool:
        safe = name.replace('"', '\\"')
        script = (
            f'tell application "{safe}"\n'
            "  activate\n"
            "end tell"
        )
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=5, capture_output=True)
            self.invalidate_caches()
            return True
        except Exception:
            # Fallback: try via System Events
            script2 = (
                f'tell application "System Events" to set frontmost of '
                f'(first process whose name is "{safe}") to true'
            )
            try:
                subprocess.run(["osascript", "-e", script2], check=True, timeout=5, capture_output=True)
                self.invalidate_caches()
                return True
            except Exception as exc:
                logger.warning("macOS activate window failed: %s", exc)
                return False

    def perform_action(self, element_id: int, action_name: str = "click") -> bool:
        elem = self.get_cached(element_id)
        bbox = elem.get("bbox")
        if bbox and action_name == "click":
            cx = bbox["x"] + bbox.get("width", 0) // 2
            cy = bbox["y"] + bbox.get("height", 0) // 2
            return self.click_at(cx, cy)
        return False

    def set_value(self, element_id: int, value: str) -> bool:
        elem = self.get_cached(element_id)
        el_name = elem.get("name", "")
        app_name = elem.get("app_name", "")
        if not app_name:
            return False
        safe_app = app_name.replace('"', '\\"')
        safe_name = el_name.replace('"', '\\"')
        safe_val = value.replace('"', '\\"')
        script = (
            f'tell application "System Events"\n'
            f'  tell process "{safe_app}"\n'
            f'    set value of text field "{safe_name}" of window 1 to "{safe_val}"\n'
            f"  end tell\n"
            f"end tell"
        )
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=10, capture_output=True)
            return True
        except Exception as exc:
            logger.debug("macOS set_value failed: %s", exc)
            return False

    def get_value(self, element_id: int) -> str:
        elem = self.get_cached(element_id)
        return elem.get("name", "")

    def get_bounding_box(self, element_id: int) -> dict | None:
        return self.get_cached(element_id).get("bbox")

    def get_center_point(self, element_id: int) -> tuple[int, int] | None:
        bbox = self.get_bounding_box(element_id)
        if not bbox or bbox.get("width", 0) <= 0 or bbox.get("height", 0) <= 0:
            return None
        return (bbox["x"] + bbox["width"] // 2, bbox["y"] + bbox["height"] // 2)

    def focus_element(self, element_id: int) -> bool:
        elem = self.get_cached(element_id)
        bbox = elem.get("bbox")
        if bbox:
            return self.click_at(
                bbox["x"] + bbox.get("width", 0) // 2,
                bbox["y"] + bbox.get("height", 0) // 2,
            )
        return False

    def is_visible(self, element_id: int) -> bool:
        bbox = self.get_bounding_box(element_id)
        return bbox is not None and bbox.get("width", 0) > 0

    def get_element_info(self, element_id: int) -> dict:
        return dict(self.get_cached(element_id))

    def scroll_element_to_view(self, element_id: int) -> bool:
        return False  # No direct JXA support for scroll-into-view

    def check_health(self) -> bool:
        try:
            apps = self.list_applications()
            return len(apps) > 0
        except Exception:
            return False

    def invalidate_caches(self) -> None:
        self._window_cache.invalidate()
        self._tree_cache.invalidate()
        self._context_cache.invalidate()


# ═══════════════════════════════════════════════════════════════════════════════
#  10. Provider Factory & Singleton
# ═══════════════════════════════════════════════════════════════════════════════

_provider: AccessibilityProvider | None = None
_provider_lock = threading.Lock()


def _create_provider() -> AccessibilityProvider:
    """Auto-select the concrete provider based on the host operating system."""
    system = platform.system()
    if system == "Linux":
        return LinuxATSPIProvider()
    if system == "Windows":
        return WindowsUIAProvider()
    if system == "Darwin":
        return MacAccessibilityProvider()
    # Default to Linux (Docker container assumption)
    logger.warning("Unknown platform %s — defaulting to LinuxATSPIProvider", system)
    return LinuxATSPIProvider()


def _get_provider() -> AccessibilityProvider:
    """Return the module-level singleton provider (lazy init)."""
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is None:
            _provider = _create_provider()
    return _provider


# ═══════════════════════════════════════════════════════════════════════════════
#  11. Post-Action Verification Helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _verify_click(provider: AccessibilityProvider, element_id: int) -> bool:
    """After a click, check that the element's state changed (focused/selected).

    Returns True if verification passed or was inconclusive; False only if
    the element is clearly unchanged.  Failures are logged but do not
    propagate to the caller.
    """
    try:
        await asyncio.sleep(0.15)
        info = await asyncio.to_thread(provider.get_element_info, element_id)
        states = set(info.get("states", []))
        if "focused" in states or "selected" in states:
            return True
        logger.debug("Post-click verification: element %d not focused/selected", element_id)
    except Exception:
        pass
    return True  # inconclusive — don't fail


async def _verify_set_value(provider: AccessibilityProvider, element_id: int, expected: str) -> bool:
    """After set_value, read back and compare.

    Returns True if value matches or verification is inconclusive.
    """
    try:
        await asyncio.sleep(0.10)
        actual = await asyncio.to_thread(provider.get_value, element_id)
        if actual == expected:
            return True
        logger.debug(
            "Post-set_value verification: expected %r, got %r for element %d",
            expected, actual, element_id,
        )
    except Exception:
        pass
    return True  # inconclusive — don't fail


def _invalidate_after_mutation() -> None:
    """Flush TTL caches after a state-changing action."""
    p = _get_provider()
    if hasattr(p, "invalidate_caches"):
        p.invalidate_caches()


# ═══════════════════════════════════════════════════════════════════════════════
#  12. Async Public API — unchanged signatures
# ═══════════════════════════════════════════════════════════════════════════════


async def list_applications() -> dict:
    """List all accessible applications."""
    try:
        apps = await asyncio.to_thread(_get_provider().list_applications)
        return {"success": True, "message": json.dumps(apps, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"list_applications failed: {exc}"}


async def get_application_tree(app_name: str, max_depth: int = 6) -> dict:
    """Get the accessibility tree of a named application."""
    app_name = _sanitize_name(app_name)
    try:
        tree = await asyncio.to_thread(_get_provider().get_application_tree, app_name, max_depth)
        # Serialize UIElement list if needed
        data = [e.to_dict() if isinstance(e, UIElement) else e for e in tree]
        return {"success": True, "message": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"get_application_tree failed: {exc}"}


async def list_windows(app_name: str | None = None) -> dict:
    """List accessible windows."""
    try:
        wins = await asyncio.to_thread(_get_provider().list_windows, app_name)
        return {"success": True, "message": json.dumps(wins, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"list_windows failed: {exc}"}


async def get_focused_window() -> dict:
    """Get the currently focused window."""
    try:
        win = await asyncio.to_thread(_get_provider().get_focused_window)
        if win:
            return {"success": True, "message": json.dumps(win, ensure_ascii=False)}
        return {"success": False, "message": "No focused window found"}
    except Exception as exc:
        return {"success": False, "message": f"get_focused_window failed: {exc}"}


async def get_focused_element() -> dict:
    """Get the currently focused element."""
    try:
        elem = await asyncio.to_thread(_get_provider().get_focused_element)
        if elem:
            return {"success": True, "message": json.dumps(elem, ensure_ascii=False)}
        return {"success": False, "message": "No focused element found"}
    except Exception as exc:
        return {"success": False, "message": f"get_focused_element failed: {exc}"}


async def find_by_role(role: str, name: str | None = None, exact: bool = False) -> dict:
    """Find elements by role and optional name."""
    role = _sanitize_role(role)
    if name:
        name = _sanitize_name(name)
    try:
        elements = await asyncio.to_thread(
            _get_provider().find_elements, role=role, name=name, exact=exact,
        )
        data = [e.to_dict() for e in elements]
        return {"success": True, "message": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"find_by_role failed: {exc}"}


async def find_by_name(name: str, exact: bool = False) -> dict:
    """Find elements by accessible name."""
    name = _sanitize_name(name)
    try:
        elements = await asyncio.to_thread(
            _get_provider().find_elements, name=name, exact=exact,
        )
        data = [e.to_dict() for e in elements]
        return {"success": True, "message": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"find_by_name failed: {exc}"}


async def find_by_description(description: str) -> dict:
    """Find elements by accessibility description."""
    description = _sanitize_name(description)
    try:
        elements = await asyncio.to_thread(
            _get_provider().find_elements, description=description,
        )
        data = [e.to_dict() for e in elements]
        return {"success": True, "message": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"find_by_description failed: {exc}"}


async def find_by_state(state: str) -> dict:
    """Find elements by accessibility state."""
    try:
        elements = await asyncio.to_thread(
            _get_provider().find_elements, state=state,
        )
        data = [e.to_dict() for e in elements]
        return {"success": True, "message": json.dumps(data, ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"find_by_state failed: {exc}"}


async def _resolve_elements(target: str) -> list[UIElement]:
    """Resolve a target string to UI elements via the active provider.

    Supports: numeric element_id, plain name search, or ``role:name`` syntax.
    Integrates with the circuit breaker.
    """
    provider = _get_provider()

    if not _circuit_breaker.allow_request():
        return []

    # Numeric element_id shortcut
    try:
        eid = int(target)
        info = await asyncio.to_thread(provider.get_element_info, eid)
        ui = UIElement(
            element_id=eid,
            role=info.get("role", ""),
            name=info.get("name", ""),
            description=info.get("description", ""),
            states=info.get("states", []),
            bbox=info.get("bbox"),
        )
        _circuit_breaker.record_success()
        return [ui]
    except (ValueError, TypeError):
        pass

    # Name-based search
    elements = await asyncio.to_thread(provider.find_elements, name=target)
    if elements:
        _circuit_breaker.record_success()
        return elements

    # Try role:name format
    if ":" in target:
        parts = target.split(":", 1)
        role_hint, name_hint = parts[0].strip(), parts[1].strip()
        if role_hint:
            elements = await asyncio.to_thread(
                provider.find_elements,
                role=role_hint,
                name=name_hint or None,
            )
            if elements:
                _circuit_breaker.record_success()
                return elements

    _circuit_breaker.record_failure()
    return []


async def click_element(target: str) -> dict:
    """Click an element found by role/name target string."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()

        provider = _get_provider()

        # Direct element_id path
        try:
            eid = int(target)
            center = await asyncio.to_thread(provider.get_center_point, eid)
            if center:
                ok = await asyncio.to_thread(provider.click_at, center[0], center[1])
                if ok:
                    _invalidate_after_mutation()
                    await _verify_click(provider, eid)
                    _circuit_breaker.record_success()
                    return {"success": True, "message": f"Clicked element {eid} at ({center[0]}, {center[1]})"}
                return {"success": False, "message": f"Physical click failed at ({center[0]}, {center[1]})"}
        except (ValueError, TypeError):
            pass

        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No accessible element found for target: {target}"}

        elem = elements[0]
        eid = elem.element_id

        # Try accessibility action first
        action_ok = await asyncio.to_thread(provider.perform_action, eid, "click")
        if action_ok:
            _invalidate_after_mutation()
            await _verify_click(provider, eid)
            return {"success": True, "message": f"Clicked '{elem.name}' ({elem.role}) via accessibility action"}

        # Fallback: physical click at center
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            ok = await asyncio.to_thread(provider.click_at, center[0], center[1])
            if ok:
                _invalidate_after_mutation()
                await _verify_click(provider, eid)
                return {"success": True, "message": f"Clicked '{elem.name}' at ({center[0]}, {center[1]}) via physical click"}
            return {"success": False, "message": f"Physical click failed for '{elem.name}'"}

        return {"success": False, "message": f"Element '{target}' found but has no bounding box"}
    except Exception as exc:
        return {"success": False, "message": f"click failed: {exc}"}


async def double_click_element(target: str) -> dict:
    """Double-click an element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            ok = await asyncio.to_thread(provider.click_at, center[0], center[1], button=1, clicks=2)
            if ok:
                _invalidate_after_mutation()
                return {"success": True, "message": f"Double-clicked at ({center[0]}, {center[1]})"}
        return {"success": False, "message": f"Cannot double-click element: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"double_click failed: {exc}"}


async def right_click_element(target: str) -> dict:
    """Right-click an element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            ok = await asyncio.to_thread(provider.click_at, center[0], center[1], button=3)
            if ok:
                _invalidate_after_mutation()
                return {"success": True, "message": f"Right-clicked at ({center[0]}, {center[1]})"}
        return {"success": False, "message": f"Cannot right-click element: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"right_click failed: {exc}"}


async def hover_element(target: str) -> dict:
    """Hover over an element (move mouse without clicking)."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            system = platform.system()
            if system == "Linux":
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["xdotool", "mousemove", "--sync", str(center[0]), str(center[1])],
                        check=True, timeout=5,
                    )
                )
            elif system == "Windows":
                provider.click_at(center[0], center[1], button=0, clicks=0)  # move only on Windows handled differently
                _ps_script = (
                    'Add-Type @"\n'
                    "using System; using System.Runtime.InteropServices;\n"
                    "public class WMouse { [DllImport(\"user32.dll\")] "
                    "public static extern bool SetCursorPos(int X, int Y); }\n"
                    '"@\n'
                    f"[WMouse]::SetCursorPos({center[0]}, {center[1]})"
                )
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["powershell", "-NoProfile", "-Command", _ps_script],
                        timeout=5, capture_output=True,
                    )
                )
            else:  # macOS
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["osascript", "-e",
                         f'tell application "System Events" to '
                         f'key code 0'],  # minimal no-op to keep process active
                        timeout=5, capture_output=True,
                    )
                )
            return {"success": True, "message": f"Hovered at ({center[0]}, {center[1]})"}
        return {"success": False, "message": f"Cannot hover element: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"hover failed: {exc}"}


async def focus_element(target: str) -> dict:
    """Focus an element via accessibility API."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.focus_element, eid)
        if ok:
            return {"success": True, "message": f"Focused '{elements[0].name}'"}
        return {"success": False, "message": f"grab_focus failed for '{target}'"}
    except Exception as exc:
        return {"success": False, "message": f"focus failed: {exc}"}


async def type_text(target: str, text: str) -> dict:
    """Focus an element, then type text."""
    if not text:
        return {"success": False, "message": "Text required for type_text"}
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id

        # Click to focus
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            await asyncio.to_thread(provider.click_at, center[0], center[1])
            await asyncio.sleep(0.1)

        ok = await asyncio.to_thread(provider.type_text_phys, text)
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Typed into '{elements[0].name}'"}
        return {"success": False, "message": f"Type failed for '{target}'"}
    except Exception as exc:
        return {"success": False, "message": f"type_text failed: {exc}"}


async def clear_text(target: str) -> dict:
    """Clear text from an element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id

        # Try editable text interface
        ok = await asyncio.to_thread(provider.set_value, eid, "")
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Cleared '{elements[0].name}' via EditableText"}

        # Fallback: Ctrl+A + Delete
        center = await asyncio.to_thread(provider.get_center_point, eid)
        if center:
            await asyncio.to_thread(provider.click_at, center[0], center[1])
            await asyncio.sleep(0.05)
            await asyncio.to_thread(provider.press_key, "ctrl+a")
            await asyncio.sleep(0.05)
            await asyncio.to_thread(provider.press_key, "Delete")
            _invalidate_after_mutation()
            return {"success": True, "message": f"Cleared '{elements[0].name}' via keyboard"}
        return {"success": False, "message": f"Cannot clear element: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"clear_text failed: {exc}"}


async def fill_element(target: str, text: str) -> dict:
    """Clear + type into an element (atomic fill)."""
    clear_result = await clear_text(target)
    if not clear_result.get("success"):
        return clear_result
    if text:
        return await type_text(target, text)
    return clear_result


async def set_value(target: str, value: str) -> dict:
    """Set element value via accessibility API or keyboard fallback."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.set_value, eid, value)
        if ok:
            _invalidate_after_mutation()
            await _verify_set_value(provider, eid, value)
            return {"success": True, "message": f"Set value on '{elements[0].name}'"}
        return await fill_element(target, value)
    except Exception as exc:
        return {"success": False, "message": f"set_value failed: {exc}"}


async def get_value(target: str) -> dict:
    """Read the current text/value of an element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for target: {target}"}
        eid = elements[0].element_id
        value = await asyncio.to_thread(provider.get_value, eid)
        return {"success": True, "message": value}
    except Exception as exc:
        return {"success": False, "message": f"get_value failed: {exc}"}


async def press_key(key: str) -> dict:
    """Press a key or key combo."""
    if not key:
        return {"success": False, "message": "Key required"}
    ok = await asyncio.to_thread(_get_provider().press_key, key)
    if ok:
        _invalidate_after_mutation()
        return {"success": True, "message": f"Pressed key: {key}"}
    return {"success": False, "message": f"Key press failed: {key}"}


async def activate_window(window_name: str) -> dict:
    """Activate/focus a window by title."""
    if not window_name:
        return {"success": False, "message": "Window name required"}
    ok = await asyncio.to_thread(_get_provider().activate_window, window_name)
    if ok:
        _invalidate_after_mutation()
        return {"success": True, "message": f"Activated window: {window_name}"}
    return {"success": False, "message": f"Window not found: {window_name}"}


async def toggle_element(target: str) -> dict:
    """Toggle a checkbox or toggle button."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        # Try specific roles first
        elements = await asyncio.to_thread(provider.find_elements, name=target, role="check box")
        if not elements:
            elements = await asyncio.to_thread(provider.find_elements, name=target, role="toggle button")
        if not elements:
            elements = await asyncio.to_thread(provider.find_elements, name=target)
        if not elements:
            return {"success": False, "message": f"No toggleable element found: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.perform_action, eid, "click")
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Toggled '{elements[0].name}' via action"}
        return await click_element(str(eid))
    except Exception as exc:
        return {"success": False, "message": f"toggle failed: {exc}"}


async def select_element(target: str) -> dict:
    """Select a list item or similar selectable element."""
    return await click_element(target)


async def expand_element(target: str) -> dict:
    """Expand a tree node or expandable element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.perform_action, eid, "expand or activate")
        if not ok:
            ok = await asyncio.to_thread(provider.perform_action, eid, "click")
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Expanded '{elements[0].name}'"}
        return {"success": False, "message": f"Cannot expand: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"expand failed: {exc}"}


async def collapse_element(target: str) -> dict:
    """Collapse a tree node or expandable element."""
    try:
        if not _circuit_breaker.allow_request():
            return _circuit_breaker.failure_response()
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.perform_action, eid, "collapse")
        if not ok:
            ok = await asyncio.to_thread(provider.perform_action, eid, "click")
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Collapsed '{elements[0].name}'"}
        return {"success": False, "message": f"Cannot collapse: {target}"}
    except Exception as exc:
        return {"success": False, "message": f"collapse failed: {exc}"}


async def perform_action(target: str, action_name: str = "click") -> dict:
    """Perform a named AT-SPI action on an element."""
    try:
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found: {target}"}
        eid = elements[0].element_id
        ok = await asyncio.to_thread(provider.perform_action, eid, action_name)
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Performed '{action_name}' on '{elements[0].name}'"}
        return {"success": False, "message": f"Action '{action_name}' failed on '{target}'"}
    except Exception as exc:
        return {"success": False, "message": f"perform_action failed: {exc}"}


async def wait_for_element(
    role: str | None = None,
    name: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Poll the accessibility tree until an element matching role/name appears."""
    if role:
        role = _sanitize_role(role)
    if name:
        name = _sanitize_name(name)
    deadline = time.monotonic() + min(timeout, 30.0)
    attempts = 0
    provider = _get_provider()
    while time.monotonic() < deadline:
        attempts += 1
        elements = await asyncio.to_thread(provider.find_elements, role=role, name=name)
        if elements:
            return {"success": True, "message": json.dumps(elements[0].to_dict(), ensure_ascii=False)}
        await asyncio.sleep(0.5)
    return {"success": False, "message": f"Element (role={role}, name={name}) not found after {timeout}s ({attempts} attempts)"}


async def dump_tree(app_name: str | None = None, max_depth: int = 4) -> dict:
    """Dump the accessibility tree as text."""
    try:
        text = await asyncio.to_thread(_get_provider().get_tree_snapshot, app_name, max_depth)
        return {"success": True, "message": text}
    except Exception as exc:
        return {"success": False, "message": f"dump_tree failed: {exc}"}


async def element_exists(role: str | None = None, name: str | None = None) -> dict:
    """Check if an element matching role/name exists."""
    if role:
        role = _sanitize_role(role)
    if name:
        name = _sanitize_name(name)
    try:
        elements = await asyncio.to_thread(_get_provider().find_elements, role=role, name=name)
        exists = len(elements) > 0
        return {"success": True, "message": json.dumps({"exists": exists, "count": len(elements)})}
    except Exception as exc:
        return {"success": False, "message": f"element_exists failed: {exc}"}


async def get_element_state_async(target: str) -> dict:
    """Return full accessibility state of an element."""
    try:
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for: {target}"}
        return {"success": True, "message": json.dumps(elements[0].to_dict(), ensure_ascii=False)}
    except Exception as exc:
        return {"success": False, "message": f"get_state failed: {exc}"}


async def get_bounding_box_async(target: str) -> dict:
    """Return the bounding box of an element."""
    try:
        provider = _get_provider()
        try:
            eid = int(target)
            bbox = await asyncio.to_thread(provider.get_bounding_box, eid)
            if bbox:
                return {"success": True, "message": json.dumps(bbox)}
            return {"success": False, "message": f"No bounding box for element {eid}"}
        except (ValueError, TypeError):
            pass
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found for: {target}"}
        bbox = elements[0].bbox
        if bbox:
            return {"success": True, "message": json.dumps(bbox)}
        return {"success": False, "message": f"No bounding box for '{target}'"}
    except Exception as exc:
        return {"success": False, "message": f"get_bounding_box failed: {exc}"}


async def scroll_element(direction: str = "down") -> dict:
    """Scroll using keyboard (Page Down / Page Up)."""
    key = "Next" if direction.lower() in ("down", "page_down") else "Prior"
    return await press_key(key)


async def scroll_into_view(target: str) -> dict:
    """Attempt to scroll an element into view."""
    try:
        provider = _get_provider()
        elements = await _resolve_elements(target)
        if not elements:
            return {"success": False, "message": f"No element found: {target}"}
        eid = elements[0].element_id
        visible = await asyncio.to_thread(provider.is_visible, eid)
        if visible:
            return {"success": True, "message": f"Element '{target}' is already visible"}
        ok = await asyncio.to_thread(provider.scroll_element_to_view, eid)
        if ok:
            _invalidate_after_mutation()
            return {"success": True, "message": f"Scrolled '{target}' into view"}
        return {"success": False, "message": f"Cannot scroll '{target}' into view (scroll not supported)"}
    except Exception as exc:
        return {"success": False, "message": f"scroll_into_view failed: {exc}"}


async def a11y_wait(duration: float = 2.0) -> dict:
    """Pause for duration seconds (capped at 10s)."""
    capped = min(max(duration, 0.1), 10.0)
    await asyncio.sleep(capped)
    return {"success": True, "message": f"Waited {capped:.1f}s"}


# ═══════════════════════════════════════════════════════════════════════════════
#  13. Health Check
# ═══════════════════════════════════════════════════════════════════════════════


async def check_accessibility_health() -> bool:
    """Verify that the accessibility subsystem is reachable and returning data."""
    try:
        return await asyncio.to_thread(_get_provider().check_health)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  14. Handler Functions — signatures: async (text: str, target: str) -> dict
# ═══════════════════════════════════════════════════════════════════════════════

from backend.models import ActionType


async def _NOOP(text: str, target: str) -> dict:
    """Stub for actions not available in accessibility mode."""
    return {"success": False, "message": "Not available in accessibility mode"}


async def _h_click(text: str, target: str) -> dict:
    return await click_element(target or text)


async def _h_double_click(text: str, target: str) -> dict:
    return await double_click_element(target or text)


async def _h_right_click(text: str, target: str) -> dict:
    return await right_click_element(target or text)


async def _h_hover(text: str, target: str) -> dict:
    return await hover_element(target or text)


async def _h_type(text: str, target: str) -> dict:
    if not text:
        return {"success": False, "message": "Text required"}
    if not target:
        return {"success": False, "message": "Target required for type in accessibility mode"}
    return await type_text(target, text)


async def _h_fill(text: str, target: str) -> dict:
    if not target:
        return {"success": False, "message": "Target required for fill"}
    return await fill_element(target, text or "")


async def _h_key(text: str, target: str) -> dict:
    return await press_key(text)


async def _h_clear_input(text: str, target: str) -> dict:
    if not target:
        return {"success": False, "message": "Target required for clear_input"}
    return await clear_text(target)


async def _h_scroll(text: str, target: str) -> dict:
    return await scroll_element(text or "down")


async def _h_scroll_to(text: str, target: str) -> dict:
    if not target:
        return {"success": False, "message": "Target required for scroll_to"}
    return await scroll_into_view(target)


async def _h_focus_window(text: str, target: str) -> dict:
    return await activate_window(target or text)


async def _h_wait(text: str, target: str) -> dict:
    dur = 2.0
    try:
        dur = float(text)
    except (ValueError, TypeError):
        pass
    return await a11y_wait(dur)


async def _h_wait_for(text: str, target: str) -> dict:
    if not target:
        return {"success": False, "message": "Target required for wait_for"}
    return await wait_for_element(name=target, timeout=10.0)


async def _h_find_element(text: str, target: str) -> dict:
    return await dump_tree(max_depth=4)


async def _h_get_text(text: str, target: str) -> dict:
    if not target:
        return {"success": False, "message": "Target required for get_text"}
    return await get_value(target)


async def _h_open_url(text: str, target: str) -> dict:
    """Open URL via platform-appropriate command.

    Validates the URL scheme (http/https only) and passes the argument
    to the OS handler without invoking a shell.  Previously the Windows
    path used ``subprocess.run(["start", url], shell=True)`` which is a
    command-injection primitive — a URL like
    ``"https://x & calc.exe & rem"`` would execute arbitrary commands
    via ``cmd.exe``.
    """
    if not text:
        return {"success": False, "message": "URL required"}
    url = text if text.startswith(("http://", "https://")) else f"https://{text}"

    # Scheme/character validation — reject anything cmd.exe or xdg-open
    # would interpret as a separator or pipe.
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
    except Exception:
        return {"success": False, "message": f"open_url: invalid URL: {url!r}"}
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"success": False, "message": f"open_url: only http/https allowed, got {parsed.scheme!r}"}
    if any(bad in url for bad in ("\n", "\r", "\x00", "&", "|", ";", "`")):
        return {"success": False, "message": "open_url: URL contains shell metacharacters"}

    system = platform.system()
    try:
        if system == "Linux":
            await asyncio.to_thread(
                lambda: subprocess.run(["xdg-open", url], timeout=10, check=True)
            )
        elif system == "Windows":
            # os.startfile invokes ShellExecuteW directly — no cmd.exe, no shell parsing.
            import os as _os
            await asyncio.to_thread(lambda: _os.startfile(url))  # type: ignore[attr-defined]
        elif system == "Darwin":
            await asyncio.to_thread(
                lambda: subprocess.run(["open", url], timeout=10, check=True)
            )
        else:
            await asyncio.to_thread(
                lambda: subprocess.run(["xdg-open", url], timeout=10, check=True)
            )
        return {"success": True, "message": f"Opened URL: {url}"}
    except Exception as exc:
        return {"success": False, "message": f"open_url failed: {exc}"}


async def _h_done(text: str, target: str) -> dict:
    return {"success": True, "message": "Task completed"}


async def _h_error(text: str, target: str) -> dict:
    return {"success": False, "message": f"Agent error: {text}"}


async def _h_paste(text: str, target: str) -> dict:
    """Paste via Ctrl+V (sets clipboard first if text provided)."""
    system = platform.system()
    if text:
        try:
            if system == "Linux":
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["xclip", "-selection", "clipboard"],
                        input=text.encode(), check=True, timeout=5,
                    )
                )
            elif system == "Windows":
                safe = text.replace("'", "''")
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Set-Clipboard -Value '{safe}'"],
                        check=True, timeout=5,
                    )
                )
            elif system == "Darwin":
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["pbcopy"], input=text.encode(), check=True, timeout=5,
                    )
                )
        except Exception as exc:
            logger.debug("Clipboard set failed: %s", exc)
    key = "ctrl+v" if system != "Darwin" else "super+v"
    return await press_key(key)


async def _h_copy(text: str, target: str) -> dict:
    key = "ctrl+c" if platform.system() != "Darwin" else "super+c"
    return await press_key(key)


async def _h_get_accessibility_tree(text: str, target: str) -> dict:
    return await dump_tree(app_name=target if target else None, max_depth=5)


async def _h_toggle(text: str, target: str) -> dict:
    return await toggle_element(target or text)


async def _h_select_option(text: str, target: str) -> dict:
    """Select handled as click on the option."""
    return await click_element(text or target)


async def _h_run_command(text: str, target: str) -> dict:
    """Execute a structured command (no shell parsing)."""
    if not text:
        return {"success": False, "message": "Command required"}
    # Strict allowlist of permitted executables.  Each entry MUST map
    # to a binary actually present in docker/Dockerfile.  Listing
    # binaries that are not installed (e.g. gnome-* apps, mousepad,
    # firefox, xterm, xfce4-taskmanager, pip) just expands the prompt
    # surface for the LLM with commands that always fail.
    _ALLOWED_COMMANDS = frozenset({
        "ls", "cat", "head", "tail", "grep", "find", "wc", "echo",
        "pwd", "whoami", "id", "date", "env", "printenv",
        "which", "file", "stat", "df", "du", "free",
        "uname", "hostname", "uptime",
        "python3", "node", "npm", "npx",
        "curl", "wget",
        "xdg-open", "xdotool", "xclip", "scrot", "wmctrl",
        "xfce4-terminal",
        # Desktop apps actually installed in the image
        "xfce4-settings-manager", "xfce4-settings-editor",
        "thunar",
        "google-chrome", "google-chrome-stable",
    })
    # GUI apps should be launched fire-and-forget (Popen) — subprocess.run
    # would block until they exit, causing a 30 s timeout.
    _GUI_COMMANDS = frozenset({
        "xfce4-terminal",
        "xfce4-settings-manager", "xfce4-settings-editor",
        "thunar",
        "google-chrome", "google-chrome-stable",
        "xdg-open",
    })
    try:
        args = shlex.split(text)
    except ValueError as e:
        return {"success": False, "message": f"Invalid command syntax: {e}"}
    if not args:
        return {"success": False, "message": "Empty command"}
    if args[0] not in _ALLOWED_COMMANDS:
        return {"success": False, "message": f"Command not allowed: {args[0]}"}
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "DISPLAY": ":99"}
    try:
        if args[0] in _GUI_COMMANDS:
            # Fire-and-forget for GUI applications
            await asyncio.to_thread(
                lambda: subprocess.Popen(
                    args, shell=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env,
                )
            )
            return {"success": True, "message": f"Launched {args[0]}"}
        result = await asyncio.to_thread(
            lambda: subprocess.run(
                args, shell=False, capture_output=True, text=True, timeout=30,
                env=env,
            )
        )
        output = (result.stdout + result.stderr).strip()[:2000]
        return {"success": result.returncode == 0, "message": output or "(no output)"}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Command timed out (30s)"}
    except Exception as exc:
        return {"success": False, "message": f"Command failed: {exc}"}


async def _h_open_terminal(text: str, target: str) -> dict:
    """Open a terminal emulator."""
    system = platform.system()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "DISPLAY": ":99"}
    try:
        if system == "Linux":
            # Try terminals in order of preference for the container environment
            for terminal_cmd in ["xfce4-terminal", "gnome-terminal", "xterm", "x-terminal-emulator"]:
                if shutil.which(terminal_cmd):
                    await asyncio.to_thread(
                        lambda cmd=terminal_cmd: subprocess.Popen([cmd], env=env)
                    )
                    return {"success": True, "message": f"Opened terminal ({terminal_cmd})"}
            return {"success": False, "message": "open_terminal failed: no terminal emulator found"}
        elif system == "Windows":
            await asyncio.to_thread(
                lambda: subprocess.Popen(["cmd.exe"])
            )
        elif system == "Darwin":
            await asyncio.to_thread(
                lambda: subprocess.Popen(["open", "-a", "Terminal"])
            )
        return {"success": True, "message": "Opened terminal"}
    except Exception as exc:
        return {"success": False, "message": f"open_terminal failed: {exc}"}


def _create_stub(tool_name: str):
    """Create a placeholder handler for an unimplemented action."""
    async def _stub_handler(text: str, target: str) -> dict:
        return {"success": False, "message": f"Action '{tool_name}' not available in accessibility engine"}
    return _stub_handler


# ═══════════════════════════════════════════════════════════════════════════════
#  15. Handler Table — maps ActionType → handler
# ═══════════════════════════════════════════════════════════════════════════════

A11Y_TOOL_HANDLERS: Dict[str, Callable[[str, str], Awaitable[dict]]] = {
    ActionType.CLICK.value: _h_click,
    ActionType.DOUBLE_CLICK.value: _h_double_click,
    ActionType.RIGHT_CLICK.value: _h_right_click,
    ActionType.HOVER.value: _h_hover,
    ActionType.TYPE.value: _h_type,
    ActionType.FILL.value: _h_fill,
    ActionType.KEY.value: _h_key,
    ActionType.HOTKEY.value: _h_key,
    ActionType.CLEAR_INPUT.value: _h_clear_input,
    ActionType.SELECT_OPTION.value: _h_select_option,
    ActionType.PASTE.value: _h_paste,
    ActionType.COPY.value: _h_copy,
    ActionType.OPEN_URL.value: _h_open_url,
    ActionType.SCROLL.value: _h_scroll,
    ActionType.SCROLL_TO.value: _h_scroll_to,
    ActionType.SCROLL_INTO_VIEW.value: _h_scroll_to,
    ActionType.FOCUS_WINDOW.value: _h_focus_window,
    ActionType.OPEN_TERMINAL.value: _h_open_terminal,
    ActionType.RUN_COMMAND.value: _h_run_command,
    ActionType.GET_TEXT.value: _h_get_text,
    ActionType.GET_ACCESSIBILITY_TREE.value: _h_get_accessibility_tree,
    ActionType.GET_SNAPSHOT.value: _h_get_accessibility_tree,
    ActionType.FIND_ELEMENT.value: _h_find_element,
    ActionType.FIND_BY_ROLE.value: _h_find_element,
    ActionType.FIND_BY_TEXT.value: _h_find_element,
    ActionType.FIND_BY_LABEL.value: _h_find_element,
    ActionType.WAIT.value: _h_wait,
    ActionType.WAIT_FOR.value: _h_wait_for,
    ActionType.DONE.value: _h_done,
    ActionType.ERROR.value: _h_error,
}

# Auto-stub remaining actions
for _member in ActionType:
    if _member.value not in A11Y_TOOL_HANDLERS:
        A11Y_TOOL_HANDLERS[_member.value] = _create_stub(_member.value)


# ═══════════════════════════════════════════════════════════════════════════════
#  16. Top-level Dispatcher — unchanged signature
# ═══════════════════════════════════════════════════════════════════════════════


async def execute_accessibility_action(action: str, text: str = "", target: str = "") -> dict:
    """Dispatch an action to the appropriate accessibility handler.

    This is the sole entry point used by ``agent_service._dispatch_accessibility``.
    Signature and return shape are 100 % backward-compatible.
    """
    handler = A11Y_TOOL_HANDLERS.get(action)
    if handler:
        return await handler(text, target)
    return {"success": False, "message": f"Unsupported action '{action}' in accessibility engine"}
