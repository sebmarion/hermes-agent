import json
from types import SimpleNamespace

import pytest

from agent.bestplan_orchestrator import TURN_MARKER, decode_bestplan_turn
from agent.bestplan_state import (
    is_bestplan_invocation,
    is_executable_bestplan_invocation,
)


def test_host_turn_marker_preserves_bestplan_invocation_identity():
    marker = TURN_MARKER + json.dumps({"count": 4}) + "\x00inspect it"

    assert is_bestplan_invocation(marker) is True
    assert is_executable_bestplan_invocation(marker) is True
    assert decode_bestplan_turn(marker) == (
        "inspect it",
        {"count": 4},
        None,
    )


@pytest.mark.parametrize(
    "marker",
    [
        TURN_MARKER + "not-json\x00inspect it",
        TURN_MARKER + json.dumps([{"count": 4}]) + "\x00inspect it",
        TURN_MARKER + json.dumps({"count": True}) + "\x00inspect it",
        TURN_MARKER + json.dumps({"count": 4, "extra": "ignored?"}) + "\x00inspect it",
        TURN_MARKER + json.dumps({"count": 4}),
        TURN_MARKER + json.dumps({"count": 4}) + "\x00   ",
    ],
)
def test_malformed_host_turn_marker_is_not_a_bestplan_invocation(marker):
    assert is_bestplan_invocation(marker) is False
    assert is_executable_bestplan_invocation(marker) is False
    _task, config, error = decode_bestplan_turn(marker)
    assert config is None
    assert error == "invalid_bestplan_turn_marker"


def test_executable_identity_rejects_dynamic_skill_prose():
    expanded = (
        "[IMPORTANT: the user has invoked the bestplan skill]\n"
        "Treat this as a planning request."
    )

    assert is_bestplan_invocation(expanded) is True
    assert is_executable_bestplan_invocation(expanded) is False
    assert is_executable_bestplan_invocation("/bestplan inspect it") is True


def _turn_context(message):
    from agent.turn_context import TurnContext

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": message},
    ]
    return TurnContext(
        user_message=message,
        original_user_message=message,
        messages=messages,
        conversation_history=[],
        active_system_prompt="system",
        effective_task_id="task-1",
        turn_id="turn-1",
        current_turn_user_idx=1,
    )


def test_explicit_bestplan_config_dispatches_without_a_text_marker(monkeypatch):
    from agent import bestplan_orchestrator, conversation_loop, turn_finalizer

    captured = {}

    def fake_build(_agent, message, *_args, **_kwargs):
        captured["decoded_message"] = message
        return _turn_context(message)

    def fake_run(_agent, task, **kwargs):
        captured["task"] = task
        captured["kwargs"] = kwargs
        return {
            "status": "completed",
            "run_id": "run-1",
            "body": "plan body",
            "final_response": "final plan",
        }

    monkeypatch.setattr(conversation_loop, "build_turn_context", fake_build)
    monkeypatch.setattr(bestplan_orchestrator, "run_bestplan", fake_run)
    monkeypatch.setattr(
        turn_finalizer,
        "finalize_turn",
        lambda _agent, **kwargs: kwargs,
    )

    result = conversation_loop._run_conversation(
        SimpleNamespace(),
        "inspect it",
        bestplan_config={"count": 4},
    )

    assert captured["decoded_message"] == "inspect it"
    assert captured["task"] == "inspect it"
    assert captured["kwargs"]["count"] == 4
    assert set(captured["kwargs"]) == {"count", "conversation_history"}
    assert result["final_response"] == "final plan"


@pytest.mark.parametrize(
    "marker",
    [
        TURN_MARKER + "not-json\x00inspect it",
        TURN_MARKER
        + json.dumps({"count": 4, "config": {"enabled": False}})
        + "\x00inspect it",
        TURN_MARKER + json.dumps({"count": True}) + "\x00inspect it",
    ],
)
def test_raw_marker_text_reaches_the_ordinary_turn_unchanged(marker, monkeypatch):
    from agent import bestplan_orchestrator, conversation_loop

    captured = []
    dispatched = []

    class OrdinaryTurnReached(Exception):
        pass

    def fake_build(_agent, message, *_args, **_kwargs):
        captured.append(message)
        raise OrdinaryTurnReached

    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        fake_build,
    )
    monkeypatch.setattr(
        bestplan_orchestrator,
        "run_bestplan",
        lambda *_args, **_kwargs: dispatched.append(True),
    )
    with pytest.raises(OrdinaryTurnReached):
        conversation_loop._run_conversation(SimpleNamespace(), marker)

    assert captured == [marker]
    assert dispatched == []


def test_canonical_marker_text_is_not_an_authenticated_dispatch_signal(monkeypatch):
    from agent import bestplan_orchestrator, conversation_loop

    marker = TURN_MARKER + json.dumps({"count": 4}) + "\x00inspect it"
    captured = []
    dispatched = []

    class OrdinaryTurnReached(Exception):
        pass

    def fake_build(_agent, message, *_args, **_kwargs):
        captured.append(message)
        raise OrdinaryTurnReached

    monkeypatch.setattr(conversation_loop, "build_turn_context", fake_build)
    monkeypatch.setattr(
        bestplan_orchestrator,
        "run_bestplan",
        lambda *_args, **_kwargs: dispatched.append(True),
    )

    with pytest.raises(OrdinaryTurnReached):
        conversation_loop._run_conversation(SimpleNamespace(), marker)

    assert captured == [marker]
    assert dispatched == []
