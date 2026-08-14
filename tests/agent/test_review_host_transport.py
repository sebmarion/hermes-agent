from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("scope", ("", "agent/core.py tests/agent/test_core.py"))
def test_manual_review_config_invokes_canonical_adapter_before_turn_setup(
    monkeypatch,
    scope,
):
    from agent import conversation_loop, review_engine

    history = [{"role": "user", "content": "earlier context"}]
    expected = {
        "final_response": "review passed",
        "messages": [],
        "api_calls": 0,
        "completed": True,
    }
    calls = []

    def fake_manual_review_request(agent, *, scope, conversation_history):
        calls.append(
            {
                "agent": agent,
                "scope": scope,
                "conversation_history": conversation_history,
            }
        )
        return expected

    def fail_if_turn_setup_starts(*_args, **_kwargs):
        raise AssertionError("manual review reached normal model/skill turn setup")

    agent = SimpleNamespace()
    monkeypatch.setattr(
        review_engine,
        "run_manual_review_request",
        fake_manual_review_request,
        raising=False,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        fail_if_turn_setup_starts,
    )

    result = conversation_loop.run_conversation(
        agent,
        "/review " + scope,
        conversation_history=history,
        review_config={"scope": scope},
    )

    assert result is expected
    assert calls == [
        {
            "agent": agent,
            "scope": scope,
            "conversation_history": history,
        }
    ]
    assert calls[0]["conversation_history"] is not history


def test_normal_turn_does_not_invoke_manual_review_adapter(monkeypatch):
    from agent import conversation_loop, review_engine

    class OrdinaryTurnReached(Exception):
        pass

    adapter_calls = []
    turn_messages = []

    monkeypatch.setattr(
        review_engine,
        "run_manual_review_request",
        lambda *_args, **_kwargs: adapter_calls.append(True),
        raising=False,
    )

    def ordinary_turn(_agent, message, *_args, **_kwargs):
        turn_messages.append(message)
        raise OrdinaryTurnReached

    monkeypatch.setattr(conversation_loop, "build_turn_context", ordinary_turn)

    with pytest.raises(OrdinaryTurnReached):
        conversation_loop.run_conversation(SimpleNamespace(), "ordinary message")

    assert turn_messages == ["ordinary message"]
    assert adapter_calls == []


@pytest.mark.parametrize(
    "review_config",
    (
        {},
        {"scope": 7},
        {"scope": "agent/", "extra": True},
        [],
    ),
)
def test_manual_review_config_rejects_invalid_metadata(monkeypatch, review_config):
    from agent import conversation_loop, review_engine

    adapter_calls = []
    monkeypatch.setattr(
        review_engine,
        "run_manual_review_request",
        lambda *_args, **_kwargs: adapter_calls.append(True),
        raising=False,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        lambda *_args, **_kwargs: pytest.fail("invalid metadata reached turn setup"),
    )

    with pytest.raises(ValueError, match="invalid review configuration"):
        conversation_loop.run_conversation(
            SimpleNamespace(),
            "/review",
            review_config=review_config,
        )

    assert adapter_calls == []


def test_aiagent_forwards_trusted_review_config(monkeypatch):
    from agent import relay_runtime
    from run_agent import AIAgent

    captured = {}
    expected = {
        "final_response": "review passed",
        "messages": [],
        "api_calls": 0,
        "completed": True,
    }

    class Coordinator:
        def register_session_initializer(self, *_args, **_kwargs):
            return None

        def acquire_conversation(self, **_kwargs):
            return object()

        def begin_turn(self, *_args, **_kwargs):
            return SimpleNamespace(relay_enabled=False)

        def finish_logical_calls(self, *_args, **_kwargs):
            return None

        def end_turn(self, *_args, **_kwargs):
            return None

        def release_conversation(self, *_args, **_kwargs):
            return None

    def fake_conversation_loop(*_args, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(relay_runtime, "SESSION_COORDINATOR", Coordinator())
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        fake_conversation_loop,
    )

    agent = object.__new__(AIAgent)
    agent.session_id = "review-session"
    agent.platform = "cli"
    agent._parent_session_id = None
    agent._session_db = None
    review_config = {"scope": "agent/core.py"}

    result = AIAgent.run_conversation(
        agent,
        "/review agent/core.py",
        conversation_history=[],
        task_id="review-task",
        review_config=review_config,
    )

    assert result is expected
    assert captured["review_config"] is review_config
