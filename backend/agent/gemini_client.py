"""Gemini API integration for the Computer-Using Agent.

Sends screenshots (PNG) + task context to Gemini and parses structured
JSON action responses. Includes retry logic for transient failures.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

from google import genai
from google.genai import types

from backend.config import config
from backend.models import ActionType, AgentAction
from backend.tools.action_aliases import resolve_action

logger = logging.getLogger(__name__)


def _build_contents(
    task: str,
    screenshot_b64: str | None,
    action_history: list[AgentAction],
    step_number: int,
    snapshot_text: str | None = None,
) -> list[types.Content]:
    """Build the multi-turn contents for Gemini."""
    parts: list[types.Part] = []

    # Action history context (trimmed to last 15 for context window)
    if action_history:
        history_lines = []
        recent = action_history[-15:]
        start_idx = max(1, len(action_history) - 14)
        for i, a in enumerate(recent):
            detail = ""
            if a.action == ActionType.CLICK and a.coordinates:
                detail = f" at ({a.coordinates[0]}, {a.coordinates[1]})"
            elif a.text:
                detail = f': "{a.text[:80]}"'
            outcome = a.reasoning or ""
            # Show more of the outcome for data-bearing actions
            if a.action in (ActionType.EVALUATE_JS, ActionType.GET_TEXT):
                outcome_limit = 400
            else:
                outcome_limit = 120
            if len(outcome) > outcome_limit:
                outcome = outcome[:outcome_limit] + "..."
            history_lines.append(
                f"  Step {start_idx + i}: {a.action.value}{detail} — {outcome}"
            )
        history_text = "Previous actions (most recent last):\n" + "\n".join(history_lines)
        parts.append(types.Part.from_text(text=history_text))

    # Task + step info — adapt depending on perception mode
    if snapshot_text:
        parts.append(types.Part.from_text(
            text=(
                f"Task: {task}\n\nCurrent step: {step_number}\n\n"
                "Below is the current accessibility-tree snapshot of the browser page. "
                "Use element names, roles and refs to decide the next action.\n\n"
                f"{snapshot_text}"
            )
        ))
    else:
        parts.append(types.Part.from_text(
            text=f"Task: {task}\n\nCurrent step: {step_number}\n\nHere is the current screenshot. Decide the next action to complete the task."
        ))

        # Screenshot as inline image
        image_bytes = base64.b64decode(screenshot_b64)
        parts.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/png")
        )

    return [types.Content(role="user", parts=parts)]


def _parse_action(raw_text: str) -> AgentAction:
    """Parse the model's JSON response into an AgentAction.

    Handles markdown fences, extra text around JSON, nested braces.
    """
    cleaned = raw_text.strip()

    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?\s*```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse first
    try:
        data = json.loads(cleaned)
        return _validate_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to extract JSON object (handle nested braces)
    depth = 0
    start = None
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    data = json.loads(cleaned[start:i + 1])
                    return _validate_action(data)
                except (json.JSONDecodeError, ValueError):
                    start = None

    # ── Attempt to repair truncated JSON (response cut off mid-output) ────
    if start is not None and depth > 0:
        truncated = cleaned[start:]
        repaired = _repair_truncated_json(truncated)
        if repaired is not None:
            logger.warning("Repaired truncated JSON response (depth=%d)", depth)
            return _validate_action(repaired)

    logger.error("Failed to parse model response: %s", raw_text[:500])
    return AgentAction(
        action=ActionType.ERROR,
        reasoning=f"Failed to parse model response: {raw_text[:200]}",
    )


def _repair_truncated_json(truncated: str) -> dict | None:
    """Try to repair a JSON object that was cut off mid-output.

    Common truncation patterns:
        {"action":"click","target":"btn","coordinates":[355,438],"text":
        {"action":"fill","target":"input","text":"hello","reasoning":"fi

    Strategy: close any open strings, arrays, and braces, then parse.
    We only need the 'action' field at minimum to produce a usable action.
    """
    # Track state: are we inside a string?
    in_string = False
    escape_next = False
    in_array = 0

    for ch in truncated:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if not in_string:
            if ch == "[":
                in_array += 1
            elif ch == "]":
                in_array -= 1

    # Build suffix to close the JSON
    suffix = ""
    if in_string:
        suffix += '"'           # close the open string value
    suffix += "]" * max(0, in_array)  # close open arrays
    suffix += "}"               # close the object

    # Try a few repair variants (progressively simpler)
    candidates = [
        truncated + suffix,
        truncated + '""' + suffix,   # empty-string value: ,"key":  → ,"key":""
    ]
    # Also try stripping back to last comma (drop the incomplete field)
    last_comma = truncated.rfind(",")
    if last_comma > 0:
        candidates.append(truncated[:last_comma] + "}")

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "action" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            continue

    return None


def _validate_action(data: dict) -> AgentAction:
    """Validate and normalize parsed action data."""
    action_str = data.get("action", "error")
    # Normalize action string + resolve aliases
    action_str = resolve_action(action_str.strip().lower())
    if action_str not in {a.value for a in ActionType}:
        return AgentAction(
            action=ActionType.ERROR,
            reasoning=f"Unsupported action '{action_str}' in model response",
        )

    coords = data.get("coordinates")
    if coords and isinstance(coords, list):
        if len(coords) >= 4:
            coords = [int(c) for c in coords[:4]]
        elif len(coords) >= 2:
            coords = [int(coords[0]), int(coords[1])]
        else:
            coords = None
    else:
        coords = None

    # Truncate model output fields to prevent oversized data
    target = data.get("target")
    if target and isinstance(target, str) and len(target) > 2000:
        target = target[:2000]
    text = data.get("text")
    if text and isinstance(text, str) and len(text) > 10_000:
        text = text[:10_000]
    reasoning = data.get("reasoning")
    if reasoning and isinstance(reasoning, str) and len(reasoning) > 2000:
        reasoning = reasoning[:2000]

    # MCP-native tool_args passthrough (Playwright MCP direct path)
    tool_args = data.get("tool_args")
    if tool_args is not None and not isinstance(tool_args, dict):
        logger.warning("tool_args is not a dict (%s), ignoring", type(tool_args).__name__)
        tool_args = None

    return AgentAction(
        action=ActionType(action_str),
        target=target,
        coordinates=coords,
        text=text,
        reasoning=reasoning,
        tool_args=tool_args,
    )


async def query_gemini(
    api_key: str,
    model_name: str,
    task: str,
    screenshot_b64: str | None,
    action_history: list[AgentAction],
    step_number: int = 1,
    mode: str = "browser",
    system_prompt: str = "",
    snapshot_text: str | None = None,
) -> tuple[AgentAction, str]:
    """Send screenshot (or AX snapshot) + context to Gemini and return parsed action.

    Retries on transient API errors up to config.gemini_retry_attempts times.

    Returns:
        (AgentAction, raw_response_text)
    """
    client = genai.Client(api_key=api_key)
    contents = _build_contents(task, screenshot_b64, action_history, step_number, snapshot_text=snapshot_text)
    # Use provided system_prompt (from prompts.py); error if missing
    if not system_prompt:
        raise ValueError(f"system_prompt is required for query_gemini (mode={mode!r})")

    generation_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.1,
        max_output_tokens=4096,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    )

    last_error = None
    for attempt in range(config.gemini_retry_attempts):
        try:
            logger.info("Querying Gemini model=%s (attempt %d)", model_name, attempt + 1)

            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=generation_config,
            )

            raw_text = response.text or ""
            logger.debug("Gemini raw response: %s", raw_text[:500])

            if not raw_text.strip():
                last_error = "Empty response from Gemini"
                logger.warning("Empty Gemini response (attempt %d)", attempt + 1)
                await asyncio.sleep(config.gemini_retry_delay)
                continue

            action = _parse_action(raw_text)

            # If parsing returned an error, retry if we have attempts left
            if action.action == ActionType.ERROR and "parse" in (action.reasoning or "").lower():
                if attempt < config.gemini_retry_attempts - 1:
                    logger.warning("Parse error, retrying: %s", action.reasoning)
                    await asyncio.sleep(config.gemini_retry_delay)
                    continue

            return action, raw_text

        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini API error (attempt %d): %s", attempt + 1, e)
            if attempt < config.gemini_retry_attempts - 1:
                await asyncio.sleep(config.gemini_retry_delay)

    return AgentAction(
        action=ActionType.ERROR,
        reasoning=f"Gemini failed after {config.gemini_retry_attempts} attempts: {last_error}",
    ), f"ERROR: {last_error}"
