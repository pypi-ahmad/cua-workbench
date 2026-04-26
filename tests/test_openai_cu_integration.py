from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.agent.openai_client import OpenAICUClient, OpenAITurnResult
from backend.engines.computer_use_engine import CUActionResult, ComputerUseEngine


def _response(response_id: str, output: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(id=response_id, output=output)


@pytest.mark.asyncio
async def test_openai_client_uses_responses_api_computer_contract_and_replays_reasoning():
    mock_create = AsyncMock(
        side_effect=[
            _response(
                "resp_1",
                [
                    {
                        "type": "computer_call",
                        "call_id": "call_123",
                        "actions": [
                            {"type": "click", "x": 100, "y": 200},
                            {"type": "keypress", "keys": ["CTRL", "L"]},
                        ],
                    },
                    {
                        "type": "message",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Opening the address bar"}],
                    },
                    {
                        "type": "reasoning",
                        "encrypted_content": "enc_1",
                    },
                ],
            ),
            _response(
                "resp_2",
                [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Done"}],
                    },
                    {
                        "type": "reasoning",
                        "encrypted_content": "enc_2",
                    },
                ],
            ),
        ]
    )
    mock_client = SimpleNamespace(responses=SimpleNamespace(create=mock_create))

    with patch("backend.agent.openai_client._client_for", return_value=mock_client):
        client = OpenAICUClient(api_key="test-key", model="gpt-5.4")

        first = await client.query(
            task="Open example.com",
            screenshot_b64="FIRST_SCREENSHOT",
            action_history=[],
            system_prompt="System prompt",
        )
        assert first.phase == "commentary"
        assert first.call_id == "call_123"
        assert len(first.computer_actions) == 2

        first_kwargs = mock_create.await_args_list[0].kwargs
        assert first_kwargs["store"] is False
        assert first_kwargs["tools"] == [{"type": "computer"}]
        assert first_kwargs["include"] == ["reasoning.encrypted_content"]
        assert first_kwargs["parallel_tool_calls"] is False
        assert first_kwargs["truncation"] == "auto"
        assert first_kwargs["reasoning"] == {"effort": "high"}
        assert any(item.get("role") == "developer" for item in first_kwargs["input"])
        assert any(
            part.get("type") == "input_image" and part.get("detail") == "original"
            for item in first_kwargs["input"]
            if item.get("role") == "user"
            for part in item.get("content", [])
        )

        second = await client.query(
            computer_call_outputs=[
                {
                    "type": "computer_call_output",
                    "call_id": "call_123",
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": "data:image/png;base64,SECOND_SCREENSHOT",
                        "detail": "original",
                    },
                }
            ]
        )
        assert second.phase == "final_answer"
        assert second.message_text == "Done"

    second_kwargs = mock_create.await_args_list[1].kwargs
    assert second_kwargs["store"] is False
    assert any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("phase") == "commentary"
        for item in second_kwargs["input"]
    )
    assert any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_1"
        for item in second_kwargs["input"]
    )
    assert any(
        isinstance(item, dict)
        and item.get("type") == "computer_call_output"
        and item.get("call_id") == "call_123"
        for item in second_kwargs["input"]
    )


@pytest.mark.asyncio
async def test_openai_engine_executes_batched_actions_in_order_and_returns_original_detail_screenshot():
    engine = ComputerUseEngine.__new__(ComputerUseEngine)
    engine._client = MagicMock()
    engine._client.query = AsyncMock(
        side_effect=[
            OpenAITurnResult(
                computer_actions=[
                    {"type": "click", "x": 100, "y": 200},
                    {"type": "keypress", "keys": ["CTRL", "L"]},
                ],
                raw_output="{}",
                message_text="Opening the address bar",
                phase="commentary",
                call_id="call_123",
            ),
            OpenAITurnResult(
                computer_actions=[],
                raw_output="{}",
                message_text="Done",
                phase="final_answer",
                call_id=None,
            ),
        ]
    )
    engine._system_instruction = "System prompt"

    executor = AsyncMock()
    executor.capture_screenshot = AsyncMock(side_effect=[b"first", b"second"])
    executor.execute = AsyncMock(
        side_effect=[
            CUActionResult(name="click_at"),
            CUActionResult(name="key_combination"),
        ]
    )

    final_text = await engine._run_openai_loop("Open example.com", executor)

    assert final_text == "Done"
    assert executor.execute.await_args_list == [
        call("click_at", {"x": 100, "y": 200}),
        call("key_combination", {"keys": "Control+L"}),
    ]

    first_query = engine._client.query.await_args_list[0].kwargs
    assert first_query["task"] == "Open example.com"
    assert first_query["system_prompt"] == "System prompt"

    second_query = engine._client.query.await_args_list[1].kwargs
    assert second_query["computer_call_outputs"][0]["output"]["detail"] == "original"
    assert second_query["computer_call_outputs"][0]["output"]["image_url"].startswith(
        "data:image/png;base64,"
    )
