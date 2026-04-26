from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.agent.openai_client import OpenAITurnResult
from backend.agent.model_router import query_model
from backend.config import config
from backend.models import ActionType


@pytest.mark.asyncio
async def test_query_model_dispatches_openai_provider() -> None:
    turn = OpenAITurnResult(
        computer_actions=[{"type": "click", "x": 128, "y": 256}],
        raw_output='[{"type":"computer_call"}]',
        message_text="Clicking the highlighted button.",
    )

    with patch.object(config, "openai_base_url", "https://us.api.openai.com/v1"), patch(
        "backend.agent.openai_client.OpenAICUClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.query = AsyncMock(return_value=turn)

        action, raw = await query_model(
            provider="openai",
            api_key="sk-test-key",
            model_name="gpt-5.4",
            task="Click the button",
            screenshot_b64="ZmFrZS1zY3JlZW5zaG90",
            action_history=[],
            step_number=3,
            mode="desktop",
            system_prompt="Use the computer tool.",
        )

    mock_client_cls.assert_called_once_with(
        api_key="sk-test-key",
        base_url="https://us.api.openai.com/v1",
        model="gpt-5.4",
    )
    mock_client.query.assert_awaited_once()
    assert action.action == ActionType.CLICK
    assert action.coordinates == [128, 256]
    assert action.reasoning == "Clicking the highlighted button."
    assert raw == '[{"type":"computer_call"}]'