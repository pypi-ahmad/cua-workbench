"""Unified Computer Use engine — native CU protocol for Gemini & Claude.

Replaces ad-hoc text-parsing of model responses with the structured
``computer_use`` tool protocol that both Gemini 3 Flash and Claude 4.6
Sonnet support natively.

Architecture
~~~~~~~~~~~~
::

    ComputerUseEngine
    ├── GeminiCUClient   (google-genai  types.Tool(computer_use=...))
    ├── ClaudeCUClient   (anthropic     computer_2025XXYY tool, auto-detected)
    └── Executors
        ├── PlaywrightExecutor  (browser actions via Playwright page)
        └── DesktopExecutor     (desktop via agent_service HTTP API → xdotool + scrot)

Playwright MCP is **not** affected — it remains a separate engine path.

Usage::

    engine = ComputerUseEngine(
        provider=Provider.GEMINI,
        api_key="...",
        environment=Environment.BROWSER,
    )
    result = await engine.execute_task("Search for ...", page=playwright_page)
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GEMINI_NORMALIZED_MAX = 1000  # Gemini CU outputs 0-999 normalized coords
DEFAULT_SCREEN_WIDTH = 1440
DEFAULT_SCREEN_HEIGHT = 900
DEFAULT_TURN_LIMIT = 25


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    GEMINI = "gemini"
    CLAUDE = "claude"


class Environment(str, Enum):
    BROWSER = "browser"
    DESKTOP = "desktop"


class SafetyDecision(str, Enum):
    ALLOWED = "allowed"
    REQUIRE_CONFIRMATION = "require_confirmation"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CUActionResult:
    """Result of executing a single CU action."""
    name: str
    success: bool = True
    error: Optional[str] = None
    safety_decision: Optional[SafetyDecision] = None
    safety_explanation: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CUTurnRecord:
    """Record of one agent-loop turn, emitted via on_turn callback."""
    turn: int
    model_text: str
    actions: List[CUActionResult]
    screenshot_b64: Optional[str] = None


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def denormalize_x(x: int, screen_width: int = DEFAULT_SCREEN_WIDTH) -> int:
    """Convert Gemini normalized x (0-999) to pixel coordinate."""
    return int(x / GEMINI_NORMALIZED_MAX * screen_width)


def denormalize_y(y: int, screen_height: int = DEFAULT_SCREEN_HEIGHT) -> int:
    """Convert Gemini normalized y (0-999) to pixel coordinate."""
    return int(y / GEMINI_NORMALIZED_MAX * screen_height)


# ---------------------------------------------------------------------------
# Executor Protocol
# ---------------------------------------------------------------------------

class ActionExecutor(Protocol):
    """Interface that both Playwright and Desktop executors implement."""

    screen_width: int
    screen_height: int

    async def execute(self, name: str, args: Dict[str, Any]) -> CUActionResult: ...
    async def capture_screenshot(self) -> bytes: ...
    def get_current_url(self) -> str: ...


# ---------------------------------------------------------------------------
# PlaywrightExecutor — browser-scoped actions
# ---------------------------------------------------------------------------

class PlaywrightExecutor:
    """Translates CU actions into async Playwright calls.

    Implements every action from the Gemini CU supported-actions table:
    ``open_web_browser``, ``wait_5_seconds``, ``go_back``, ``go_forward``,
    ``search``, ``navigate``, ``click_at``, ``hover_at``, ``type_text_at``,
    ``key_combination``, ``scroll_document``, ``scroll_at``, ``drag_and_drop``.

    Gemini sends normalized 0-999 coords → denormalized here.
    Claude sends real pixel coords → passed through (``normalize_coords=False``).
    """

    def __init__(
        self,
        page: Any,
        screen_width: int = DEFAULT_SCREEN_WIDTH,
        screen_height: int = DEFAULT_SCREEN_HEIGHT,
        normalize_coords: bool = True,
    ):
        self.page = page
        self.screen_width = screen_width
        self.screen_height = screen_height
        self._normalize = normalize_coords

    def _px(self, x: int, y: int) -> Tuple[int, int]:
        if self._normalize:
            return denormalize_x(x, self.screen_width), denormalize_y(y, self.screen_height)
        return x, y

    async def execute(self, name: str, args: Dict[str, Any]) -> CUActionResult:
        safety = self._pop_safety(args)
        handler = getattr(self, f"_act_{name}", None)
        if handler is None:
            return CUActionResult(name=name, success=False,
                                  error=f"Unimplemented action: {name}", **safety)
        try:
            extra = await handler(args) or {}
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.4)
            return CUActionResult(name=name, success=True, extra=extra, **safety)
        except Exception as exc:
            logger.error("PlaywrightExecutor %s failed: %s", name, exc, exc_info=True)
            return CUActionResult(name=name, success=False, error=str(exc), **safety)

    @staticmethod
    def _pop_safety(args: Dict) -> Dict:
        sd = args.pop("safety_decision", None)
        if isinstance(sd, dict):
            return {
                "safety_decision": SafetyDecision(sd.get("decision", "allowed")),
                "safety_explanation": sd.get("explanation"),
            }
        return {}

    # ── Action implementations ────────────────────────────────────────

    async def _act_open_web_browser(self, a: Dict) -> Dict:
        return {}

    async def _act_wait_5_seconds(self, a: Dict) -> Dict:
        await asyncio.sleep(5)
        return {}

    async def _act_go_back(self, a: Dict) -> Dict:
        await self.page.go_back()
        return {}

    async def _act_go_forward(self, a: Dict) -> Dict:
        await self.page.go_forward()
        return {}

    async def _act_search(self, a: Dict) -> Dict:
        await self.page.goto("https://www.google.com")
        return {}

    async def _act_navigate(self, a: Dict) -> Dict:
        url = a["url"]
        await self.page.goto(url)
        return {"url": url}

    async def _act_click_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        await self.page.mouse.click(px, py)
        return {"pixel_x": px, "pixel_y": py}

    async def _act_hover_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        await self.page.mouse.move(px, py)
        return {"pixel_x": px, "pixel_y": py}

    async def _act_type_text_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        text = a["text"]
        press_enter = a.get("press_enter", True)
        clear_before = a.get("clear_before_typing", True)
        await self.page.mouse.click(px, py)
        if clear_before:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
        await self.page.keyboard.type(text)
        if press_enter:
            await self.page.keyboard.press("Enter")
        return {"pixel_x": px, "pixel_y": py, "text": text}

    async def _act_key_combination(self, a: Dict) -> Dict:
        keys = a["keys"]
        await self.page.keyboard.press(keys)
        return {"keys": keys}

    async def _act_scroll_document(self, a: Dict) -> Dict:
        direction = a["direction"]
        dx, dy = self._scroll_delta(direction)
        await self.page.mouse.wheel(dx, dy)
        return {"direction": direction}

    async def _act_scroll_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        direction = a["direction"]
        magnitude = a.get("magnitude", 800)
        await self.page.mouse.move(px, py)
        dx, dy = self._scroll_delta(direction, magnitude)
        await self.page.mouse.wheel(dx, dy)
        return {"pixel_x": px, "pixel_y": py, "direction": direction}

    async def _act_drag_and_drop(self, a: Dict) -> Dict:
        sx, sy = self._px(a["x"], a["y"])
        dx, dy = self._px(a["destination_x"], a["destination_y"])
        await self.page.mouse.move(sx, sy)
        await self.page.mouse.down()
        await self.page.mouse.move(dx, dy, steps=10)
        await self.page.mouse.up()
        return {"from": (sx, sy), "to": (dx, dy)}

    @staticmethod
    def _scroll_delta(direction: str, magnitude: int = 800) -> Tuple[int, int]:
        pixel_mag = int(magnitude / GEMINI_NORMALIZED_MAX * DEFAULT_SCREEN_HEIGHT)
        return {
            "up": (0, -pixel_mag), "down": (0, pixel_mag),
            "left": (-pixel_mag, 0), "right": (pixel_mag, 0),
        }.get(direction, (0, pixel_mag))

    async def capture_screenshot(self) -> bytes:
        return await self.page.screenshot(type="png")

    def get_current_url(self) -> str:
        return self.page.url


# ---------------------------------------------------------------------------
# DesktopExecutor — remote execution via agent_service HTTP API
# ---------------------------------------------------------------------------

# Reference mapping from CU action names (Gemini protocol) to agent_service's
# action vocabulary at ``POST /action``.  This dict is NOT used at runtime —
# each action is dispatched via ``_act_*`` methods below — but serves as a
# quick-reference for developers maintaining the two-sided integration.
_CU_TO_AGENT_SERVICE: Dict[str, str] = {
    "click_at": "click",
    "double_click": "double_click",
    "right_click": "right_click",
    "triple_click": "click (×3)",
    "hover_at": "hover",
    "type_text_at": "type",
    "type_at_cursor": "type",
    "key_combination": "key",
    "scroll_document": "scroll",
    "scroll_at": "scroll",
    "drag_and_drop": "drag",
    "navigate": "open_url",
    "open_web_browser": "open_url",
    "go_back": "key",
    "go_forward": "key",
    "search": "open_url",
    "wait_5_seconds": "wait",
}


class DesktopExecutor:
    """Translates CU actions into ``POST /action`` calls to the agent_service.

    All commands are executed inside the Docker container by sending
    HTTP requests to the agent_service (port 9222 by default), so the
    backend can run on **any host OS** — including Windows — while
    ``xdotool`` and ``scrot`` run in the Linux container.

    Screenshots are retrieved via ``GET /screenshot?mode=desktop`` on the
    same agent_service.  If the service is unreachable, a ``docker exec``
    fallback is used for screenshots only.
    """

    def __init__(
        self,
        screen_width: int = DEFAULT_SCREEN_WIDTH,
        screen_height: int = DEFAULT_SCREEN_HEIGHT,
        normalize_coords: bool = True,
        agent_service_url: str = "http://127.0.0.1:9222",
        container_name: str = "cua-environment",
    ):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self._normalize = normalize_coords
        self._service_url = agent_service_url
        self._container = container_name
        self._client: Optional[httpx.AsyncClient] = None

    def _px(self, x: int, y: int) -> Tuple[int, int]:
        if self._normalize:
            return denormalize_x(x, self.screen_width), denormalize_y(y, self.screen_height)
        return x, y

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _post_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST an action to the agent_service and return the JSON result."""
        client = await self._get_client()
        resp = await client.post(f"{self._service_url}/action", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ── ActionExecutor interface ──────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying httpx client to prevent resource leaks."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, name: str, args: Dict[str, Any]) -> CUActionResult:
        """Map a CU action to the agent_service ``/action`` endpoint."""
        handler = getattr(self, f"_act_{name}", None)
        if handler is None:
            return CUActionResult(
                name=name, success=False,
                error=f"Unimplemented desktop action: {name}",
            )
        try:
            extra = await handler(args) or {}
            # Detect agent_service returning {"success": false}
            if isinstance(extra, dict) and extra.get("success") is False:
                return CUActionResult(
                    name=name, success=False,
                    error=extra.get("message", "Action failed"),
                    extra=extra,
                )
            await asyncio.sleep(0.3)
            return CUActionResult(name=name, success=True, extra=extra)
        except Exception as exc:
            logger.error("DesktopExecutor %s failed: %s", name, exc, exc_info=True)
            return CUActionResult(name=name, success=False, error=str(exc))

    # ── Desktop-level actions (via agent_service) ─────────────────────

    async def _act_click_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        result = await self._post_action({
            "action": "click", "coordinates": [px, py], "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, **result}

    async def _act_double_click(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        result = await self._post_action({
            "action": "double_click", "coordinates": [px, py], "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, **result}

    async def _act_right_click(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        result = await self._post_action({
            "action": "right_click", "coordinates": [px, py], "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, **result}

    async def _act_triple_click(self, a: Dict) -> Dict:
        """Simulate triple-click (select paragraph/line) via 3 rapid clicks."""
        px, py = self._px(a["x"], a["y"])
        # Use double_click (2 rapid clicks via xdotool) + single click
        await self._post_action({
            "action": "double_click", "coordinates": [px, py], "mode": "desktop",
        })
        result = await self._post_action({
            "action": "click", "coordinates": [px, py], "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, **result}

    async def _act_hover_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        result = await self._post_action({
            "action": "hover", "coordinates": [px, py], "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, **result}

    async def _act_type_text_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        text = a["text"]
        press_enter = a.get("press_enter", True)
        clear_before = a.get("clear_before_typing", True)
        # Click at coordinates first
        await self._post_action({
            "action": "click", "coordinates": [px, py], "mode": "desktop",
        })
        # Clear existing text if requested
        if clear_before:
            await self._post_action({
                "action": "hotkey", "text": "ctrl+a", "mode": "desktop",
            })
            await self._post_action({
                "action": "key", "text": "BackSpace", "mode": "desktop",
            })
        # Type the text
        await self._post_action({
            "action": "type", "text": text, "mode": "desktop",
        })
        if press_enter:
            await self._post_action({
                "action": "key", "text": "Return", "mode": "desktop",
            })
        return {"pixel_x": px, "pixel_y": py, "text": text}

    async def _act_key_combination(self, a: Dict) -> Dict:
        keys = a["keys"]
        xdo_keys = (keys.replace("Control", "ctrl").replace("Alt", "alt")
                        .replace("Shift", "shift").replace("Meta", "super"))
        # Normalize single alphabetic tokens to lowercase for xdotool
        # but preserve special tokens like Return, BackSpace, Tab, F1-F12, etc.
        _SPECIAL_KEYS = {
            "return", "enter", "backspace", "tab", "escape", "delete",
            "space", "home", "end", "insert", "pause",
            "left", "right", "up", "down",
            "page_up", "page_down", "pageup", "pagedown",
            "print", "scroll_lock", "num_lock", "caps_lock",
            "super", "ctrl", "alt", "shift",
        }
        _SPECIAL_KEYS.update(f"f{i}" for i in range(1, 25))
        parts = xdo_keys.split("+")
        normalized = []
        for part in parts:
            stripped = part.strip()
            if len(stripped) == 1 and stripped.isalpha():
                normalized.append(stripped.lower())
            elif stripped.lower() in _SPECIAL_KEYS:
                normalized.append(stripped)
            else:
                normalized.append(stripped)
        xdo_keys = "+".join(normalized)
        await self._post_action({
            "action": "key", "text": xdo_keys, "mode": "desktop",
        })
        return {"keys": keys}

    async def _act_scroll_document(self, a: Dict) -> Dict:
        direction = a["direction"]
        await self._post_action({
            "action": "scroll", "text": direction, "mode": "desktop",
        })
        return {"direction": direction}

    async def _act_scroll_at(self, a: Dict) -> Dict:
        px, py = self._px(a["x"], a["y"])
        direction = a["direction"]
        await self._post_action({
            "action": "scroll", "coordinates": [px, py],
            "text": direction, "mode": "desktop",
        })
        return {"pixel_x": px, "pixel_y": py, "direction": direction}

    async def _act_drag_and_drop(self, a: Dict) -> Dict:
        sx, sy = self._px(a["x"], a["y"])
        dx, dy = self._px(a["destination_x"], a["destination_y"])
        await self._post_action({
            "action": "drag", "coordinates": [sx, sy, dx, dy], "mode": "desktop",
        })
        return {"from": (sx, sy), "to": (dx, dy)}

    async def _act_navigate(self, a: Dict) -> Dict:
        url = a["url"]
        await self._post_action({
            "action": "open_url", "text": url, "mode": "desktop",
        })
        return {"url": url}

    async def _act_open_web_browser(self, a: Dict) -> Dict:
        await self._post_action({
            "action": "open_url", "text": "https://www.google.com", "mode": "desktop",
        })
        return {}

    async def _act_wait_5_seconds(self, a: Dict) -> Dict:
        await asyncio.sleep(5)
        return {}

    async def _act_go_back(self, a: Dict) -> Dict:
        await self._post_action({
            "action": "key", "text": "alt+Left", "mode": "desktop",
        })
        return {}

    async def _act_go_forward(self, a: Dict) -> Dict:
        await self._post_action({
            "action": "key", "text": "alt+Right", "mode": "desktop",
        })
        return {}

    async def _act_type_at_cursor(self, a: Dict) -> Dict:
        """Type text at the current cursor position without clicking.

        Used by Claude's ``type`` action which means *keyboard input at
        current focus* — no coordinate movement.
        """
        text = a["text"]
        press_enter = a.get("press_enter", False)
        await self._post_action({
            "action": "type", "text": text, "mode": "desktop",
        })
        if press_enter:
            await self._post_action({
                "action": "key", "text": "Return", "mode": "desktop",
            })
        return {"text": text}

    async def _act_search(self, a: Dict) -> Dict:
        await self._post_action({
            "action": "open_url", "text": "https://www.google.com", "mode": "desktop",
        })
        return {}

    # ── Screenshot ────────────────────────────────────────────────────

    async def capture_screenshot(self) -> bytes:
        """Capture a screenshot via the agent_service, with docker exec fallback."""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self._service_url}/screenshot", params={"mode": "desktop"},
            )
            resp.raise_for_status()
            data = resp.json()
            b64 = data["screenshot"]
            return base64.b64decode(b64)
        except Exception as exc:
            logger.warning(
                "Agent service screenshot failed (%s), falling back to docker exec", exc,
            )
            return await self._fallback_screenshot()

    async def _fallback_screenshot(self) -> bytes:
        """Grab a screenshot via ``docker exec scrot`` as last resort."""
        path = "/tmp/cu_screenshot.png"
        # Run scrot inside the container
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec",
            "-e", "DISPLAY=:99",
            self._container, "scrot", "-z", "-o", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        # Read the resulting PNG back
        proc_read = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container, "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc_read.communicate()
        if proc_read.returncode != 0 or not stdout:
            raise RuntimeError(
                f"Fallback screenshot failed: {stderr.decode(errors='replace')}"
            )
        return stdout

    def get_current_url(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# Gemini Computer Use Client
# ---------------------------------------------------------------------------

class GeminiCUClient:
    """Native Gemini Computer Use tool protocol.

    API contract:
    - Declares ``types.Tool(computer_use=ComputerUse(...))``
    - Enables ``ThinkingConfig(include_thoughts=True)``
    - Sends screenshots inline in ``FunctionResponse`` parts
    - Handles ``safety_decision`` → ``require_confirmation``
    - Supports both ``ENVIRONMENT_BROWSER`` and ``ENVIRONMENT_DESKTOP``
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3-flash-preview",
        environment: Environment = Environment.BROWSER,
        excluded_actions: Optional[List[str]] = None,
        system_instruction: Optional[str] = None,
    ):
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:
            raise ImportError(
                "google-genai is required. Install: pip install google-genai"
            ) from exc

        self._genai = genai
        self._types = genai_types
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._environment = environment
        self._excluded = excluded_actions or []
        self._system_instruction = system_instruction

    def _get_env_enum(self) -> Any:
        types = self._types
        if self._environment == Environment.DESKTOP:
            desktop_env = getattr(types.Environment, "ENVIRONMENT_DESKTOP", None)
            if desktop_env is not None:
                return desktop_env
            logger.warning(
                "ENVIRONMENT_DESKTOP not available in google-genai SDK; "
                "falling back to ENVIRONMENT_BROWSER.  Desktop xdotool "
                "actions will still execute via DesktopExecutor."
            )
            return types.Environment.ENVIRONMENT_BROWSER
        return types.Environment.ENVIRONMENT_BROWSER

    def _build_config(self) -> Any:
        types = self._types
        tools = [
            types.Tool(
                computer_use=types.ComputerUse(
                    environment=self._get_env_enum(),
                    excluded_predefined_functions=self._excluded,
                )
            )
        ]

        # Relax safety thresholds so the model doesn't silently refuse when
        # seeing desktop screenshots that contain innocuous UI chrome the
        # safety classifier may over-flag (e.g. browser with sign-in pages,
        # system toolbars, ads).
        safety_settings = []
        _HarmCategory = getattr(types, "HarmCategory", None)
        _SafetySetting = getattr(types, "SafetySetting", None)
        _HarmBlockThreshold = getattr(types, "HarmBlockThreshold", None)
        if _HarmCategory and _SafetySetting and _HarmBlockThreshold:
            block_level = getattr(_HarmBlockThreshold, "BLOCK_ONLY_HIGH",
                                  getattr(_HarmBlockThreshold, "BLOCK_NONE", None))
            if block_level is not None:
                for cat_name in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                ):
                    cat = getattr(_HarmCategory, cat_name, None)
                    if cat is not None:
                        safety_settings.append(
                            _SafetySetting(category=cat, threshold=block_level)
                        )

        kwargs: Dict[str, Any] = {
            "tools": tools,
            "thinking_config": types.ThinkingConfig(include_thoughts=True),
        }
        if safety_settings:
            kwargs["safety_settings"] = safety_settings
        if self._system_instruction:
            kwargs["system_instruction"] = self._system_instruction
        return self._genai.types.GenerateContentConfig(**kwargs)

    async def run_loop(
        self,
        goal: str,
        executor: ActionExecutor,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
        on_safety: Optional[Callable[[str], bool]] = None,
        on_turn: Optional[Callable[[CUTurnRecord], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Run the full Gemini CU agent loop.

        Args:
            goal: Natural language task.
            executor: PlaywrightExecutor or DesktopExecutor.
            turn_limit: Max loop iterations.
            on_safety: Callback(explanation) → bool. True=confirm, False=deny.
            on_turn: Progress callback per turn.
            on_log: Logging callback(level, message).

        Returns:
            Final text response from the model.
        """
        types = self._types
        config = self._build_config()

        # Initial screenshot
        screenshot_bytes = await executor.capture_screenshot()
        if not screenshot_bytes or len(screenshot_bytes) < 100:
            if on_log:
                on_log("error", "Initial screenshot capture failed or returned empty bytes")
            return "Error: Could not capture initial screenshot"

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(text=goal),
                    types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
                ],
            )
        ]

        final_text = ""

        for turn in range(turn_limit):
            if on_log:
                on_log("info", f"Gemini CU turn {turn + 1}/{turn_limit}")

            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self._model,
                    contents=contents,
                    config=config,
                )
            except Exception as api_err:
                error_msg = str(api_err)
                if on_log:
                    on_log("error", f"Gemini API error at turn {turn + 1}: {error_msg}")
                # Try to provide actionable info for common error patterns
                if "INVALID_ARGUMENT" in error_msg:
                    if on_log:
                        on_log("error",
                            "INVALID_ARGUMENT usually means: (1) screenshot too large/corrupt, "
                            "(2) model doesn't support computer_use tool, or "
                            "(3) conversation context exceeded limits. "
                            f"Contents length: {len(contents)} turns, "
                            f"last screenshot: {len(screenshot_bytes)} bytes")
                final_text = f"Gemini API error: {error_msg}"
                break

            if not response.candidates:
                if on_log:
                    on_log("warning", f"Gemini returned no candidates at turn {turn + 1} — retrying with nudge")

                # Retry once: append a user nudge reminding the model to
                # use computer_use tools and re-send with a fresh screenshot.
                try:
                    retry_ss = await executor.capture_screenshot()
                except Exception:
                    retry_ss = screenshot_bytes

                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                text=(
                                    "Please continue using the computer_use tools to "
                                    "complete the task. Here is the current screen."
                                )
                            ),
                            types.Part.from_bytes(
                                data=retry_ss, mime_type="image/png"
                            ),
                        ],
                    )
                )
                try:
                    response = await asyncio.to_thread(
                        self._client.models.generate_content,
                        model=self._model,
                        contents=contents,
                        config=config,
                    )
                except Exception as retry_err:
                    if on_log:
                        on_log("error", f"Retry also failed: {retry_err}")
                    final_text = f"Error: Gemini returned no candidates and retry failed: {retry_err}"
                    break

                if not response.candidates:
                    if on_log:
                        on_log("error", f"Gemini returned no candidates even after retry at turn {turn + 1}")
                    final_text = "Error: Gemini returned no candidates (after retry)"
                    break

            candidate = response.candidates[0]
            contents.append(candidate.content)

            # Extract function calls and text
            function_calls = [
                p.function_call for p in candidate.content.parts if p.function_call
            ]
            text_parts = [p.text for p in candidate.content.parts if p.text]
            turn_text = " ".join(text_parts)

            # No function calls → model is done
            if not function_calls:
                final_text = turn_text
                if on_log:
                    on_log("info", f"Gemini CU completed: {final_text[:200]}")
                if on_turn:
                    on_turn(CUTurnRecord(turn=turn + 1, model_text=turn_text, actions=[]))
                break

            # Execute each function call
            results: List[CUActionResult] = []
            terminated = False

            for fc in function_calls:
                args = dict(fc.args) if fc.args else {}

                # Extract safety_decision BEFORE passing args to executor.
                # This ensures the acknowledgement is tracked regardless of
                # which executor (Playwright or Desktop) is used.
                safety_confirmed = False
                if "safety_decision" in args:
                    sd = args.pop("safety_decision")
                    if isinstance(sd, dict) and sd.get("decision") == "require_confirmation":
                        if on_safety:
                            if asyncio.iscoroutinefunction(on_safety):
                                confirmed = await on_safety(sd.get("explanation", ""))
                            else:
                                confirmed = on_safety(sd.get("explanation", ""))
                        else:
                            confirmed = False
                        if not confirmed:
                            if on_log:
                                on_log("warning", f"Safety denied for {fc.name}")
                            terminated = True
                            break
                        safety_confirmed = True

                result = await executor.execute(fc.name, args)
                # Stamp safety metadata so FunctionResponse includes
                # safety_acknowledgement when the user confirmed.
                if safety_confirmed:
                    result.safety_decision = SafetyDecision.REQUIRE_CONFIRMATION
                results.append(result)

            # Emit turn record
            try:
                screenshot_bytes = await executor.capture_screenshot()
            except Exception as ss_err:
                if on_log:
                    on_log("warning", f"Screenshot capture failed at turn {turn + 1}: {ss_err}")
                screenshot_bytes = b""

            screenshot_b64 = base64.standard_b64encode(screenshot_bytes).decode() if screenshot_bytes else ""
            if on_turn:
                on_turn(CUTurnRecord(
                    turn=turn + 1, model_text=turn_text,
                    actions=results, screenshot_b64=screenshot_b64 or None,
                ))

            if terminated:
                final_text = "Agent terminated: safety confirmation denied."
                break

            # Build FunctionResponses with inline screenshot per Gemini CU docs:
            # https://ai.google.dev/gemini-api/docs/computer-use
            # Each FunctionResponse embeds the screenshot via
            #   parts=[FunctionResponsePart(inline_data=FunctionResponseBlob(...))]
            # The screenshot must NOT be sent as a separate Part.from_bytes().
            current_url = executor.get_current_url()
            screenshot_ok = bool(screenshot_bytes) and len(screenshot_bytes) >= 100

            function_responses = []
            for r in results:
                resp_data: Dict[str, Any] = {"url": current_url}
                if r.error:
                    resp_data["error"] = r.error
                if r.safety_decision == SafetyDecision.REQUIRE_CONFIRMATION:
                    resp_data["safety_acknowledgement"] = "true"
                # Merge extra data, converting non-serializable types (tuples → lists)
                for k, v in r.extra.items():
                    if isinstance(v, tuple):
                        resp_data[k] = list(v)
                    elif isinstance(v, (str, int, float, bool, type(None), list, dict)):
                        resp_data[k] = v
                    else:
                        resp_data[k] = str(v)

                fr_kwargs: Dict[str, Any] = {"name": r.name, "response": resp_data}

                if screenshot_ok:
                    fr_kwargs["parts"] = [
                        types.FunctionResponsePart(
                            inline_data=types.FunctionResponseBlob(
                                mime_type="image/png",
                                data=screenshot_bytes,
                            )
                        )
                    ]

                function_responses.append(types.FunctionResponse(**fr_kwargs))

            # IMPORTANT: send ONLY FunctionResponse parts — no separate image Part
            if not function_responses:
                if on_log:
                    on_log("warning", "No function responses to send; ending loop")
                break

            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part(function_response=fr) for fr in function_responses],
                )
            )

        return final_text


# ---------------------------------------------------------------------------
# Claude Computer Use Client
# ---------------------------------------------------------------------------

class ClaudeCUClient:
    """Native Claude computer-use tool protocol.

    API contract:
    - Auto-detects tool version from model name:
      * Claude Sonnet 4.6 / Opus 4.6 / Opus 4.5 → ``computer_20251124``
        with beta header ``computer-use-2025-11-24``
      * All other CU models → ``computer_20250124``
        with beta header ``computer-use-2025-01-24``
    - Uses ``client.beta.messages.create()`` (beta endpoint required)
    - Enables thinking with a conservative token budget
    - Sends screenshots as base64 in ``tool_result`` content
    - Claude outputs real pixel coordinates (no normalization)
    - ``display_number`` is intentionally omitted (optional, often wrong)
    - Actions: screenshot, click, double_click, type, key, scroll,
      mouse_move, left_click_drag, triple_click, right_click
    """

    # Models that require the newer computer_20251124 tool version.
    # TODO: Remove this list once all callers pass tool_version/beta_flag
    # from allowed_models.json (cu_tool_version / cu_betas fields).
    _NEW_TOOL_MODELS = (
        "claude-sonnet-4-6", "claude-sonnet-4.6",
        "claude-opus-4-6", "claude-opus-4.6",
        "claude-opus-4-5", "claude-opus-4.5",
    )

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        system_prompt: Optional[str] = None,
        tool_version: Optional[str] = None,
        beta_flag: Optional[str] = None,
    ):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic is required. Install: pip install anthropic"
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._system_prompt = system_prompt or ""

        # Use explicit values from allowed_models.json if provided,
        # otherwise auto-detect from model name (backwards compatibility).
        if tool_version and beta_flag:
            self._tool_version = tool_version
            self._beta_flag = beta_flag
        elif any(tag in model for tag in self._NEW_TOOL_MODELS):
            self._tool_version = "computer_20251124"
            self._beta_flag = "computer-use-2025-11-24"
        else:
            self._tool_version = "computer_20250124"
            self._beta_flag = "computer-use-2025-01-24"

    def _build_tools(self, sw: int, sh: int) -> List[Dict]:
        return [
            {
                "type": self._tool_version,
                "name": "computer",
                "display_width_px": sw,
                "display_height_px": sh,
            }
        ]

    async def run_loop(
        self,
        goal: str,
        executor: ActionExecutor,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
        on_safety: Optional[Callable[[str], bool]] = None,
        on_turn: Optional[Callable[[CUTurnRecord], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Run the full Claude CU agent loop."""
        tools = self._build_tools(executor.screen_width, executor.screen_height)

        screenshot_bytes = await executor.capture_screenshot()
        screenshot_b64 = base64.standard_b64encode(screenshot_bytes).decode()

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": goal},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                ],
            }
        ]

        final_text = ""

        for turn in range(turn_limit):
            if on_log:
                on_log("info", f"Claude CU turn {turn + 1}/{turn_limit}")

            response = await asyncio.to_thread(
                self._client.beta.messages.create,
                model=self._model,
                max_tokens=4096,
                system=self._system_prompt,
                tools=tools,
                messages=messages,
                betas=[self._beta_flag],
                thinking={"type": "enabled", "budget_tokens": 4096},
            )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            tool_uses = [b for b in assistant_content if b.type == "tool_use"]
            text_blocks = [b.text for b in assistant_content
                          if hasattr(b, "text") and b.text]
            turn_text = " ".join(text_blocks)

            if response.stop_reason == "end_turn" or not tool_uses:
                final_text = turn_text
                if on_log:
                    on_log("info", f"Claude CU completed: {final_text[:200]}")
                if on_turn:
                    on_turn(CUTurnRecord(turn=turn + 1, model_text=turn_text, actions=[]))
                break

            # Execute tool uses
            tool_result_parts = []
            results: List[CUActionResult] = []

            for tu in tool_uses:
                result = await self._execute_claude_action(tu.input, executor)
                results.append(result)

                screenshot_bytes = await executor.capture_screenshot()
                screenshot_b64 = base64.standard_b64encode(screenshot_bytes).decode()

                content: List[Dict] = []
                if result.error:
                    content.append({"type": "text", "text": f"Error: {result.error}"})
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                })

                tool_result_parts.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": content,
                })

            if on_turn:
                on_turn(CUTurnRecord(
                    turn=turn + 1, model_text=turn_text,
                    actions=results, screenshot_b64=screenshot_b64,
                ))

            messages.append({"role": "user", "content": tool_result_parts})

        return final_text

    async def _execute_claude_action(
        self, action_input: Dict, executor: ActionExecutor,
    ) -> CUActionResult:
        """Map Claude computer tool actions to executor calls.

        Claude actions: screenshot, click, double_click, type, key,
        scroll, mouse_move, left_click_drag, triple_click, right_click.

        Claude uses REAL pixel coordinates — no denormalization.
        """
        action = action_input.get("action", "")

        if action == "screenshot":
            return CUActionResult(name="screenshot")

        # Map Claude actions → Gemini-style CU action names
        CLAUDE_TO_CU: Dict[str, str] = {
            "click": "click_at",
            "double_click": "click_at",  # with special handling
            "right_click": "click_at",   # with special handling
            "type": "_claude_type",
            "key": "key_combination",
            "scroll": "scroll_at",
            "mouse_move": "hover_at",
            "left_click_drag": "drag_and_drop",
            "triple_click": "click_at",
        }

        mapped = CLAUDE_TO_CU.get(action, action)

        # Build args in the CU format the executor expects
        coord = action_input.get("coordinate")
        args: Dict[str, Any] = {}

        if action in ("click", "double_click", "right_click", "triple_click"):
            if coord:
                args["x"], args["y"] = coord[0], coord[1]
            # Handle click variants via Playwright directly
            if action in ("double_click", "right_click", "triple_click"):
                return await self._special_click(action, coord, executor)
            return await executor.execute("click_at", args)

        elif action == "type":
            text = action_input.get("text", "")
            # Claude "type" = keyboard input at current cursor (no coords needed)
            page = getattr(executor, "page", None)
            if page:
                try:
                    await page.keyboard.type(text)
                    return CUActionResult(name="type", extra={"text": text})
                except Exception as exc:
                    return CUActionResult(name="type", success=False, error=str(exc))
            # Desktop fallback — type at current cursor via agent_service
            # (uses type_at_cursor which does NOT click/move focus first)
            try:
                result = await executor.execute("type_at_cursor", {
                    "text": text,
                    "press_enter": False,
                })
                return CUActionResult(
                    name="type", success=result.success,
                    error=result.error, extra={"text": text},
                )
            except Exception as exc:
                return CUActionResult(name="type", success=False, error=str(exc))

        elif action == "key":
            key = action_input.get("key", "")
            KEY_MAP = {"Return": "Enter", "space": "Space"}
            args["keys"] = KEY_MAP.get(key, key)
            return await executor.execute("key_combination", args)

        elif action == "scroll":
            if coord:
                args["x"], args["y"] = coord[0], coord[1]
            args["direction"] = action_input.get("direction", "down")
            amount = action_input.get("amount", 3)
            args["magnitude"] = min(999, amount * 200)
            return await executor.execute("scroll_at", args)

        elif action == "mouse_move":
            if coord:
                args["x"], args["y"] = coord[0], coord[1]
            return await executor.execute("hover_at", args)

        elif action == "left_click_drag":
            start = action_input.get("start_coordinate", coord or [0, 0])
            end = action_input.get("coordinate", [0, 0])
            args["x"], args["y"] = start[0], start[1]
            args["destination_x"], args["destination_y"] = end[0], end[1]
            return await executor.execute("drag_and_drop", args)

        else:
            return CUActionResult(name=action, success=False,
                                  error=f"Unknown Claude action: {action}")

    async def _special_click(
        self, action: str, coord: Optional[List[int]], executor: ActionExecutor,
    ) -> CUActionResult:
        """Handle double_click, right_click, triple_click via Playwright/xdotool.

        Playwright path uses native dblclick / button="right" / click_count=3.
        Desktop path delegates to the executor's dedicated ``_act_*`` handlers
        which send the correct single action to the agent_service — avoiding
        the previous bug where a redundant left click preceded the real action.
        """
        x, y = (coord[0], coord[1]) if coord else (0, 0)
        page = getattr(executor, "page", None)

        if page:
            try:
                if action == "double_click":
                    await page.mouse.dblclick(x, y)
                elif action == "right_click":
                    await page.mouse.click(x, y, button="right")
                elif action == "triple_click":
                    await page.mouse.click(x, y, click_count=3)
                return CUActionResult(name=action, extra={"x": x, "y": y})
            except Exception as exc:
                return CUActionResult(name=action, success=False, error=str(exc))

        # Desktop path — use executor.execute which dispatches to
        # _act_double_click / _act_right_click / _act_triple_click.
        try:
            return await executor.execute(action, {"x": x, "y": y})
        except Exception as exc:
            return CUActionResult(name=action, success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Unified ComputerUseEngine facade
# ---------------------------------------------------------------------------

class ComputerUseEngine:
    """Single entry point for native Computer Use across providers and environments.

    Replaces:
    - xdotool engine (raw pixel, no CU protocol)
    - desktop engine (xdotool, raw pixel, no CU protocol)

    Keeps untouched:
    - Playwright engine (existing path via agent_service)
    - Playwright MCP (separate STDIO transport)
    - Accessibility engine (AT-SPI, separate concern)

    Usage::

        engine = ComputerUseEngine(
            provider=Provider.GEMINI,
            api_key="AIza...",
            environment=Environment.BROWSER,
        )
        final_text = await engine.execute_task(
            "Search for flights to Paris",
            page=playwright_page,
        )
    """

    def __init__(
        self,
        provider: Provider,
        api_key: str,
        model: Optional[str] = None,
        environment: Environment = Environment.BROWSER,
        screen_width: int = DEFAULT_SCREEN_WIDTH,
        screen_height: int = DEFAULT_SCREEN_HEIGHT,
        system_instruction: Optional[str] = None,
        excluded_actions: Optional[List[str]] = None,
        container_name: str = "cua-environment",
        agent_service_url: str = "http://127.0.0.1:9222",
    ):
        self.provider = provider
        self.environment = environment
        self.screen_width = screen_width
        self.screen_height = screen_height
        self._container_name = container_name
        self._agent_service_url = agent_service_url

        if provider == Provider.GEMINI:
            self._client: Any = GeminiCUClient(
                api_key=api_key,
                model=model or "gemini-3-flash-preview",
                environment=environment,
                excluded_actions=excluded_actions,
                system_instruction=system_instruction,
            )
        elif provider == Provider.CLAUDE:
            self._client = ClaudeCUClient(
                api_key=api_key,
                model=model or "claude-sonnet-4-6",
                system_prompt=system_instruction,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _build_executor(self, page: Optional[Any] = None) -> ActionExecutor:
        """Build PlaywrightExecutor (browser) or DesktopExecutor (desktop)."""
        # Gemini uses normalized 0-999 coords; Claude uses real pixels
        normalize = self.provider == Provider.GEMINI

        if self.environment == Environment.BROWSER:
            if page is None:
                raise ValueError("Browser environment requires a Playwright page")
            return PlaywrightExecutor(
                page=page,
                screen_width=self.screen_width,
                screen_height=self.screen_height,
                normalize_coords=normalize,
            )
        return DesktopExecutor(
            screen_width=self.screen_width,
            screen_height=self.screen_height,
            normalize_coords=normalize,
            agent_service_url=self._agent_service_url,
            container_name=self._container_name,
        )

    async def execute_task(
        self,
        goal: str,
        page: Optional[Any] = None,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
        on_safety: Optional[Callable[[str], bool]] = None,
        on_turn: Optional[Callable[[CUTurnRecord], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Execute a CU task end-to-end using the native tool protocol.

        Args:
            goal: Natural language task description.
            page: Playwright async Page (required for BROWSER, optional for DESKTOP).
            turn_limit: Maximum agent loop iterations.
            on_safety: Callback for safety confirmations.
            on_turn: Progress callback per turn.
            on_log: Logging callback(level, message).

        Returns:
            Final text response from the model.
        """
        executor = self._build_executor(page)
        try:
            return await self._client.run_loop(
                goal=goal,
                executor=executor,
                turn_limit=turn_limit,
                on_safety=on_safety,
                on_turn=on_turn,
                on_log=on_log,
            )
        finally:
            # Close httpx client to prevent resource leaks
            if hasattr(executor, 'aclose'):
                try:
                    await executor.aclose()
                except Exception:
                    logger.debug("Error closing executor", exc_info=True)
