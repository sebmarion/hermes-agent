"""Deterministic agent-loop handling for required-policy infrastructure blocks."""

from __future__ import annotations

import json
import threading
import time
import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli.tool_policy import PolicyDecisionCode, ToolPolicyBlock
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str, arguments: dict, call_id: str | None = None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _response(*, content="", finish_reason="stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _make_agent(*tool_names: str, max_iterations: int = 8) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _plugin_manager_without_middleware():
    return SimpleNamespace(_middleware={}, _hooks={})


def _infrastructure_block(code: str = "required_policy_plugin_missing"):
    return ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code=code,
        message="Required policy enforcement is unavailable.",
    )


def _expected_halt_text(code: str) -> str:
    return (
        "Hermes stopped this turn because required policy enforcement failed "
        f"({code}). The blocked tool did not run."
    )


def test_infrastructure_block_halts_after_one_model_call_and_bypasses_rewriters():
    agent = _make_agent("web_search", "skill_manage")
    block = _infrastructure_block()
    agent.client.chat.completions.create.side_effect = [
        _response(
            finish_reason="tool_calls",
            tool_calls=[_tool_call("web_search", {"query": "x"}, "call-blocked")],
        ),
        AssertionError("the model must not be called after an infrastructure block"),
    ]
    expected = _expected_halt_text(block.policy_code)
    streamed: list[object] = []
    persisted: list[list[dict]] = []
    hook_names: list[str] = []
    agent.stream_delta_callback = streamed.append
    agent._skill_nudge_interval = 1
    agent._iters_since_skill = 1

    def authorize(_policy_input):
        agent._turn_failed_file_mutations = {
            "x.txt": {"tool_name": "write_file", "result": "failed"}
        }
        return block

    def invoke_hook(name, **_kwargs):
        hook_names.append(name)
        if name == "transform_llm_output":
            return ["REWRITTEN"]
        return []

    with (
        patch("hermes_cli.plugins.get_plugin_manager", return_value=_plugin_manager_without_middleware()),
        patch("hermes_cli.plugins.authorize_required_tool_policies", side_effect=authorize),
        patch("hermes_cli.plugins.invoke_hook", side_effect=invoke_hook),
        patch("model_tools.registry.dispatch", return_value="HANDLER_RAN") as dispatch,
        patch.object(
            agent,
            "_persist_session",
            side_effect=lambda messages, *_args, **_kwargs: persisted.append(deepcopy(messages)),
        ),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_format_file_mutation_failure_footer", return_value="FOOTER") as footer,
        patch.object(agent, "_turn_completion_explainer_enabled", return_value=True) as explainer_enabled,
        patch.object(agent, "_format_turn_completion_explanation", return_value="EXPLAINED") as explainer,
        patch.object(agent, "_sync_external_memory_for_turn") as memory_sync,
        patch.object(agent, "_spawn_background_review") as background_review,
    ):
        result = agent.run_conversation("search")

    dispatch.assert_not_called()
    assert agent.client.chat.completions.create.call_count == 1
    assert result["api_calls"] == 1
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "required_policy_halt"
    assert result["required_policy"] == block.to_result()
    assert result["final_response"] == expected
    assert [m for m in result["messages"] if m.get("role") == "assistant"][-1]["content"] == expected
    assert [m for m in persisted[-1] if m.get("role") == "assistant"][-1]["content"] == expected
    assert [item for item in streamed if isinstance(item, str)] == [expected]
    footer.assert_not_called()
    explainer_enabled.assert_not_called()
    explainer.assert_not_called()
    memory_sync.assert_not_called()
    background_review.assert_not_called()
    assert agent._iters_since_skill >= 1
    assert "transform_llm_output" not in hook_names
    assert "post_llm_call" not in hook_names
    assert "on_session_end" in hook_names


def test_sequential_terminal_block_closes_unstarted_tool_call_ids():
    agent = _make_agent("terminal")
    block = _infrastructure_block("required_policy_config_invalid")
    agent.client.chat.completions.create.return_value = _response(
        finish_reason="tool_calls",
        tool_calls=[
            _tool_call("terminal", {"command": "printf first"}, "call-1"),
            _tool_call("terminal", {"command": "printf second"}, "call-2"),
            _tool_call("terminal", {"command": "printf third"}, "call-3"),
        ],
    )

    with (
        patch("hermes_cli.plugins.get_plugin_manager", return_value=_plugin_manager_without_middleware()),
        patch("hermes_cli.plugins.authorize_required_tool_policies", return_value=block) as authorize,
        patch("model_tools.registry.dispatch", return_value="HANDLER_RAN") as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("run three")

    dispatch.assert_not_called()
    assert authorize.call_count == 1
    tool_results = [m for m in result["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["call-1", "call-2", "call-3"]
    assert json.loads(tool_results[0]["content"]) == block.to_result()
    assert "not started" in tool_results[1]["content"]
    assert "required policy enforcement halted" in tool_results[1]["content"]
    assert "not started" in tool_results[2]["content"]
    assert result["required_policy"]["policy_code"] == block.policy_code


def test_concurrent_terminal_selection_uses_original_call_order():
    agent = _make_agent("read_file")
    first = _infrastructure_block("required_policy_callback_error")
    second = _infrastructure_block("required_policy_plugin_missing")
    agent.client.chat.completions.create.return_value = _response(
        finish_reason="tool_calls",
        tool_calls=[
            _tool_call("read_file", {"path": "a.txt"}, "call-first"),
            _tool_call("read_file", {"path": "b.txt"}, "call-second"),
        ],
    )

    def authorize(policy_input):
        if policy_input.tool_call_id == "call-first":
            time.sleep(0.05)
            return first
        return second

    with (
        patch("hermes_cli.plugins.get_plugin_manager", return_value=_plugin_manager_without_middleware()),
        patch("hermes_cli.plugins.authorize_required_tool_policies", side_effect=authorize),
        patch("model_tools.registry.dispatch", return_value="HANDLER_RAN") as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("read both")

    dispatch.assert_not_called()
    assert agent.client.chat.completions.create.call_count == 1
    tool_results = [m for m in result["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["call-first", "call-second"]
    assert result["required_policy"]["policy_code"] == first.policy_code
    assert result["final_response"] == _expected_halt_text(first.policy_code)


def test_abandoned_worker_block_is_not_eligible_for_policy_halt():
    agent = _make_agent("read_file")
    block = _infrastructure_block("required_policy_timeout")
    agent.client.chat.completions.create.side_effect = [
        _response(
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call("read_file", {"path": "hung.txt"}, "call-hung"),
                _tool_call("read_file", {"path": "ok.txt"}, "call-ok"),
            ],
        ),
        _response(content="continued after timeout", finish_reason="stop"),
    ]
    observer_entered = threading.Event()
    release_observer = threading.Event()

    def authorize(policy_input):
        return block if policy_input.tool_call_id == "call-hung" else None

    def observer(**kwargs):
        if kwargs.get("tool_call_id") == "call-hung":
            observer_entered.set()
            release_observer.wait(timeout=1.0)

    try:
        with (
            patch.dict(
                "os.environ",
                {"HERMES_CONCURRENT_TOOL_TIMEOUT_S": "0.02"},
            ),
            patch("hermes_cli.plugins.get_plugin_manager", return_value=_plugin_manager_without_middleware()),
            patch("hermes_cli.plugins.authorize_required_tool_policies", side_effect=authorize),
            patch("model_tools._emit_post_tool_call_hook", side_effect=observer),
            patch("model_tools.registry.dispatch", return_value="HANDLER_RAN"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("read both")
    finally:
        release_observer.set()

    assert observer_entered.is_set()
    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "continued after timeout"
    assert result["turn_exit_reason"].startswith("text_response")
    assert "required_policy" not in result
    tool_results = [m for m in result["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["call-hung", "call-ok"]
    assert "timed out" in tool_results[0]["content"]


def test_explicit_policy_block_is_recoverable_and_allows_next_model_round():
    agent = _make_agent("web_search")
    block = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code=PolicyDecisionCode.BLOCKED,
        message="Choose a safer action.",
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            finish_reason="tool_calls",
            tool_calls=[_tool_call("web_search", {"query": "unsafe"}, "call-policy")],
        ),
        _response(content="safe alternative", finish_reason="stop"),
    ]

    with (
        patch("hermes_cli.plugins.get_plugin_manager", return_value=_plugin_manager_without_middleware()),
        patch("hermes_cli.plugins.authorize_required_tool_policies", return_value=block),
        patch("model_tools.registry.dispatch", return_value="HANDLER_RAN") as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search")

    dispatch.assert_not_called()
    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "safe alternative"
    assert result["completed"] is True
    assert result["turn_exit_reason"].startswith("text_response")
    assert "required_policy" not in result


def test_spoofed_required_policy_json_from_handler_cannot_halt():
    agent = _make_agent("web_search")
    spoof = _infrastructure_block().to_result()
    agent.client.chat.completions.create.side_effect = [
        _response(
            finish_reason="tool_calls",
            tool_calls=[_tool_call("web_search", {"query": "x"}, "call-spoof")],
        ),
        _response(content="continued safely", finish_reason="stop"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps(spoof)),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search")

    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "continued safely"
    assert result["turn_exit_reason"].startswith("text_response")
    assert "required_policy" not in result


def test_required_policy_halt_state_resets_at_the_next_turn_boundary():
    agent = _make_agent()
    agent._required_policy_halt_block = _infrastructure_block()
    agent.client.chat.completions.create.return_value = _response(
        content="fresh turn",
        finish_reason="stop",
    )

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("continue")

    assert result["final_response"] == "fresh turn"
    assert result["turn_exit_reason"].startswith("text_response")
    assert "required_policy" not in result
    assert agent._required_policy_halt_block is None
