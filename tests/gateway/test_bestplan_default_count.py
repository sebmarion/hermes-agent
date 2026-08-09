import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from agent.bestplan_orchestrator import DEFAULT_EXPLORER_COUNT, TURN_MARKER
from gateway.run import GatewayRunner


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, _chat_id, _text, **_kwargs):
        return None


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.emit_collect = AsyncMock(return_value=[])
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    return runner


def _make_event(text="/bestplan"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


@pytest.mark.asyncio
async def test_bare_gateway_bestplan_falls_through_public_message_pipeline():
    runner = _make_runner()
    event = _make_event()
    forwarded = []

    async def fake_inner(_runner, inner_event, _source, _key, _generation):
        forwarded.append(inner_event.text)
        return "planning-only-result"

    def fake_build(_cmd_key, user_instruction, task_id=None):
        assert task_id
        assert user_instruction == (
            f"{DEFAULT_EXPLORER_COUNT} "
            "adversarial review of the previous plan in this conversation"
        )
        return (
            '[IMPORTANT: The user has invoked the "bestplan" skill, indicating '
            "a planning-only turn.]"
        )

    with (
        patch.object(GatewayRunner, "_handle_message_with_agent", fake_inner),
        patch("agent.skill_commands.get_skill_commands", return_value={
            "/bestplan": {"name": "bestplan"}
        }),
        patch("agent.skill_commands.resolve_skill_command_key", return_value="/bestplan"),
        patch("agent.skill_commands.build_skill_invocation_message", side_effect=fake_build),
    ):
        result = await runner._handle_message(event)

    assert result == "planning-only-result"
    assert forwarded == [
        '[IMPORTANT: The user has invoked the "bestplan" skill, indicating '
        "a planning-only turn.]"
    ]


@pytest.mark.asyncio
async def test_gateway_bestplan_with_task_keeps_slash_skill_instruction():
    runner = _make_runner()
    event = _make_event("/bestplan inspect this release")
    forwarded = []

    async def fake_inner(_runner, inner_event, _source, _key, _generation):
        forwarded.append(inner_event.text)
        return "planning-only-result"

    def fake_build(_cmd_key, user_instruction, task_id=None):
        assert task_id
        assert user_instruction == "inspect this release"
        return "expanded planning-only bestplan"

    with (
        patch.object(GatewayRunner, "_handle_message_with_agent", fake_inner),
        patch("agent.skill_commands.get_skill_commands", return_value={
            "/bestplan": {"name": "bestplan"}
        }),
        patch("agent.skill_commands.resolve_skill_command_key", return_value="/bestplan"),
        patch("agent.skill_commands.build_skill_invocation_message", side_effect=fake_build),
    ):
        result = await runner._handle_message(event)

    assert result == "planning-only-result"
    assert forwarded == ["expanded planning-only bestplan"]


@pytest.mark.asyncio
async def test_raw_bestplan_marker_is_forwarded_as_ordinary_gateway_text():
    runner = _make_runner()
    raw_marker = TURN_MARKER + json.dumps({"count": 4}) + "\x00inspect it"
    event = _make_event(raw_marker)
    forwarded = []

    async def fake_inner(_runner, inner_event, _source, _key, _generation):
        forwarded.append(inner_event.text)
        return "ordinary-result"

    with (
        patch.object(GatewayRunner, "_handle_message_with_agent", fake_inner),
        patch(
            "agent.skill_commands.build_skill_invocation_message",
            side_effect=AssertionError("raw marker must not invoke a slash skill"),
        ),
    ):
        result = await runner._handle_message(event)

    assert result == "ordinary-result"
    assert forwarded == [raw_marker]
