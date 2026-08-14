import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.run import GatewayRunner
from tests.gateway.test_bestplan_default_count import _make_event, _make_runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "scope"),
    (
        ("/review", ""),
        ("/review agent/review_engine.py", "agent/review_engine.py"),
    ),
)
async def test_gateway_review_is_host_owned_before_skill_expansion(command, scope):
    runner = _make_runner()
    forwarded = []

    async def fake_inner(_runner, event, _source, _key, _generation):
        forwarded.append(getattr(event, "_review_config", None))
        return "review-result"

    build_skill = MagicMock(return_value="legacy review skill prompt")
    with (
        patch.object(GatewayRunner, "_handle_message_with_agent", fake_inner),
        patch(
            "agent.skill_commands.get_skill_commands",
            return_value={"/review": {"name": "review"}},
        ),
        patch(
            "agent.skill_commands.resolve_skill_command_key",
            return_value="/review",
        ),
        patch(
            "agent.skill_commands.build_skill_invocation_message",
            build_skill,
        ),
    ):
        result = await runner._handle_message(_make_event(command))

    assert result == "review-result"
    build_skill.assert_not_called()
    assert forwarded == [{"scope": scope}]


@pytest.mark.asyncio
async def test_gateway_review_does_not_interrupt_running_agent():
    runner = _make_runner()
    event = _make_event("/review agent/review_engine.py")
    session_key = runner._session_key_for_source(event.source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent
    runner._busy_input_mode = "interrupt"
    runner.session_store = None

    build_skill = MagicMock(return_value="legacy review skill prompt")
    with patch(
        "agent.skill_commands.build_skill_invocation_message",
        build_skill,
    ):
        result = await runner._handle_message(event)

    running_agent.interrupt.assert_not_called()
    assert result is not None
    assert "can't run mid-turn" in result
    assert "/review" in result
    build_skill.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_handler_forwards_trusted_review_config(monkeypatch, tmp_path):
    from tests.gateway.test_42039_duplicate_user_message import (
        _bootstrap,
        _event,
        _source,
    )

    runner = _bootstrap(monkeypatch, tmp_path)
    event = _event()
    event.text = "/review agent/review_engine.py"
    event._review_config = {"scope": "agent/review_engine.py"}
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "review complete",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    await runner._handle_message_with_agent(
        event,
        _source(),
        "agent:main:telegram:group:-1001:12345",
        1,
    )

    assert runner._run_agent.await_args.kwargs["review_config"] == {
        "scope": "agent/review_engine.py",
    }


@pytest.mark.asyncio
async def test_gateway_handler_ordinary_turn_omits_review_config(
    monkeypatch,
    tmp_path,
):
    from tests.gateway.test_42039_duplicate_user_message import (
        _bootstrap,
        _event,
        _source,
    )

    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ordinary response",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    await runner._handle_message_with_agent(
        _event(),
        _source(),
        "agent:main:telegram:group:-1001:12345",
        1,
    )

    assert "review_config" not in runner._run_agent.await_args.kwargs


@pytest.mark.asyncio
async def test_gateway_review_reaches_canonical_conversation_adapter(
    monkeypatch,
    tmp_path,
):
    import gateway.run as gateway_run
    from agent import review_engine
    from gateway.config import Platform
    from gateway.session import SessionSource
    from tests.gateway.test_run_progress_topics import (
        ProgressCaptureAdapter,
        _make_runner as _make_progress_runner,
    )

    calls = []

    def fake_manual_review_request(agent, *, scope, conversation_history):
        calls.append(
            {
                "agent": agent,
                "scope": scope,
                "conversation_history": conversation_history,
            }
        )
        return {
            "final_response": "review passed",
            "messages": [],
            "api_calls": 0,
            "completed": True,
        }

    class Agent:
        def __init__(self, **_kwargs):
            self.tools = []

        def run_conversation(
            self,
            message,
            conversation_history=None,
            task_id=None,
            **kwargs,
        ):
            from agent.conversation_loop import run_conversation

            return run_conversation(
                self,
                message,
                conversation_history=conversation_history,
                task_id=task_id,
                review_config=kwargs.get("review_config"),
            )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = Agent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        review_engine,
        "run_manual_review_request",
        fake_manual_review_request,
        raising=False,
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***"},
    )
    submit_shadow = MagicMock()
    monkeypatch.setattr(
        "agent.autonomy_shadow.submit_shadow_observation",
        submit_shadow,
    )

    runner = _make_progress_runner(ProgressCaptureAdapter())
    runner._get_proxy_url = lambda: "http://remote-agent.invalid"
    runner._run_agent_via_proxy = AsyncMock(
        side_effect=AssertionError("host-owned review entered proxy mode")
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="review-chat",
        chat_type="dm",
    )
    result = await runner._run_agent(
        message="/review agent/review_engine.py",
        context_prompt="",
        history=[],
        source=source,
        session_id="review-session",
        session_key="agent:main:telegram:dm:review-chat",
        review_config={"scope": "agent/review_engine.py"},
    )

    assert result["final_response"] == "review passed"
    assert len(calls) == 1
    assert calls[0]["scope"] == "agent/review_engine.py"
    submit_shadow.assert_not_called()
    runner._run_agent_via_proxy.assert_not_awaited()
