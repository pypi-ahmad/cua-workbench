"""Pydantic models for agent actions, messages, and API contracts."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


# ── Action types ──────────────────────────────────────────────────────────────

class ActionType(str, enum.Enum):
    """All supported agent actions across every automation engine."""

    # ── Navigation (Playwright) ───────────────────────────────────────────
    OPEN_URL = "open_url"
    RELOAD = "reload"
    GO_BACK = "go_back"
    GO_FORWARD = "go_forward"
    
    # ── Tabs / Context ────────────────────────────────────────────────────
    NEW_TAB = "new_tab"
    CLOSE_TAB = "close_tab"
    SWITCH_TAB = "switch_tab"
    NEW_CONTEXT = "new_context"
    CLOSE_CONTEXT = "close_context"
    SWITCH_CONTEXT = "switch_context"

    # ── xdotool Navigation ────────────────────────────────────────────────
    OPEN_APP = "open_app"
    OPEN_TERMINAL = "open_terminal"
    RUN_COMMAND = "run_command"

    # ── Mouse / Interaction ───────────────────────────────────────────────
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MIDDLE_CLICK = "middle_click"
    HOVER = "hover"
    DRAG = "drag"
    
    # ── Playwright Specific (DOM-aware) ───────────────────────────────────
    CLICK_SELECTOR = "click_selector"
    HOVER_SELECTOR = "hover_selector"
    DRAG_SELECTOR = "drag_selector"

    # ── Low Level Mouse (xdotool) ─────────────────────────────────────
    MOUSE_MOVE = "mousemove"
    MOUSE_RELATIVE = "mouse_relative"
    MOUSEDOWN = "mousedown"
    MOUSEUP = "mouseup"

    # ── Keyboard / Input ──────────────────────────────────────────────────
    TYPE = "type"
    TYPE_AT = "type_at"
    FILL = "fill"
    KEY = "key"
    KEYDOWN = "keydown"
    KEYUP = "keyup"
    HOTKEY = "hotkey"
    PRESS_SEQUENTIAL = "press_sequential"
    CLEAR_INPUT = "clear_input"
    PASTE = "paste"
    COPY = "copy"
    TYPE_SLOW = "type_slow"
    PRESS = "press"  # Playwright specific alias for key
    SELECT_OPTION = "select_option"
    
    # ── Desktop Specific ──────────────────────────────────────────────────
    KEY_SEQUENCE = "key_sequence"
    COMBINED_INPUT = "combined_input"

    # ── Waiting ───────────────────────────────────────────────────────────
    WAIT = "wait"
    WAIT_FOR_SELECTOR = "wait_for_selector"
    WAIT_FOR_NAVIGATION = "wait_for_navigation"
    WAIT_FOR = "wait_for"

    # ── Scrolling & Viewport ──────────────────────────────────────────────
    SCROLL = "scroll"
    SCROLL_TO = "scroll_to"
    SCROLL_TO_SELECTOR = "scroll_to_selector"
    SCROLL_INTO_VIEW = "scroll_into_view"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    SET_VIEWPORT = "set_viewport"
    ZOOM = "zoom"

    # ── DOM / Accessibility Intelligence ──────────────────────────────────
    QUERY_SELECTOR = "query_selector"
    QUERY_ALL = "query_all"
    GET_TEXT = "get_text"
    GET_HTML = "get_html"
    GET_ATTRIBUTE = "get_attribute"
    GET_BOUNDING_BOX = "get_bounding_box"
    GET_VISIBLE_ELEMENTS = "get_visible_elements"
    
    # ── MCP Superpower (Accessibility) ────────────────────────────────────
    GET_ACCESSIBILITY_TREE = "get_accessibility_tree"
    GET_SNAPSHOT = "get_snapshot"
    FIND_BY_ROLE = "find_by_role"
    FIND_BY_TEXT = "find_by_text"
    FIND_BY_LABEL = "find_by_label"
    FIND_ELEMENT = "find_element" # Generic finder

    # ── Screenshot & Vision ───────────────────────────────────────────────
    SCREENSHOT = "screenshot"
    SCREENSHOT_FULL = "screenshot_full"
    SCREENSHOT_VIEWPORT = "screenshot_viewport"
    SCREENSHOT_ELEMENT = "screenshot_element"
    SCREENSHOT_REGION = "screenshot_region"
    CLICK_COORDINATES = "click_coordinates"
    DRAG_COORDINATES = "drag_coordinates"

    # ── JavaScript Execution ──────────────────────────────────────────────
    EVALUATE_JS = "evaluate_js"
    EVALUATE_ON_SELECTOR = "evaluate_on_selector"
    EVALUATE = "evaluate"
    EVALUATE_ON = "evaluate_on"

    # ── File & Upload Handling ────────────────────────────────────────────
    UPLOAD_FILE = "upload_file"
    DOWNLOAD_FILE = "download_file"
    HANDLE_FILE_CHOOSER = "handle_file_chooser"

    # ── Auth & Session ────────────────────────────────────────────────────
    SET_COOKIES = "set_cookies"
    GET_COOKIES = "get_cookies"
    CLEAR_COOKIES = "clear_cookies"
    STORAGE_STATE = "storage_state"
    GET_CURRENT_URL = "get_current_url"
    GET_PAGE_TITLE = "get_page_title"

    # ── Network & Debugging ───────────────────────────────────────────────
    INTERCEPT_REQUEST = "intercept_request"
    BLOCK_RESOURCE = "block_resource"
    MONITOR_REQUESTS = "monitor_requests"
    GET_RESPONSE_BODY = "get_response_body"
    GET_CONSOLE_LOGS = "get_console_logs"
    MONITOR_CONSOLE = "monitor_console"

    # ── Extraction & Scraping ─────────────────────────────────────────────
    GET_ALL_TEXT = "get_all_text"
    GET_LINKS = "get_links"
    EXTRACT_DATA = "extract_data"
    SCRAPE_PAGE = "scrape_page"

    # ── PDF & Document ────────────────────────────────────────────────────
    GENERATE_PDF = "generate_pdf"
    EXPORT_PAGE_PDF = "export_page_pdf"

    # ── Window & System Control (xdotool) ─────────────────────────────────
    SEARCH_WINDOW = "search_window"
    WINDOW_ACTIVATE = "window_activate"
    WINDOW_FOCUS = "window_focus" # Alias for activate/focus
    WINDOW_CLOSE = "close_window" # Mapped to close_window in agent_service
    WINDOW_MINIMIZE = "window_minimize"
    WINDOW_MAXIMIZE = "window_maximize"
    WINDOW_MOVE = "window_move"
    WINDOW_RESIZE = "window_resize"
    
    # ── Focus & Context Control ───────────────────────────────────────────
    FOCUS_WINDOW = "focus_window"
    FOCUS_CLICK = "focus_click"
    FOCUS_MOUSE = "focus_mouse"

    # ── Agent Meta Tools ──────────────────────────────────────────────────
    ASSERT_ELEMENT_PRESENT = "assert_element_present"
    VERIFY_TEXT = "verify_text"
    RETRY_LAST_ACTION = "retry_last_action"
    FALLBACK_STRATEGY = "fallback_strategy"
    LIST_TOOLS = "list_tools"

    # ── Desktop Reliability Wrappers ──────────────────────────────────────
    FOCUS_AND_TYPE = "focus_and_type"
    SAFE_TYPE = "safe_type"
    RETRY_CLICK = "retry_click"
    VERIFY_INPUT = "verify_input"
    PASTE_FALLBACK = "paste_fallback"

    # ── Computer-Use Native Actions (Gemini / Claude CU protocol) ─────────
    CLICK_AT = "click_at"
    HOVER_AT = "hover_at"
    TYPE_TEXT_AT = "type_text_at"
    SCROLL_AT = "scroll_at"
    DRAG_AND_DROP = "drag_and_drop"
    KEY_COMBINATION = "key_combination"
    NAVIGATE = "navigate"
    OPEN_WEB_BROWSER = "open_web_browser"
    SCROLL_DOCUMENT = "scroll_document"
    SEARCH = "search"
    WAIT_5_SECONDS = "wait_5_seconds"

    # ── Accessibility Engine (AT-SPI / UIA) ───────────────────────────────
    GET_ROLE = "get_role"
    GET_STATE = "get_state"
    GET_ATTRIBUTES = "get_attributes"
    QUERY_TEXT = "query_text"
    QUERY_VALUE = "query_value"
    QUERY_COMPONENT = "query_component"
    INVOKE_ACTION = "invoke_action"
    SUBSCRIBE_EVENT = "subscribe_event"
    UNSUBSCRIBE_EVENT = "unsubscribe_event"

    # ── Terminal / Control ────────────────────────────────────────────────
    DONE = "done"
    ERROR = "error"


class AgentMode(str, enum.Enum):
    """Agent operating mode — browser or full desktop."""

    BROWSER = "browser"
    DESKTOP = "desktop"


class AutomationEngine(str, enum.Enum):
    """Supported automation engines for action execution."""

    PLAYWRIGHT_MCP = "playwright_mcp"
    OMNI_ACCESSIBILITY = "omni_accessibility"
    COMPUTER_USE = "computer_use"


class AgentAction(BaseModel):
    """Structured action returned by Gemini."""
    action: ActionType
    target: Optional[str] = None
    coordinates: Optional[list[int]] = Field(default=None, max_length=4)
    text: Optional[str] = None
    reasoning: Optional[str] = None


class TaskState(BaseModel):
    """Structured per-task state for tracking collected results.

    Prevents data-re-collection loops by explicitly recording what the
    agent has already obtained.  When ``complete`` is ``True`` the agent
    loop must short-circuit to a ``done`` action rather than executing
    further steps.
    """

    results: list[str] = Field(default_factory=list)
    step: int = 0
    complete: bool = False

    # Number of meaningful results that trigger automatic completion.
    COMPLETION_THRESHOLD: int = Field(default=3, exclude=True)

    def record_result(self, result: str) -> None:
        """Append a non-trivial result and auto-complete when threshold met."""
        if not result or len(result.strip()) < 20:
            return
        self.results.append(result.strip())
        if len(self.results) >= self.COMPLETION_THRESHOLD:
            self.complete = True

    def advance(self) -> None:
        """Increment the step counter."""
        self.step += 1

    def summary(self) -> str:
        """Return a combined summary of all collected results."""
        if not self.results:
            return "No results collected."
        parts = [f"[{i+1}] {r[:500]}" for i, r in enumerate(self.results)]
        return "Collected results:\n" + "\n".join(parts)


# ── Session management ────────────────────────────────────────────────────────

class SessionStatus(str, enum.Enum):
    """Lifecycle states for an agent session."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class StepRecord(BaseModel):
    """One step in the agent loop."""
    step_number: int
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    screenshot_b64: Optional[str] = None  # base64 PNG
    action: Optional[AgentAction] = None
    raw_model_response: Optional[str] = None
    error: Optional[str] = None


class AgentSession(BaseModel):
    """Full state of an agent run."""
    session_id: str
    task: str
    status: SessionStatus = SessionStatus.IDLE
    model: str = "gemini-3-flash-preview"
    engine: str = "playwright_mcp"
    steps: list[StepRecord] = Field(default_factory=list)
    max_steps: int = 50
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── API request / response ────────────────────────────────────────────────────

class StartTaskRequest(BaseModel):
    """Validated request body for POST /api/agent/start."""

    task: str = Field(max_length=10_000)
    api_key: Optional[str] = Field(default=None, max_length=256)
    model: str = Field(default="gemini-3-flash-preview", max_length=64)
    max_steps: int = Field(default=50, ge=1, le=200)
    mode: str = Field(max_length=20)
    engine: str = Field(max_length=20)  # Required — no default, user must choose
    provider: str = Field(max_length=20)
    execution_target: str = Field(default="local", max_length=20)  # "local" or "docker"
    system_prompt: Optional[str] = Field(default=None, max_length=50_000)
    allowed_domains: Optional[list[str]] = Field(default=None, max_length=50)


class TaskStatusResponse(BaseModel):
    """Response shape for GET /api/agent/status."""

    session_id: str
    status: SessionStatus
    current_step: int
    total_steps: int
    last_action: Optional[AgentAction] = None


class StructuredError(BaseModel):
    """Uniform error envelope returned by the agent loop and executor.

    Every error produced by the system carries the step number, the action
    that triggered it, a machine-readable ``errorCode``, and a
    human-readable ``message``.
    """

    step: int = 0
    action: str = "unknown"
    errorCode: str = "unknown_error"
    message: str = "An unknown error occurred"

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON responses."""
        return self.model_dump()


class LogEntry(BaseModel):
    """Structured log entry emitted over WebSocket."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    level: str = "info"
    message: str
    data: Optional[dict] = None
