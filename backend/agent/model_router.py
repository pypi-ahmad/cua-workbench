"""Model router — dispatches to Gemini or Anthropic based on provider."""

from __future__ import annotations

import logging

from backend.models import AgentAction

logger = logging.getLogger(__name__)


async def query_model(
    provider: str,
    api_key: str,
    model_name: str,
    task: str,
    screenshot_b64: str,
    action_history: list[AgentAction],
    step_number: int = 1,
    mode: str = "browser",
    system_prompt: str = "",
) -> tuple[AgentAction, str]:
    """Route to the appropriate model provider.

    Args:
        provider: "google" or "anthropic"
        api_key: API key for the provider
        model_name: Model identifier
        task: User task description
        screenshot_b64: Current screenshot
        action_history: Previous actions
        step_number: Current step
        mode: Automation engine mode
        system_prompt: System prompt override

    Returns:
        (AgentAction, raw_response_text)
    """
    if provider == "anthropic":
        from backend.agent.anthropic_client import query_claude
        return await query_claude(
            api_key=api_key,
            model_name=model_name,
            task=task,
            screenshot_b64=screenshot_b64,
            action_history=action_history,
            step_number=step_number,
            mode=mode,
            system_prompt=system_prompt,
        )
    else:
        # Default: Google Gemini
        from backend.agent.gemini_client import query_gemini
        return await query_gemini(
            api_key=api_key,
            model_name=model_name,
            task=task,
            screenshot_b64=screenshot_b64,
            action_history=action_history,
            step_number=step_number,
            mode=mode,
            system_prompt=system_prompt,
        )
