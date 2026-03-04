"""Agent loop — the core perceive → think → act orchestrator.

Implements:
- Step-by-step execution with timeout per step
- Action history buffer with context window trimming
- Failure recovery (consecutive error tolerance)
- Duplicate action detection
- Graceful stop / cancellation
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from backend.config import config
from backend.models import (
    ActionType,
    AgentAction,
    AgentSession,
    LogEntry,
    SessionStatus,
    StepRecord,
    StructuredError,
    TaskState,
)
from backend.agent.model_router import query_model
from backend.agent.prompts import get_system_prompt
from backend.agent.screenshot import capture_screenshot, check_service_health
from backend.agent.executor import execute_action

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 3
MAX_DUPLICATE_ACTIONS = 3
# Desktop engines (accessibility/computer_use) need tighter stuck detection
# because the model tends to jitter coordinates by small amounts while
# repeating the same fundamentally-broken click.  A wider pixel tolerance
# and shorter lookback window catch these loops earlier.
MAX_DUPLICATE_ACTIONS_DESKTOP = 2
DESKTOP_COORD_TOLERANCE = 30

# Maximum times stuck detection can fire before force-terminating the loop.
# Prevents the agent from burning all remaining steps on recovery hints
# that the model ignores.
MAX_STUCK_DETECTIONS = 3

# Maximum identical execution results (e.g. evaluate_js returning the same
# JSON) before the loop injects an ultimatum to return done/error.
MAX_DUPLICATE_RESULTS = 2


class AgentLoop:
    """Runs the perceive → think → act loop for a CUA session."""

    def __init__(
        self,
        task: str,
        api_key: str,
        model: str | None = None,
        max_steps: int | None = None,
        mode: str = "browser",
        engine: str = "playwright_mcp",
        provider: str = "google",
        execution_target: str = "local",
        on_step: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
        on_screenshot: Optional[Callable] = None,
    ):
        """Initialise a new agent loop for *task* using the given provider/model."""
        self.session = AgentSession(
            session_id=str(uuid.uuid4()),
            task=task,
            model=model or config.gemini_model,
            engine=engine,
            max_steps=max_steps or config.max_steps,
        )
        self._api_key = api_key
        self._engine = engine
        self._mode = mode
        self._provider = provider
        self._execution_target = execution_target  # "local" or "docker"
        self._action_history: list[AgentAction] = []
        self._stop_requested = False
        self._consecutive_errors = 0
        self._stuck_count = 0  # how many times _detect_stuck fired
        self._result_cache: list[str] = []  # recent execution result messages
        self._task_state = TaskState()  # structured per-task state tracking
        self._action_count: int = 0  # global action counter for watchdog
        self.structured_errors: list[StructuredError] = []  # structured error log

        # Callbacks for real-time streaming
        self._on_step = on_step
        self._on_log = on_log
        self._on_screenshot = on_screenshot

        # Playwright lifecycle refs (cleaned up on session end)
        self._pw = None
        self._browser = None
        self._context = None

    @property
    def session_id(self) -> str:
        """Return the unique session identifier."""
        return self.session.session_id

    def request_stop(self) -> None:
        """Request the loop to stop after the current step."""
        self._stop_requested = True
        self._emit_log("info", "Stop requested by user")

    def _emit_log(self, level: str, message: str, data: dict | None = None) -> None:
        """Create a LogEntry and forward it to the log callback."""
        entry = LogEntry(level=level, message=message, data=data)
        logger.log(
            getattr(logging, level.upper(), logging.INFO),
            "[%s] %s",
            self.session.session_id[:8],
            message,
        )
        if self._on_log:
            try:
                self._on_log(entry)
            except Exception:
                pass

    def _make_structured_error(
        self,
        *,
        step: int,
        action: str,
        errorCode: str,
        message: str,
    ) -> StructuredError:
        """Create a :class:`StructuredError`, append it to the error log, and return it."""
        err = StructuredError(
            step=step,
            action=action,
            errorCode=errorCode,
            message=message,
        )
        self.structured_errors.append(err)
        return err

    def _is_retryable_failure(self, action: AgentAction, result: dict) -> bool:
        """Return True when an execution failure should be retried once."""
        if action.action in (ActionType.DONE, ActionType.ERROR):
            return False
        error_type = (result.get("error_type") or "").lower()
        if error_type in {"validation", "agent_error"}:
            return False
        return not result.get("success", False)

    async def run(self) -> AgentSession:
        """Execute the full agent loop. Returns the final session state."""
        self.session.status = SessionStatus.RUNNING
        self._emit_log("info", f"Agent starting — task: {self.session.task}")
        self._emit_log("info", f"Model: {self.session.model} | Max steps: {self.session.max_steps} | Mode: {self._mode} | Engine: {self._engine} | Provider: {self._provider} | Target: {self._execution_target}")

        # Pre-flight: check agent service health
        healthy = await check_service_health()
        if not healthy:
            self._emit_log("warning", "Agent service not responding, will retry during execution")

        # Pre-flight: ensure Playwright MCP server is running (STDIO transport)
        if self._engine == "playwright_mcp":
            if self._execution_target == "docker":
                # Docker mode: connect to the MCP HTTP server running inside the container
                mcp_url = f"http://{config.playwright_mcp_host}:{config.playwright_mcp_port}"
                self._emit_log("info", f"Using Docker Playwright MCP server at {mcp_url}...")
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(mcp_url)
                        if resp.status_code < 500:
                            self._emit_log("info", "Docker Playwright MCP server responding")
                        else:
                            self._emit_log("warning", f"Docker MCP server returned HTTP {resp.status_code}")
                except Exception as e:
                    self._emit_log("warning", f"Docker MCP server unreachable: {e}")
            else:
                # Local mode: spawn MCP via STDIO on the host machine
                self._emit_log("info", "Initializing Playwright MCP server (STDIO)...")
                try:
                    from backend.agent.playwright_mcp_client import (
                        _ensure_mcp_initialized,
                        check_mcp_health,
                    )
                    await _ensure_mcp_initialized()

                    # Verify STDIO session is alive
                    from backend.agent import playwright_mcp_client as _mcp_mod

                    if _mcp_mod._mcp_session is not None:
                        self._emit_log(
                            "info",
                            "Playwright MCP STDIO session established",
                        )
                    else:
                        self._emit_log(
                            "warning",
                            "Playwright MCP initialized but STDIO session is None. "
                            "Actions will likely fail.",
                        )

                    mcp_ok = await check_mcp_health()
                    if mcp_ok:
                        self._emit_log("info", "Playwright MCP health check passed")
                    else:
                        self._emit_log("warning", "Playwright MCP health check failed — actions may fail")
                except Exception as e:
                    self._emit_log("warning", f"MCP initialization error: {e}")

        # Pre-flight: verify AT-SPI bus is responsive for accessibility engine
        # (health check runs inside the container via agent service HTTP API)
        if self._engine == "omni_accessibility":
            self._emit_log("info", "Checking AT-SPI accessibility bus (via agent service)...")
            try:
                from backend.agent.executor import check_accessibility_health_remote
                a11y_status = await check_accessibility_health_remote()
                if not a11y_status.get("bindings"):
                    self._emit_log(
                        "error",
                        "AT-SPI bindings unavailable inside container. "
                        "Ensure gir1.2-atspi-2.0, python3-gi, and at-spi2-core are installed "
                        "and the DBus session bus is running. "
                        f"Detail: {a11y_status.get('error', 'unknown')}",
                    )
                    self.session.status = SessionStatus.ERROR
                    return self.session
                if a11y_status.get("healthy"):
                    self._emit_log("info", "AT-SPI bus healthy — applications detected")
                else:
                    self._emit_log(
                        "warning",
                        "AT-SPI bus returned no applications — accessibility "
                        "actions may fail until a desktop app is opened",
                    )
            except Exception as e:
                self._emit_log("warning", f"AT-SPI health check error (agent service unreachable?): {e}")

        # ── Computer Use engine: delegate to native CU protocol loop ──
        if self._engine == "computer_use":
            return await self._run_computer_use_engine()

        try:
            for step_num in range(1, self.session.max_steps + 1):
                if self._stop_requested:
                    self._emit_log("info", "Agent stopped by user")
                    self.session.status = SessionStatus.COMPLETED
                    break

                # ── Early completion guard (top of loop) ──────────────
                # Don't break here — let _execute_step detect completion
                # and synthesise a proper DONE step record so the session
                # history includes the termination action.
                if self._task_state.complete:
                    self._emit_log("info", "Task already complete — executing final step")

                # ── Watchdog: max-steps guard ─────────────────────────
                if self._action_count >= self.session.max_steps:
                    err = self._make_structured_error(
                        step=step_num,
                        action="watchdog",
                        errorCode="max_steps_exceeded",
                        message=f"Aborted: reached max step limit ({self.session.max_steps})",
                    )
                    self._emit_log("error", err.message, data=err.to_dict())
                    self.session.status = SessionStatus.ERROR
                    break

                # Execute one step with timeout
                try:
                    step = await asyncio.wait_for(
                        self._execute_step(step_num),
                        timeout=config.step_timeout,
                    )
                except asyncio.TimeoutError:
                    err = self._make_structured_error(
                        step=step_num,
                        action="step_timeout",
                        errorCode="step_timeout",
                        message=f"Step timed out after {config.step_timeout}s",
                    )
                    step = StepRecord(
                        step_number=step_num,
                        error=err.message,
                    )
                    self._emit_log("error", f"Step {step_num}: Timed out", data=err.to_dict())

                self._action_count += 1
                self.session.steps.append(step)
                self._fire_callback(self._on_step, step)

                # ── Termination checks ────────────────────────────────────
                if step.action and step.action.action == ActionType.DONE:
                    self._emit_log("info", "Task completed successfully")
                    self.session.status = SessionStatus.COMPLETED
                    break

                if step.action and step.action.action == ActionType.ERROR:
                    self._emit_log("error", f"Agent error: {step.action.reasoning}")
                    self.session.status = SessionStatus.ERROR
                    break

                # ── Error tracking ────────────────────────────────────────
                if step.error:
                    self._consecutive_errors += 1
                    action_name = step.action.action.value if step.action else "unknown"
                    err = self._make_structured_error(
                        step=step_num,
                        action=action_name,
                        errorCode="consecutive_error",
                        message=step.error,
                    )
                    self._emit_log("warning",
                        f"Error #{self._consecutive_errors}: {step.error}",
                        data=err.to_dict())
                    if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        consec_err = self._make_structured_error(
                            step=step_num,
                            action=action_name,
                            errorCode="max_consecutive_errors",
                            message=(
                                f"{MAX_CONSECUTIVE_ERRORS} consecutive errors — "
                                "resetting counter and continuing execution"
                            ),
                        )
                        self._emit_log("warning",
                            consec_err.message, data=consec_err.to_dict())
                        # Reset counter and continue instead of aborting.
                        # The step is already marked as failed; the model
                        # will see the failure in its action history and
                        # can decide to try a different strategy.
                        self._consecutive_errors = 0
                else:
                    self._consecutive_errors = 0

                # ── Duplicate detection ───────────────────────────────────
                if self._detect_stuck():
                    self._stuck_count += 1
                    self._emit_log("warning",
                        f"Agent appears stuck (repeating same action) "
                        f"[stuck detection #{self._stuck_count}/{MAX_STUCK_DETECTIONS}]")

                    # Force-terminate if stuck too many times
                    if self._stuck_count >= MAX_STUCK_DETECTIONS:
                        self._emit_log("error",
                            f"Aborting: agent stuck {self._stuck_count} times — "
                            "unable to make progress")
                        self.session.status = SessionStatus.ERROR
                        break

                    # Build actionable recovery hint based on the stuck action type
                    hint = self._build_recovery_hint()
                    self._action_history.append(AgentAction(
                        action=ActionType.WAIT,
                        reasoning=hint,
                    ))

                # ── Duplicate result detection (e.g. same JS output) ──────
                if self._detect_duplicate_results():
                    self._emit_log("warning",
                        "Duplicate execution results detected — injecting "
                        "completion ultimatum")
                    self._action_history.append(AgentAction(
                        action=ActionType.WAIT,
                        reasoning=(
                            "System: STOP — you have already collected this data. "
                            "The same result has been returned multiple times. "
                            "You MUST now either:\n"
                            '1. Return done: {"action":"done","reasoning":"Task completed. <your summary here>"}\n'
                            '2. Close extra tabs and return done\n'
                            '3. Return error if something is genuinely wrong\n'
                            "DO NOT call evaluate_js or get_text again — the data is already captured."
                        ),
                    ))

                # Note: per-action delay is handled inside executor.py

            else:
                err = self._make_structured_error(
                    step=self.session.max_steps,
                    action="loop",
                    errorCode="max_steps_exceeded",
                    message=f"Reached max steps ({self.session.max_steps})",
                )
                self._emit_log("warning", err.message, data=err.to_dict())
                self.session.status = SessionStatus.COMPLETED

        except asyncio.CancelledError:
            self._emit_log("info", "Agent loop cancelled")
            self.session.status = SessionStatus.COMPLETED
        except Exception as e:
            err = self._make_structured_error(
                step=len(self.session.steps),
                action="loop",
                errorCode="fatal_error",
                message=f"Fatal error: {e}",
            )
            self._emit_log("error", err.message, data=err.to_dict())
            self.session.status = SessionStatus.ERROR

        self._emit_log("info",
            f"Finished — status: {self.session.status.value}, steps: {len(self.session.steps)}")
        return self.session

    # ── Computer Use engine delegation ────────────────────────────────────

    async def _run_computer_use_engine(self) -> AgentSession:
        """Delegate the entire task to the native CU protocol engine.

        The CU engine runs its own perceive→act→screenshot loop using the
        structured ``computer_use`` tool from Gemini or ``computer_20250124``
        from Claude — no text parsing needed.
        """
        from backend.engines.computer_use_engine import (
            ComputerUseEngine,
            CUTurnRecord,
            Environment,
            Provider,
        )
        from backend.agent.prompts import get_system_prompt

        self._emit_log("info", "Delegating to native Computer Use engine")

        # Map provider string → CU Provider enum
        provider_map = {"google": Provider.GEMINI, "anthropic": Provider.CLAUDE}
        cu_provider = provider_map.get(self._provider)
        if cu_provider is None:
            self._emit_log("error", f"Unsupported CU provider: {self._provider}")
            self.session.status = SessionStatus.ERROR
            return self.session

        # Map mode string → CU Environment enum
        env_map = {"browser": Environment.BROWSER, "desktop": Environment.DESKTOP}
        cu_env = env_map.get(self._mode, Environment.BROWSER)

        system_instruction = get_system_prompt("computer_use", self._mode)

        engine = ComputerUseEngine(
            provider=cu_provider,
            api_key=self._api_key,
            model=self.session.model,
            environment=cu_env,
            screen_width=config.screen_width,
            screen_height=config.screen_height,
            system_instruction=system_instruction,
            container_name=config.container_name,
            agent_service_url=config.agent_service_url,
        )

        # For browser mode, acquire a Playwright page from the agent service
        page = None
        if cu_env == Environment.BROWSER:
            page = await self._acquire_playwright_page()
            if page is None:
                self._emit_log("error", "Failed to acquire Playwright page for CU engine")
                self.session.status = SessionStatus.ERROR
                return self.session

        # CU action name → ActionType best-effort mapping for the step timeline
        _CU_ACTION_MAP = {
            "click_at": ActionType.CLICK, "double_click": ActionType.DOUBLE_CLICK,
            "right_click": ActionType.RIGHT_CLICK, "triple_click": ActionType.CLICK,
            "hover_at": ActionType.HOVER, "type_text_at": ActionType.TYPE,
            "type_at_cursor": ActionType.TYPE, "key_combination": ActionType.KEY,
            "scroll_document": ActionType.SCROLL, "scroll_at": ActionType.SCROLL,
            "drag_and_drop": ActionType.DRAG, "navigate": ActionType.OPEN_URL,
            "open_web_browser": ActionType.OPEN_URL, "search": ActionType.OPEN_URL,
            "go_back": ActionType.GO_BACK, "go_forward": ActionType.GO_FORWARD,
            "wait_5_seconds": ActionType.WAIT,
        }

        def _on_turn(record: CUTurnRecord) -> None:
            """Map CU turn records to session step records + broadcast."""
            # Build an AgentAction from the first CU action in this turn
            agent_action = None
            if record.actions:
                first = record.actions[0]
                action_type = _CU_ACTION_MAP.get(first.name)
                if action_type:
                    agent_action = AgentAction(
                        action=action_type,
                        reasoning=record.model_text[:500] if record.model_text else None,
                    )
                    # Attach coordinates/text from extra data if available
                    px = first.extra.get("pixel_x")
                    py = first.extra.get("pixel_y")
                    if px is not None and py is not None:
                        agent_action.coordinates = [px, py]
                    if first.extra.get("text"):
                        agent_action.text = str(first.extra["text"])
                else:
                    # Unknown CU action — log it but still record the step
                    self._emit_log(
                        "warning",
                        f"Unmapped CU action '{first.name}' — not in ActionType enum",
                    )
            step = StepRecord(
                step_number=record.turn,
                screenshot_b64=record.screenshot_b64,
                raw_model_response=record.model_text,
                action=agent_action,
            )
            self.session.steps.append(step)
            self._fire_callback(self._on_step, step)
            if record.screenshot_b64 and self._on_screenshot:
                self._fire_callback(self._on_screenshot, record.screenshot_b64)

        def _on_log(level: str, message: str) -> None:
            self._emit_log(level, message)

        def _on_safety(explanation: str) -> bool:
            """Safety confirmation callback for CU require_confirmation.

            Broadcasts the safety prompt via WebSocket and waits for user
            response.  Falls back to DENY (False) if no response within
            30 seconds — this satisfies the TOS requirement to never
            silently proceed on require_confirmation.
            """
            self._emit_log(
                "warning",
                f"Safety confirmation required: {explanation}",
                data={"type": "safety_confirmation", "explanation": explanation,
                      "session_id": self.session.session_id},
            )
            # In a production system this would block on an asyncio.Event
            # that the /api/agent/safety-confirm endpoint sets.  For now
            # we conservatively deny — the user sees the log and can retry.
            return False

        try:
            final_text = await engine.execute_task(
                goal=self.session.task,
                page=page,
                turn_limit=self.session.max_steps,
                on_safety=_on_safety,
                on_turn=_on_turn,
                on_log=_on_log,
            )
            self._emit_log("info", f"CU engine completed: {final_text[:300]}")
            self.session.status = SessionStatus.COMPLETED
        except Exception as exc:
            self._emit_log("error", f"CU engine failed: {exc}")
            self.session.status = SessionStatus.ERROR
        finally:
            await self._cleanup_playwright()

        return self.session

    async def _acquire_playwright_page(self):
        """Acquire a Playwright page for the CU browser engine.

        Connects to the agent_service's Chromium browser via CDP.  The
        container's Playwright instance is launched with
        ``--remote-debugging-port=9223`` so the host backend can attach
        to it.  If the agent_service exposes ``cdp_url`` in its health
        response we use that; otherwise we construct a default from the
        well-known debugging port.

        Returns the Page object or None on failure.
        """
        try:
            import httpx

            # 1. Ask agent_service for a CDP URL (may or may not be present)
            cdp_url: str | None = None
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{config.agent_service_url}/health")
                    health = resp.json()
                    cdp_url = health.get("cdp_url")
            except Exception as exc:
                self._emit_log("warning", f"Agent service health check failed: {exc}")

            # 2. Fallback: well-known debugging endpoint on container
            if not cdp_url:
                cdp_url = f"http://127.0.0.1:9223"
                self._emit_log(
                    "info",
                    "No cdp_url in health response, trying default "
                    f"debugging endpoint: {cdp_url}",
                )

            # 3. Connect via CDP
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            self._pw = pw
            try:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
            except Exception as cdp_exc:
                self._emit_log("warning", f"CDP connect failed ({cdp_exc}), launching local browser")
                browser = await pw.chromium.launch(headless=True)
            self._browser = browser

            contexts = browser.contexts
            if contexts:
                pages = contexts[0].pages
                if pages:
                    self._emit_log("info", "Acquired Playwright page via CDP")
                    return pages[0]
                page = await contexts[0].new_page()
            else:
                ctx = await browser.new_context()
                self._context = ctx
                page = await ctx.new_page()
            self._emit_log("info", "Created new Playwright page")
            return page

        except Exception as exc:
            self._emit_log("error", f"Failed to acquire Playwright page: {exc}")
            return None

    async def _cleanup_playwright(self) -> None:
        """Close Playwright context, browser, and process safely."""
        for label, obj in [
            ("context", self._context),
            ("browser", self._browser),
            ("playwright", self._pw),
        ]:
            if obj is None:
                continue
            try:
                if label == "playwright":
                    await obj.stop()
                else:
                    await obj.close()
            except Exception:
                logger.debug("Error closing %s", label, exc_info=True)
        self._context = None
        self._browser = None
        self._pw = None

    async def _execute_step(self, step_num: int) -> StepRecord:
        """Execute a single perceive → think → act cycle."""
        step = StepRecord(step_number=step_num)
        self._task_state.advance()

        # ── Early termination guard ───────────────────────────────────────
        # If the structured state indicates all required data has been
        # collected, skip the expensive screenshot/think/act cycle and
        # return a synthetic done action immediately.
        if self._task_state.complete:
            self._emit_log(
                "info",
                f"Step {step_num}: Task state complete — returning collected results",
            )
            done_action = AgentAction(
                action=ActionType.DONE,
                reasoning=f"Task completed (auto-finish). {self._task_state.summary()}",
            )
            step.action = done_action
            self._action_history.append(done_action)
            return step

        # ── 1. PERCEIVE: Capture screenshot ───────────────────────────────
        self._emit_log("info", f"Step {step_num}: Capturing screenshot...")
        try:
            screenshot_b64 = await capture_screenshot(mode=self._mode, engine=self._engine)
            step.screenshot_b64 = screenshot_b64
            self._fire_callback(self._on_screenshot, screenshot_b64)
        except Exception as e:
            step.error = f"Screenshot failed: {e}"
            self._emit_log("error", step.error)
            return step

        # ── 2. THINK: Query model ─────────────────────────────────────────
        self._emit_log("info", f"Step {step_num}: Thinking...")
        try:
            action, raw_response = await query_model(
                provider=self._provider,
                api_key=self._api_key,
                model_name=self.session.model,
                task=self.session.task,
                screenshot_b64=screenshot_b64,
                action_history=self._action_history,
                step_number=step_num,
                mode=self._mode,
                system_prompt=get_system_prompt(self._engine, self._mode),
            )
            step.action = action
            step.raw_model_response = raw_response
            self._emit_log("info",
                f"Step {step_num}: → {action.action.value}"
                + (f" | {action.reasoning}" if action.reasoning else ""))
        except Exception as e:
            step.error = f"Model query failed: {e}"
            self._emit_log("error", step.error)
            return step

        # ── 3. ACT: Execute action ────────────────────────────────────────
        if action.action not in (ActionType.DONE, ActionType.ERROR):
            self._emit_log("info", f"Step {step_num}: Executing {action.action.value}...")
            try:
                result = await execute_action(action, mode=self._mode, engine=self._engine, step=step_num, execution_target=self._execution_target)
                if result.get("success"):
                    self._emit_log("info", f"Step {step_num}: {result['message']}")
                else:
                    initial_error = result.get("message", "Unknown execution error")
                    if self._is_retryable_failure(action, result):
                        self._emit_log("warning", f"Step {step_num}: Action failed: {initial_error}")
                        self._emit_log("info", f"Step {step_num}: Retrying once...")
                        retry_result = await execute_action(action, mode=self._mode, engine=self._engine, step=step_num, execution_target=self._execution_target)
                        if retry_result.get("success"):
                            self._emit_log("info", f"Step {step_num}: Retry succeeded: {retry_result['message']}")
                        else:
                            retry_error = retry_result.get("message", "Unknown execution error")
                            step.error = f"initial failure: {initial_error} | retry failure: {retry_error}"
                            self._emit_log("warning", f"Step {step_num}: {step.error}")
                    else:
                        step.error = initial_error
                        self._emit_log("warning", f"Step {step_num}: {step.error}")
            except Exception as e:
                step.error = f"Execution error: {e}"
                self._emit_log("error", step.error)

        # Enrich action with execution result so the model can see it in
        # the action history (critical for evaluate_js / get_text results).
        if action.action not in (ActionType.DONE, ActionType.ERROR):
            result_msg = ""
            if step.error:
                result_msg = f"[FAILED: {step.error[:200]}]"
            elif 'result' in locals() and isinstance(result, dict):
                result_msg = result.get("message", "")
            if result_msg:
                # Append execution outcome to reasoning so the model sees it
                existing = action.reasoning or ""
                action.reasoning = f"{existing} → Result: {result_msg[:500]}".strip()
                # Track for duplicate result detection
                self._result_cache.append(result_msg[:500])
                # Keep cache bounded
                if len(self._result_cache) > 10:
                    self._result_cache = self._result_cache[-10:]
                # Record in structured task state for early termination
                self._task_state.record_result(result_msg[:500])

        # Record in history
        self._action_history.append(action)
        return step

    def _detect_stuck(self) -> bool:
        """Check if the last N actions are identical or near-identical (agent stuck).

        Detects both exact duplicates and actions with similar coordinates
        (within a tolerance), which handles the common case of the model
        slightly adjusting click targets each retry.

        For desktop engines the check is stricter: only 2 near-identical
        actions (with a 30 px tolerance) trigger the stuck flag, because
        blind coordinate clicking against legacy X11 widget apps almost
        never self-corrects.
        """
        is_desktop = self._engine in ("omni_accessibility", "computer_use")
        window = MAX_DUPLICATE_ACTIONS_DESKTOP if is_desktop else MAX_DUPLICATE_ACTIONS
        tolerance = DESKTOP_COORD_TOLERANCE if is_desktop else 10

        if len(self._action_history) < window:
            return False
        recent = self._action_history[-window:]
        first = recent[0]

        def _coords_similar(a_coords, b_coords, tol=tolerance):
            """Check if two coordinate sets are within tolerance pixels."""
            if a_coords is None and b_coords is None:
                return True
            if a_coords is None or b_coords is None:
                return False
            if len(a_coords) != len(b_coords):
                return False
            return all(abs(a - b) <= tol for a, b in zip(a_coords, b_coords))

        return all(
            a.action == first.action
            and _coords_similar(a.coordinates, first.coordinates)
            and a.text == first.text
            for a in recent[1:]
        )

    def _detect_duplicate_results(self) -> bool:
        """Check if the last N execution results are identical.

        This catches the common loop where evaluate_js or get_text returns
        the same data repeatedly — the agent keeps re-extracting data it
        already has because the model can't remember prior results.
        """
        window = MAX_DUPLICATE_RESULTS + 1  # need N+1 items to detect N duplicates
        if len(self._result_cache) < window:
            return False
        recent = self._result_cache[-window:]
        # Only flag if the results are non-trivial (>20 chars) and all identical
        first = recent[0]
        if len(first) < 20:
            return False
        return all(r == first for r in recent[1:])

    def _build_recovery_hint(self) -> str:
        """Build a specific recovery hint based on the type of stuck action.

        Instead of a generic 'try something different', this returns
        concrete alternative actions the agent should attempt.
        """
        if not self._action_history:
            return "System: You appear stuck. Try a completely different approach."

        last = self._action_history[-1]
        action = last.action

        if action == ActionType.CLICK:
            return (
                "System: STOP clicking the same coordinates — it is not working. "
                "Switch strategy NOW. Use ONE of these alternatives:\n"
                '1. evaluate_js to click via JS: {"action":"evaluate_js","text":"document.querySelector(\'button[type=submit],input[type=submit]\').click()"}\n'
                '2. key Enter while a form field is focused: {"action":"key","text":"Enter"}\n'
                '3. scroll_to to ensure the element is in viewport, then try different coordinates.\n'
                "4. find_element to re-locate the target element."
            )

        if action == ActionType.FILL:
            return (
                "System: STOP using the same fill selector — it is wrong. "
                "Switch strategy NOW. Use ONE of these alternatives:\n"
                '1. evaluate_js to discover field names: {"action":"evaluate_js","text":"JSON.stringify([...document.querySelectorAll(\'input,textarea,select\')].map(e=>({tag:e.tagName,name:e.name,id:e.id,type:e.type})))"}\n'
                "2. Click the field at its coordinates, then use type to enter text.\n"
                "3. Use a different CSS selector (try by id, type, or placeholder)."
            )

        if action == ActionType.TYPE:
            return (
                "System: type is not working. Switch strategy NOW:\n"
                "1. Click the target field first, then try fill with a CSS selector.\n"
                "2. Use paste instead of type.\n"
                "3. Use evaluate_js to set the field value directly."
            )

        if action == ActionType.FIND_ELEMENT:
            return (
                "System: find_element keeps failing. Use ONLY the exact visible text "
                "as the target — no extra words like 'button' or 'field'. "
                "Alternatively, use evaluate_js to query the DOM directly."
            )

        if action == ActionType.EVALUATE_JS:
            return (
                "System: STOP calling evaluate_js — you already have the data. "
                "The same result has been returned multiple times. DO NOT extract "
                "again. You MUST now:\n"
                '1. Return done with a summary: {"action":"done","reasoning":"Task completed. <your structured summary>"}\n'
                "2. Or close extra tabs first, then return done.\n"
                "3. Or return error if something is genuinely incomplete.\n"
                "Review your previous action results — the extracted data is "
                "already recorded there."
            )

        if action == ActionType.GET_TEXT:
            return (
                "System: STOP calling get_text — you are extracting the same text "
                "repeatedly. The data is already in your action history. "
                "Return done now with your accumulated results."
            )

        if action == ActionType.GET_ACCESSIBILITY_TREE:
            return (
                "System: STOP calling get_accessibility_tree — you have already "
                "dumped the tree and the element information is in your action "
                "history. Use the data you already have. DO NOT dump the tree again. "
                "Either click/interact with a discovered element, or return done "
                "with the information you collected."
            )

        if action == ActionType.RUN_COMMAND:
            return (
                "System: The same run_command keeps failing. "
                "The command may not be installed or is not in the allowlist. "
                "Try an alternative: use xfce4-settings-manager instead of "
                "gnome-control-center, or use a different approach entirely."
            )

        return (
            f"System: You are stuck repeating '{action.value}'. "
            "STOP and try a completely different approach. "
            "Use evaluate_js, find_element, or a different action type."
        )

    def _fire_callback(self, cb: Optional[Callable], *args) -> None:
        """Invoke a callback, swallowing exceptions to keep the loop alive."""
        if cb:
            try:
                cb(*args)
            except Exception:
                logger.debug("Callback %r raised an exception", cb, exc_info=True)

    async def _check_playwright_session(self) -> dict:
        """Verify the agent service has an active Playwright browser session.

        Calls ``GET /health`` on the agent service inside the container.
        The health endpoint returns ``{"browser": true/false, ...}``.
        We translate ``browser: true`` into a synthetic ``session_id`` so
        the preflight gate works consistently.
        """
        import httpx

        url = f"{config.agent_service_url}/health"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Translate health response into session-preflight shape
            if data.get("browser"):
                return {"session_id": f"playwright-{self.session.session_id[:8]}"}
            return {"session_id": None, "error": "browser not initialized in agent service"}
