"""Anthropic Claude API integration for the Computer-Using Agent.

Sends screenshots (PNG) + task context to Claude and parses structured
JSON action responses. Mirror of gemini_client.py for the Anthropic provider.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

import anthropic

from backend.config import config
from backend.models import ActionType, AgentAction
from backend.tools.action_aliases import resolve_action

logger = logging.getLogger(__name__)


def _build_messages(
    task: str,
    screenshot_b64: str,
    action_history: list[AgentAction],
    step_number: int,
) -> list[dict]:
    """Build the messages list for Claude."""
    content_parts: list[dict] = []

    # Action history context (trimmed to last 15)
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
        content_parts.append({"type": "text", "text": history_text})

    # Task + step info
    content_parts.append({
        "type": "text",
        "text": f"Task: {task}\n\nCurrent step: {step_number}\n\nHere is the current screenshot. Decide the next action to complete the task.",
    })

    # Screenshot as base64 image
    content_parts.append({
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": screenshot_b64,
        },
    })

    return [{"role": "user", "content": content_parts}]


def _parse_action(raw_text: str) -> AgentAction:
    """Parse the model's JSON response into an AgentAction."""
    cleaned = raw_text.strip()

    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?\s*```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse
    try:
        data = json.loads(cleaned)
        return _validate_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # Extract JSON object with nested brace handling
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

    logger.error("Failed to parse Claude response: %s", raw_text[:500])
    return AgentAction(
        action=ActionType.ERROR,
        reasoning=f"Failed to parse model response: {raw_text[:200]}",
    )


def _validate_action(data: dict) -> AgentAction:
    """Validate and normalize parsed action data."""
    action_str = resolve_action(data.get("action", "error").strip().lower())

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

    return AgentAction(
        action=ActionType(action_str),
        target=target,
        coordinates=coords,
        text=text,
        reasoning=reasoning,
    )


async def query_claude(
    api_key: str,
    model_name: str,
    task: str,
    screenshot_b64: str,
    action_history: list[AgentAction],
    step_number: int = 1,
    mode: str = "browser",
    system_prompt: str = "",
) -> tuple[AgentAction, str]:
    """Send screenshot + context to Claude and return parsed action.

    Returns:
        (AgentAction, raw_response_text)
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages = _build_messages(task, screenshot_b64, action_history, step_number)

    last_error = None
    for attempt in range(config.gemini_retry_attempts):
        try:
            logger.info("Querying Claude model=%s (attempt %d)", model_name, attempt + 1)

            response = await client.messages.create(
                model=model_name,
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
                temperature=0.1,
            )

            raw_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    raw_text += block.text

            logger.debug("Claude raw response: %s", raw_text[:500])

            if not raw_text.strip():
                last_error = "Empty response from Claude"
                logger.warning("Empty Claude response (attempt %d)", attempt + 1)
                await asyncio.sleep(config.gemini_retry_delay)
                continue

            action = _parse_action(raw_text)

            if action.action == ActionType.ERROR and "parse" in (action.reasoning or "").lower():
                if attempt < config.gemini_retry_attempts - 1:
                    logger.warning("Parse error, retrying: %s", action.reasoning)
                    await asyncio.sleep(config.gemini_retry_delay)
                    continue

            return action, raw_text

        except Exception as e:
            last_error = str(e)
            logger.warning("Claude API error (attempt %d): %s", attempt + 1, e)
            if attempt < config.gemini_retry_attempts - 1:
                await asyncio.sleep(config.gemini_retry_delay)

    return AgentAction(
        action=ActionType.ERROR,
        reasoning=f"Claude failed after {config.gemini_retry_attempts} attempts: {last_error}",
    ), f"ERROR: {last_error}"
