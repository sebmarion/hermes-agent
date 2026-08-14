import queue
from unittest.mock import patch

import pytest

from cli import HermesCLI
from hermes_cli.commands import resolve_command, should_bypass_active_session


def _make_cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_input = queue.Queue()
    cli._pending_resume_sessions = None
    cli.session_id = "test-session"
    cli.conversation_history = []
    cli.config = {"quick_commands": {}}
    return cli


def test_review_command_is_registered_as_host_owned():
    registered = resolve_command("review")

    assert registered is not None
    assert registered.name == "review"
    assert registered.busy_policy == "reject"
    assert should_bypass_active_session("review") is True


@pytest.mark.parametrize(
    ("command", "scope"),
    (
        ("/review", ""),
        ("/review agent/review_engine.py", "agent/review_engine.py"),
    ),
)
def test_cli_review_queues_trusted_request_without_skill_expansion(command, scope):
    cli = _make_cli()

    with (
        patch("cli._ensure_skill_commands", return_value={"/review": {"name": "review"}}),
        patch("cli.get_skill_bundles", return_value={}),
        patch("cli._get_plugin_cmd_handler_names", return_value=set()),
        patch(
            "agent.skill_commands.build_skill_invocation_message",
            return_value="legacy review skill prompt",
        ) as build_skill,
    ):
        assert cli.process_command(command) is True

    build_skill.assert_not_called()
    queued = cli._pending_input.get_nowait()
    assert queued == (
        scope,
        [],
        {"kind": "review", "config": {"scope": scope}},
    )


def test_cli_chat_forwards_trusted_review_config():
    from tests.cli.test_cli_interrupt_ack_race import (
        _StubAgent,
        _make_cli as _make_running_cli,
    )

    cli = _make_running_cli()

    class CapturingAgent(_StubAgent):
        def __init__(self, session_id):
            super().__init__(session_id, turn_seconds=0)
            self.calls = []

        def run_conversation(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "final_response": "review complete",
                "messages": [],
                "api_calls": 0,
                "completed": True,
                "partial": True,
                "response_previewed": True,
            }

    agent = CapturingAgent(cli.session_id)
    cli.agent = agent
    review_config = {"scope": "go"}

    with (
        patch.object(cli, "_ensure_runtime_credentials", return_value=True),
        patch.object(
            cli,
            "_resolve_turn_agent_config",
            return_value={
                "signature": cli._active_agent_route_signature,
                "model": None,
                "runtime": None,
                "request_overrides": None,
            },
        ),
        patch.object(cli, "_init_agent", return_value=True),
        patch("agent.bestplan_state.try_resolve_go") as resolve_go,
        patch(
            "agent.bestplan_local_push.try_resolve_local_push"
        ) as resolve_local_push,
        patch(
            "agent.autonomy_shadow.submit_shadow_observation"
        ) as submit_shadow,
    ):
        assert cli._should_route_pending_input_controls(
            "go",
            bestplan_config=None,
            review_config=review_config,
        ) is False
        cli.chat("go", review_config=review_config)

    assert len(agent.calls) == 1
    assert agent.calls[0]["user_message"] == "go"
    assert agent.calls[0]["review_config"] == {"scope": "go"}
    resolve_go.assert_not_called()
    resolve_local_push.assert_not_called()
    submit_shadow.assert_not_called()


def test_cli_chat_ordinary_turn_omits_review_config():
    from tests.cli.test_cli_interrupt_ack_race import (
        _StubAgent,
        _make_cli as _make_running_cli,
    )

    cli = _make_running_cli()

    class CapturingAgent(_StubAgent):
        def __init__(self, session_id):
            super().__init__(session_id, turn_seconds=0)
            self.calls = []

        def run_conversation(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "final_response": "ordinary response",
                "messages": [],
                "api_calls": 1,
                "completed": True,
                "partial": True,
                "response_previewed": True,
            }

    agent = CapturingAgent(cli.session_id)
    cli.agent = agent

    with (
        patch.object(cli, "_ensure_runtime_credentials", return_value=True),
        patch.object(
            cli,
            "_resolve_turn_agent_config",
            return_value={
                "signature": cli._active_agent_route_signature,
                "model": None,
                "runtime": None,
                "request_overrides": None,
            },
        ),
        patch.object(cli, "_init_agent", return_value=True),
        patch("agent.bestplan_state.try_resolve_go", return_value=None),
        patch("agent.autonomy_shadow.submit_shadow_observation"),
    ):
        assert cli._should_route_pending_input_controls(
            "ordinary message",
            bestplan_config=None,
            review_config=None,
        ) is True
        cli.chat("ordinary message")

    assert len(agent.calls) == 1
    assert "review_config" not in agent.calls[0]
