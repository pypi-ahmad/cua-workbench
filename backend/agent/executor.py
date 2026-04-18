"""Action execution engine — pure dispatch to the selected engine.

POSTs to the agent service running inside the container. Each engine is
self-contained: no fallback, no cross-engine calls, no auto-switching.
The user’s chosen engine is used for every action in the session."""

from __future__ import annotations

import asyncio
import logging
from typing import Union, Dict, Any

import httpx

from backend.config import config
from backend.models import ActionType, AgentAction, StructuredError
from backend.tools.unified_schema import UnifiedAction, normalize_action
from backend.tools.router import validate_engine, InvalidEngineError
from backend.engine_capabilities import EngineCapabilities

logger = logging.getLogger(__name__)

# Singleton capability registry (loaded once from engine_capabilities.json)
_capability_registry = EngineCapabilities()

# Reusable client
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return or create the module-level reusable httpx client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def _structured_error(
    *,
    success: bool = False,
    message: str,
    error_type: str = "validation",
    step: int = 0,
    action: str = "unknown",
    errorCode: str = "unknown_error",
) -> dict:
    """Build a result dict that includes structured error metadata."""
    return {
        "success": success,
        "message": message,
        "error_type": error_type,
        "structured_error": StructuredError(
            step=step, action=action, errorCode=errorCode, message=message,
        ).to_dict(),
    }


def validate_unified_action(action: UnifiedAction) -> Dict[str, Any] | None:
    """Pre-dispatch validation for coordinates, text, and required fields."""

    # 1. Validate coordinates
    if action.coordinates:
        if any(c < 0 for c in action.coordinates):
            return {
                "success": False,
                "message": f"Coordinates must be positive integers: {action.coordinates}",
                "error_type": "validation"
            }

        # Screen bounds check — use the actually-configured screen size
        # (set via SCREEN_WIDTH / SCREEN_HEIGHT env vars).  Previously
        # hard-coded to 1440x900, which silently rejected every click
        # beyond that on non-default resolutions.
        if len(action.coordinates) >= 2:
            x, y = action.coordinates[0], action.coordinates[1]
            max_x, max_y = config.screen_width, config.screen_height
            if x > max_x or y > max_y:
                return {
                    "success": False,
                    "message": f"Coordinates out of bounds ({max_x}x{max_y}): ({x}, {y})",
                    "error_type": "validation"
                }

    # 2. Validate text length
    if action.text and len(action.text) > 5000:
        return {
            "success": False, 
            "message": f"Text too long ({len(action.text)} chars), max 5000", 
            "error_type": "validation"
        }

    # 3. Required fields for specific actions (Selector Validation)
    if action.engine == "playwright_mcp" and action.canonical_action in (
        ActionType.CLICK, ActionType.HOVER, ActionType.TYPE, ActionType.FILL
    ):
        # MCP requires selector/target for these interactions (no coordinates)
        if not action.target and not action.selector:
             return {
                 "success": False, 
                 "message": f"Action '{action.action}' requires target/selector in MCP mode", 
                 "error_type": "validation"
             }

    return None


async def execute_action(
    action: Union[AgentAction, Dict[str, Any]],
    mode: str = "browser",
    engine: str = "playwright_mcp",
    step: int = 0,
    execution_target: str = "local",
) -> dict:
    """Execute a single agent action via the internal agent service.

    Args:
        action: The action to execute.
        mode: 'browser' or 'desktop'.
        engine: 'playwright_mcp', 'omni_accessibility', or 'computer_use'.
        step: Current step number (used in structured error reporting).
        execution_target: 'local' (host machine) or 'docker' (container).

    Returns:
        dict with keys: success (bool), message (str)
    """
    # The computer_use engine runs its own internal loop via
    # ComputerUseEngine.execute_task() — it should never reach this
    # per-action dispatcher.
    if engine == "computer_use":
        return _structured_error(
            message="computer_use engine uses its own internal loop — "
                    "actions should not be dispatched through execute_action()",
            error_type="validation",
            step=step,
            action="computer_use",
            errorCode="wrong_dispatch_path",
        )

    # Resolve action name for structured errors
    if isinstance(action, AgentAction):
        action_name = action.action.value if hasattr(action.action, "value") else str(action.action)
    else:
        action_name = str(action.get("action", "unknown"))

    if isinstance(action, AgentAction):
        # Handle the case where action.action is an Enum
        action_val = action.action.value if hasattr(action.action, "value") else str(action.action)
        action_dict = action.model_dump(exclude_none=True)
        action_dict["action"] = action_val
    else:
        action_dict = dict(action)

    # 1. Validate engine (strict — no override, no fallback)
    try:
        validate_engine(engine)
    except InvalidEngineError as e:
        return _structured_error(
            message=str(e),
            error_type="validation",
            step=step,
            action=action_name,
            errorCode="invalid_engine",
        )

    # 2. Normalize
    try:
        u_action = normalize_action(action, engine=engine)
    except Exception as e:
        return _structured_error(
            message=f"Normalization error: {e}",
            error_type="validation",
            step=step,
            action=action_name,
            errorCode="normalization_error",
        )

    # 2b. Validate action is supported by this engine (JSON-schema gate)
    if not _capability_registry.validate_action(engine, u_action.action):
        ok, detail = _capability_registry.validate_action_detailed(engine, u_action.action)
        return _structured_error(
            message=detail or f"Action {u_action.action!r} not supported by {engine}",
            error_type="validation",
            step=step,
            action=action_name,
            errorCode="unsupported_action",
        )

    # 3. Validate
    validation_error = validate_unified_action(u_action)
    if validation_error:
        # Enrich existing validation error with structured metadata
        validation_error["structured_error"] = StructuredError(
            step=step,
            action=action_name,
            errorCode="validation_error",
            message=validation_error.get("message", "Validation failed"),
        ).to_dict()
        return validation_error

    logger.info("Executing action: %s (engine=%s, mode=%s)", u_action.action, engine, mode)

    # Terminal actions don't need the service
    if u_action.action == ActionType.DONE.value:
        return {"success": True, "message": "Task completed", "error_type": None}
    if u_action.action == ActionType.ERROR.value:
        reasoning = action_dict.get("reasoning", "Unknown error")
        return {"success": False, "message": f"Agent error: {reasoning}", "error_type": "agent_error"}

    # ── Dispatch: Accessibility ─────────────────────────────────────────
    # execution_target="docker": AT-SPI bindings via container agent service
    # execution_target="local": Use the platform-native provider directly
    #   (Windows UIA, Mac JXA, Linux AT-SPI on host)
    if engine == "omni_accessibility":
        if execution_target == "local":
            # Run locally via the platform-specific accessibility provider
            try:
                from backend.engines.accessibility_engine import execute_accessibility_action
                result = await execute_accessibility_action(
                    action=u_action.action,
                    text=u_action.text or "",
                    target=u_action.target or u_action.selector or "",
                )
            except Exception as exc:
                result = {"success": False, "message": f"Local accessibility error: {exc}"}
        else:
            # Docker path: route through agent service HTTP API (mode=accessibility)
            # where DBus + AT-SPI are available inside the Linux container.
            payload = {
                "action": u_action.action,
                "text": u_action.text or "",
                "target": u_action.target or u_action.selector or "",
                "coordinates": u_action.coordinates or [],
                "mode": "omni_accessibility",
            }
            result = await _send_with_retry(payload, retries=2)

        if not result.get("success") and "error_type" not in result:
            result["error_type"] = "execution"
        result["engine"] = "omni_accessibility"

        if result.get("success") and u_action.canonical_action != ActionType.WAIT:
            await asyncio.sleep(config.action_delay_ms / 1000)
        return result

    # ── Dispatch: Playwright MCP ──────────────────────────────────────────
    # Playwright MCP: https://github.com/microsoft/playwright-mcp
    if engine == "playwright_mcp":
        # Direct passthrough: when the LLM provided native MCP tool_args,
        # bypass _build_mcp_args / ref resolution / JS fallback entirely.
        _has_tool_args = (
            isinstance(action, AgentAction) and action.tool_args is not None
        ) or (
            isinstance(action, dict) and action.get("tool_args") is not None
        )
        _tool_args = (
            action.tool_args if isinstance(action, AgentAction) else action_dict.get("tool_args")
        ) if _has_tool_args else None

        if _has_tool_args and _tool_args is not None:
            # ── Direct MCP path (tool_args provided) ─────────────────
            if execution_target == "docker":
                from backend.agent.playwright_mcp_client import execute_mcp_action_direct_docker
                result = await execute_mcp_action_direct_docker(
                    tool_name=u_action.action,
                    tool_args=_tool_args,
                    step=step,
                )
            else:
                from backend.agent.playwright_mcp_client import execute_mcp_action_direct
                result = await execute_mcp_action_direct(
                    tool_name=u_action.action,
                    tool_args=_tool_args,
                    step=step,
                )
        elif execution_target == "docker":
            # Docker mode: legacy flat path
            from backend.agent.playwright_mcp_client import execute_mcp_action_docker
            result = await execute_mcp_action_docker(
                action=u_action.action,
                text=u_action.text or "",
                target=u_action.target or u_action.selector or "",
                step=step,
            )
        else:
            # Local mode: legacy flat path
            from backend.agent.playwright_mcp_client import execute_mcp_action
            result = await execute_mcp_action(
                action=u_action.action,
                text=u_action.text or "",
                target=u_action.target or u_action.selector or "",
                step=step,
            )

        if not result.get("success") and "error_type" not in result:
            result["error_type"] = "execution"
        result["engine"] = "playwright_mcp"

        if result.get("success") and u_action.canonical_action != ActionType.WAIT:
            await asyncio.sleep(config.action_delay_ms / 1000)
        return result

    # ── Unsupported engine ────────────────────────────────────────────────
    return _structured_error(
        message=f"Unsupported engine: {engine!r}",
        error_type="validation",
        step=step,
        action=action_name,
        errorCode="unsupported_engine",
    )


async def _send_with_retry(payload: dict, retries: int = 2) -> dict:
    """Send action to agent service, retrying on transient failures."""
    url = f"{config.agent_service_url}/action"
    client = _get_client()
    last_error = None

    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, json=payload)
            
            try:
                data = resp.json()
            except Exception:
                return {"success": False, "message": f"Invalid JSON response: {resp.text[:200]}", "error_type": "service_error"}

            if resp.status_code == 200:
                # Ensure structure
                if "success" not in data:
                    data["success"] = True
                if "message" not in data:
                    data["message"] = "Success"
                return data
            else:
                last_error = data.get("message", f"HTTP {resp.status_code}")
                logger.warning("Action failed (attempt %d): %s", attempt + 1, last_error)

        except httpx.TimeoutException:
            last_error = "Agent service timeout"
            logger.warning("Agent service timeout (attempt %d)", attempt + 1)
        except httpx.ConnectError:
            last_error = "Agent service unreachable"
            logger.warning("Agent service unreachable (attempt %d)", attempt + 1)
        except Exception as e:
            last_error = str(e)
            logger.warning("Action error (attempt %d): %s", attempt + 1, e)

        if attempt < retries:
            await asyncio.sleep(0.5)

    return {"success": False, "message": f"Action failed after {retries + 1} attempts: {last_error}", "error_type": "service_error"}


async def check_accessibility_health_remote() -> dict:
    """Check AT-SPI health via the agent service /health/a11y endpoint.

    Returns dict with keys: healthy (bool), bindings (bool), error (str|None).
    Runs in the host process — the actual AT-SPI check executes inside the
    container where the DBus session bus and GI bindings are available.
    """
    url = f"{config.agent_service_url}/health/a11y"
    try:
        client = _get_client()
        resp = await client.get(url, timeout=10.0)
        return resp.json()
    except Exception as exc:
        return {"healthy": False, "bindings": False, "error": str(exc)}
