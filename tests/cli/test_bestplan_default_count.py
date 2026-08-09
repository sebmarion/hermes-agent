import queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.bestplan_orchestrator import DEFAULT_EXPLORER_COUNT
from cli import HermesCLI


def _make_cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_input = queue.Queue()
    cli._pending_resume_sessions = None
    cli.session_id = "test-session"
    cli.conversation_history = []
    return cli


def test_cli_bestplan_without_count_defaults_to_all_four_configured_lanes():
    cli = _make_cli()

    with (
        patch("cli._ensure_skill_commands", return_value={"/bestplan": object()}),
        patch("cli.get_skill_bundles", return_value={}),
        patch(
            "agent.skill_commands.build_skill_invocation_message",
            return_value="legacy-skill-prompt",
        ),
    ):
        assert cli.process_command("/bestplan inspect it") is True

    task, images, internal_meta = cli._pending_input.get_nowait()
    assert task == "inspect it"
    assert images == []
    assert internal_meta == {
        "kind": "bestplan",
        "config": {"count": DEFAULT_EXPLORER_COUNT},
    }


def test_bare_cli_bestplan_queues_four_lane_auto_review():
    cli = _make_cli()

    with patch("cli._cprint"):
        assert cli.process_command("/bestplan") is True

    task, images, internal_meta = cli._pending_input.get_nowait()
    assert "adversarial review of the previous plan" in task.lower()
    assert images == []
    assert internal_meta == {
        "kind": "bestplan",
        "config": {"count": DEFAULT_EXPLORER_COUNT},
    }


@pytest.mark.parametrize("task", ["/status", "!cmd", "1", "go"])
def test_cli_owned_bestplan_control_shaped_tasks_reach_the_agent(task):
    from tests.cli.test_cli_interrupt_ack_race import _StubAgent, _make_cli as _make_running_cli

    cli = _make_running_cli()

    class CapturingAgent(_StubAgent):
        def __init__(self, session_id):
            super().__init__(session_id, turn_seconds=0)
            self.captured = None
            self.calls = []

        def run_conversation(self, **kwargs):
            self.captured = kwargs
            self.calls.append(kwargs)
            return {
                "final_response": "plan",
                "messages": [{"role": "assistant", "content": "plan"}],
                "api_calls": 0,
                "completed": True,
                "partial": True,
                "response_previewed": True,
            }

    agent = CapturingAgent(cli.session_id)
    cli.agent = agent
    capture_calls = []

    def fake_capture(result, **kwargs):
        capture_calls.append(kwargs)
        return result

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
        patch("agent.bestplan_state.try_resolve_go", return_value=None) as resolve_go,
        patch("agent.autonomy_shadow.submit_shadow_observation"),
        patch("cli._capture_cli_bestplan_result", side_effect=fake_capture),
    ):
        bestplan_config = {"count": DEFAULT_EXPLORER_COUNT}
        assert cli._should_route_pending_input_controls(
            task,
            bestplan_config=bestplan_config,
        ) is False
        assert cli._should_route_pending_input_controls(
            task,
            bestplan_config=None,
        ) is True
        cli.chat(task, bestplan_config=bestplan_config)

    assert agent.captured is not None
    assert len(agent.calls) == 1
    assert agent.captured["user_message"] == task
    assert agent.captured["bestplan_config"] == {
        "count": DEFAULT_EXPLORER_COUNT,
    }
    resolve_go.assert_not_called()
    assert len(capture_calls) == 1
    assert capture_calls[0]["invocation_message"] == "/bestplan"


def test_untrusted_go_keeps_existing_cli_resolver_routing():
    from tests.cli.test_cli_interrupt_ack_race import _StubAgent, _make_cli as _make_running_cli

    cli = _make_running_cli()
    agent = _StubAgent(cli.session_id, turn_seconds=0)
    agent.run_conversation = MagicMock()
    cli.agent = agent
    resolved_result = {
        "final_response": "executed pending plan",
        "messages": [{"role": "assistant", "content": "executed pending plan"}],
        "completed": True,
        "partial": True,
        "response_previewed": True,
    }
    resolved_go = SimpleNamespace(
        resolved=True,
        to_agent_result=MagicMock(return_value=resolved_result),
    )

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
        patch(
            "agent.bestplan_state.try_resolve_go",
            return_value=resolved_go,
        ) as resolve_go,
        patch("cli._capture_cli_bestplan_result") as capture_result,
    ):
        cli.chat("go")

    resolve_go.assert_called_once()
    resolved_go.to_agent_result.assert_called_once()
    agent.run_conversation.assert_not_called()
    capture_result.assert_not_called()
