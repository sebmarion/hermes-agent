"""Turn-boundary lifecycle coverage for every conversation exit path."""

from types import SimpleNamespace

import pytest

from agent import conversation_loop


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-1",
        model="test-model",
        platform="webui",
        _current_turn_id=None,
        _current_task_id=None,
    )


def test_run_conversation_closes_a_direct_return(monkeypatch):
    agent = _agent()
    calls = []

    def fake_run(*_args, **_kwargs):
        agent._current_turn_id = "turn-1"
        agent._current_task_id = "task-1"
        return {
            "final_response": "partial",
            "completed": False,
            "failed": True,
            "interrupted": False,
        }

    monkeypatch.setattr(conversation_loop, "_run_conversation", fake_run)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: calls.append((name, kwargs)) or [],
    )

    result = conversation_loop.run_conversation(agent, "hello", task_id="task-1")

    assert result["final_response"] == "partial"
    assert calls == [
        (
            "on_session_end",
            {
                "session_id": "session-1",
                "task_id": "task-1",
                "turn_id": "turn-1",
                "completed": False,
                "interrupted": False,
                "model": "test-model",
                "platform": "webui",
            },
        )
    ]


def test_run_conversation_closes_a_raised_turn_and_reraises(monkeypatch):
    agent = _agent()
    calls = []

    def fake_run(*_args, **_kwargs):
        agent._current_turn_id = "turn-2"
        agent._current_task_id = "task-2"
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr(conversation_loop, "_run_conversation", fake_run)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: calls.append((name, kwargs)) or [],
    )

    with pytest.raises(RuntimeError, match="provider disconnected"):
        conversation_loop.run_conversation(agent, "hello", task_id="task-2")

    assert calls[0][0] == "on_session_end"
    assert calls[0][1]["turn_id"] == "turn-2"
    assert calls[0][1]["completed"] is False
