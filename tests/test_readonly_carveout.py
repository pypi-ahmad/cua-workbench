"""Lock-in for read-only carve-out in MAX_DUPLICATE_RESULTS (F-031 / I-023)."""

from __future__ import annotations

from backend.models import ActionType, AgentAction


def _make_loop():
    """Construct an AgentLoop without running __init__'s heavy setup."""
    from backend.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._action_history = []
    loop._result_cache = []
    return loop


def _push(loop, action: ActionType, result: str) -> None:
    loop._action_history.append(AgentAction(action=action, reasoning="t"))
    loop._result_cache.append(result)


def test_three_identical_readonly_results_does_not_abort() -> None:
    """Below the read-only cap of 5, identical reads must not trigger."""
    loop = _make_loop()
    payload = "x" * 50
    for _ in range(3):
        _push(loop, ActionType.GET_TEXT, payload)
    assert loop._detect_duplicate_results() is False


def test_six_identical_readonly_results_does_abort() -> None:
    """Above the cap (5 consecutive), the detector must fire."""
    loop = _make_loop()
    payload = "x" * 50
    for _ in range(6):
        _push(loop, ActionType.GET_TEXT, payload)
    assert loop._detect_duplicate_results() is True


def test_two_identical_mutating_results_aborts() -> None:
    """Mutating actions still abort at the original tight cap (2)."""
    loop = _make_loop()
    payload = "x" * 50
    for _ in range(3):
        _push(loop, ActionType.CLICK, payload)
    assert loop._detect_duplicate_results() is True


def test_short_results_never_abort() -> None:
    """Trivial result strings (<20 chars) are ignored entirely."""
    loop = _make_loop()
    for _ in range(6):
        _push(loop, ActionType.GET_TEXT, "ok")
    assert loop._detect_duplicate_results() is False


def test_is_read_only_helper() -> None:
    from backend.tools.unified_schema import is_read_only_action

    assert is_read_only_action("get_text") is True
    assert is_read_only_action("screenshot") is True
    assert is_read_only_action("find_elements") is True
    assert is_read_only_action("click") is False
    assert is_read_only_action("type") is False
    assert is_read_only_action(ActionType.GET_TEXT) is True
    assert is_read_only_action(ActionType.CLICK) is False
