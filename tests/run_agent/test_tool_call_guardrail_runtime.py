"""Runtime tests for tool-call loop guardrails."""

import json
import uuid
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(*tool_names: str, max_iterations: int = 10, config: dict | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
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
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _seed_exact_failures(agent: AIAgent, tool_name: str, args: dict, count: int = 2) -> None:
    for _ in range(count):
        agent._tool_guardrails.after_call(
            tool_name,
            args,
            json.dumps({"error": "boom"}),
            failed=True,
        )


def _hard_stop_config(**overrides) -> dict:
    cfg = {
        "tool_loop_guardrails": {
            "warnings_enabled": True,
            "hard_stop_enabled": True,
            "hard_stop_after": {
                "exact_failure": 2,
                "same_tool_failure": 8,
                "idempotent_no_progress": 5,
            },
        }
    }
    cfg["tool_loop_guardrails"].update(overrides)
    return cfg


def test_default_sequential_path_warns_repeated_exact_failure_without_blocking_execution():
    agent = _make_agent("web_search")
    args = {"query": "same"}
    _seed_exact_failures(agent, "web_search", args)
    starts = []
    progress = []
    agent.tool_start_callback = lambda *a, **k: starts.append((a, k))
    agent.tool_progress_callback = lambda *a, **k: progress.append((a, k))
    tc = _mock_tool_call("web_search", json.dumps(args), "c-soft")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    def fake_handle(name, call_args, task_id, **kwargs):
        kwargs["on_authorized"](call_args)
        return json.dumps({"error": "boom"})

    with patch("run_agent.handle_function_call", side_effect=fake_handle) as mock_hfc:
        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    mock_hfc.assert_called_once()
    assert len(starts) == 1
    assert any(event[0][0] == "tool.completed" for event in progress)
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "c-soft"
    assert "repeated_exact_failure_warning" in messages[0]["content"]
    assert "repeated_exact_failure_block" not in messages[0]["content"]
    assert agent._tool_guardrail_halt_decision is None


def test_config_enabled_hard_stop_blocks_repeated_exact_failure_before_execution():
    agent = _make_agent("web_search", config=_hard_stop_config())
    args = {"query": "same"}
    _seed_exact_failures(agent, "web_search", args)
    starts = []
    progress = []
    agent.tool_start_callback = lambda *a, **k: starts.append((a, k))
    agent.tool_progress_callback = lambda *a, **k: progress.append((a, k))
    tc = _mock_tool_call("web_search", json.dumps(args), "c-block")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value="SHOULD_NOT_RUN") as mock_hfc:
        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    mock_hfc.assert_not_called()
    assert starts == []
    assert progress == []
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "c-block"
    assert "repeated_exact_failure_block" in messages[0]["content"]


def test_sequential_after_call_appends_guidance_to_tool_result_without_extra_messages():
    agent = _make_agent("web_search")
    args = {"query": "same"}
    _seed_exact_failures(agent, "web_search", args, count=1)
    tc = _mock_tool_call("web_search", json.dumps(args), "c-warn")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    assert [m["role"] for m in messages] == ["tool"]
    assert messages[0]["tool_call_id"] == "c-warn"
    assert "Tool loop warning" in messages[0]["content"]
    assert "repeated_exact_failure_warning" in messages[0]["content"]


def test_same_tool_failure_warning_tells_model_to_recover_with_tools():
    agent = _make_agent("terminal")
    guardrails = getattr(agent, "_tool_guardrails")
    guardrails.after_call(
        "terminal",
        {"command": "bad-1"},
        json.dumps({"exit_code": 1}),
        failed=True,
    )
    guardrails.after_call(
        "terminal",
        {"command": "bad-2"},
        json.dumps({"exit_code": 1}),
        failed=True,
    )
    tc = _mock_tool_call("terminal", json.dumps({"command": "bad-3"}), "c-recover")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    content = messages[0]["content"]
    assert "same_tool_failure_warning" in content
    assert "Do not switch to text-only replies" in content
    assert "keep using tools" in content
    assert "pwd && ls -la" in content
    assert "absolute path" in content
    assert "different tool" in content


def test_config_enabled_hard_stop_concurrent_path_does_not_submit_blocked_calls_and_preserves_result_order():
    agent = _make_agent("web_search", config=_hard_stop_config())
    blocked_args = {"query": "blocked"}
    allowed_args = {"query": "allowed"}
    _seed_exact_failures(agent, "web_search", blocked_args)
    starts = []
    progress_events = []
    agent.tool_start_callback = lambda tool_call_id, name, args: starts.append((tool_call_id, name, args))
    agent.tool_progress_callback = lambda event, name, preview, args, **kw: progress_events.append((event, name, args, kw))
    calls = [
        _mock_tool_call("web_search", json.dumps(blocked_args), "c-block"),
        _mock_tool_call("web_search", json.dumps(allowed_args), "c-allow"),
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages = []
    executed = []

    def fake_handle(name, args, task_id, **kwargs):
        kwargs["on_authorized"](args)
        executed.append((name, args, kwargs["tool_call_id"]))
        return json.dumps({"ok": args["query"]})

    with patch("run_agent.handle_function_call", side_effect=fake_handle):
        agent._execute_tool_calls_concurrent(msg, messages, "task-1")

    assert executed == [("web_search", allowed_args, "c-allow")]
    assert [m["tool_call_id"] for m in messages] == ["c-block", "c-allow"]
    assert "repeated_exact_failure_block" in messages[0]["content"]
    assert json.loads(messages[1]["content"]) == {"ok": "allowed"}
    assert starts == [("c-allow", "web_search", allowed_args)]
    started_events = [event for event in progress_events if event[0] == "tool.started"]
    completed_events = [event for event in progress_events if event[0] == "tool.completed"]
    assert started_events == [("tool.started", "web_search", allowed_args, {})]
    assert len(completed_events) == 1
    assert completed_events[0][1] == "web_search"


def test_concurrent_failures_finalize_once_per_assistant_batch():
    agent = _make_agent(
        "web_search",
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    calls = [
        _mock_tool_call(
            "web_search",
            json.dumps({"query": f"q-{index}"}),
            f"c-{index}",
        )
        for index in range(4)
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages = []

    def fail(name, args, task_id, **kwargs):
        kwargs["on_authorized"](args)
        return json.dumps({"error": "boom"})

    with patch("run_agent.handle_function_call", side_effect=fail) as mock_hfc:
        agent._execute_tool_calls_concurrent(msg, messages, "task-1")
        assert mock_hfc.call_count == 4
        assert agent._tool_guardrail_halt_decision is None
        assert len(messages) == 4

        agent._execute_tool_calls_concurrent(msg, messages, "task-1")

    assert mock_hfc.call_count == 8
    assert len(messages) == 8
    assert agent._tool_guardrail_halt_decision is not None
    assert agent._tool_guardrail_halt_decision.code == "same_tool_failure_halt"
    assert agent._tool_guardrail_halt_decision.count == 2


def test_concurrent_completion_order_does_not_change_model_call_order():
    agent = _make_agent(
        "web_search",
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    calls = [
        _mock_tool_call(
            "web_search",
            json.dumps({"query": f"q-{index}"}),
            f"c-order-{index}",
        )
        for index in range(4)
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages = []
    completed = []
    barrier = Barrier(5)
    release = [Event() for _ in range(4)]
    acknowledged = [Event() for _ in range(4)]

    def reverse_completion(name, args, task_id, **kwargs):
        kwargs["on_authorized"](args)
        index = int(args["query"].split("-")[-1])
        barrier.wait(timeout=2)
        assert release[index].wait(timeout=2)
        completed.append(index)
        acknowledged[index].set()
        return json.dumps({"error": f"boom-{index}"})

    def release_in_reverse_order():
        barrier.wait(timeout=2)
        for index in (3, 2, 1, 0):
            release[index].set()
            assert acknowledged[index].wait(timeout=2)

    coordinator = Thread(target=release_in_reverse_order, daemon=True)
    coordinator.start()
    with patch("run_agent.handle_function_call", side_effect=reverse_completion):
        agent._execute_tool_calls_concurrent(msg, messages, "task-1")
    coordinator.join(timeout=2)

    assert not coordinator.is_alive()
    assert completed == [3, 2, 1, 0]
    assert [message["tool_call_id"] for message in messages] == [
        "c-order-0",
        "c-order-1",
        "c-order-2",
        "c-order-3",
    ]
    assert agent._tool_guardrails.raw_call_counts == {"web_search": 4}
    assert agent._tool_guardrail_halt_decision is None


def test_concurrent_timeout_snapshot_keeps_transcript_and_guardrail_consistent(monkeypatch):
    agent = _make_agent("web_search")
    blocker = Event()
    tc = _mock_tool_call(
        "web_search",
        json.dumps({"query": "slow"}),
        "c-timeout-snapshot",
    )
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []
    monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "0.05")

    def blocked_handle(name, args, task_id, **kwargs):
        kwargs["on_authorized"](args)
        blocker.wait(5)
        return "late-real-result"

    def snapshot_then_simulate_late_write(results):
        snapshot = list(results)
        assert snapshot == [None]
        results[0] = (
            "web_search",
            {"query": "slow"},
            "late-real-result",
            0.06,
            False,
            False,
            [],
        )
        return snapshot

    try:
        with (
            patch("run_agent.handle_function_call", side_effect=blocked_handle),
            patch(
                "agent.tool_executor._snapshot_concurrent_results",
                side_effect=snapshot_then_simulate_late_write,
            ),
        ):
            agent._execute_tool_calls_concurrent(msg, messages, "task-1")
    finally:
        blocker.set()

    assert len(messages) == 1
    assert messages[0]["tool_call_id"] == "c-timeout-snapshot"
    assert "timed out after" in messages[0]["content"]
    assert "late-real-result" not in messages[0]["content"]
    assert agent._tool_guardrails.raw_call_counts == {"web_search": 1}
    assert agent._tool_guardrails._same_tool_failure_counts == {"web_search": 1}


def test_sequential_failures_finalize_once_per_assistant_batch():
    agent = _make_agent(
        "terminal",
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    calls = [
        _mock_tool_call(
            "terminal",
            json.dumps({"command": f"bad-{index}"}),
            f"c-seq-{index}",
        )
        for index in range(4)
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages = []

    with patch(
        "run_agent.handle_function_call",
        return_value=json.dumps({"exit_code": 1}),
    ) as mock_hfc:
        agent._execute_tool_calls_sequential(msg, messages, "task-1")
        assert mock_hfc.call_count == 4
        assert agent._tool_guardrail_halt_decision is None

        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    assert mock_hfc.call_count == 8
    assert len(messages) == 8
    assert agent._tool_guardrail_halt_decision is not None
    assert agent._tool_guardrail_halt_decision.code == "same_tool_failure_halt"
    assert agent._tool_guardrail_halt_decision.count == 2


def test_plugin_pre_tool_block_wins_without_counting_as_toolguard_block():
    agent = _make_agent("web_search")
    args = {"query": "same"}
    tc = _mock_tool_call("web_search", json.dumps(args), "c-plugin")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with (
        patch("hermes_cli.plugins.resolve_pre_tool_block", return_value="plugin policy"),
        patch("run_agent.handle_function_call", return_value="SHOULD_NOT_RUN") as mock_hfc,
    ):
        agent._execute_tool_calls_sequential(msg, messages, "task-1")

    mock_hfc.assert_not_called()
    assert "plugin policy" in messages[0]["content"]
    assert agent._tool_guardrails.before_call("web_search", args).action == "allow"


def test_run_conversation_recovers_via_safe_no_effect_pivot_without_dispatching_effectful_calls():
    agent = _make_agent(
        "web_search",
        "read_file",
        "terminal",
        "mcp_unknown_reader",
        max_iterations=8,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                _mock_tool_call("web_search", '{"query":"again"}', "c-quarantine"),
                _mock_tool_call("terminal", '{"command":"touch /tmp/never"}', "c-effect"),
                _mock_tool_call("mcp_unknown_reader", '{"path":"/tmp/never"}', "c-unknown"),
                _mock_tool_call("read_file", '{"path":"/tmp/evidence"}', "c-safe"),
            ],
        ),
        _mock_response(content="recovered safely", finish_reason="stop"),
    ]
    dispatched = []

    def dispatch(name, args, task_id, **kwargs):
        kwargs["on_authorized"](args)
        dispatched.append(name)
        if name == "web_search":
            return json.dumps({"error": "boom"})
        if name == "read_file":
            return "evidence"
        raise AssertionError(f"effect-capable tool executed during recovery: {name}")

    with (
        patch("run_agent.handle_function_call", side_effect=dispatch),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("recover automatically")

    assert dispatched == ["web_search", "web_search", "read_file"]
    assert result["final_response"] == "recovered safely"
    assert result["turn_exit_reason"].startswith("text_response")
    assert result["guardrail_recovery"]["state"] == "recovered"
    assert result["guardrail_recovery"]["quarantined_tool"] == "web_search"
    assert "guardrail" not in result
    by_id = {
        message["tool_call_id"]: message["content"]
        for message in result["messages"]
        if message.get("role") == "tool"
    }
    assert "recovery_quarantined_tool_block" in by_id["c-quarantine"]
    assert "recovery_effectful_tool_block" in by_id["c-effect"]
    assert "recovery_effectful_tool_block" in by_id["c-unknown"]
    assert by_id["c-safe"] == "evidence"


def test_final_text_on_recovery_iteration_resolves_without_another_tool_call():
    agent = _make_agent(
        "web_search",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
        _mock_response(content="resolved from existing evidence", finish_reason="stop"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("answer without retrying")

    assert dispatch.call_count == 2
    assert result["api_calls"] == 3
    assert result["final_response"] == "resolved from existing evidence"
    assert result["guardrail_recovery"]["state"] == "recovered"
    assert result["guardrail_recovery"]["outcome"] == "final_text"
    assert "guardrail" not in result


def test_empty_recovery_response_halts_without_synthetic_user_retry():
    agent = _make_agent(
        "web_search",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
        _mock_response(content="", finish_reason="stop"),
        AssertionError("recovery must not mint an empty-response retry"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do not synthesize recovery users")

    assert dispatch.call_count == 2
    assert result["api_calls"] == 3
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["guardrail"]["code"] == "recovery_no_safe_alternative_halt"
    assert result["guardrail_recovery"]["state"] == "failed"
    assert not any(
        message.get("_empty_recovery_synthetic")
        for message in result["messages"]
        if isinstance(message, dict)
    )


def test_invalid_tool_during_recovery_is_blocked_once_without_model_retry():
    agent = _make_agent(
        "web_search",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("mcp_unknown_reader", '{}', "c-unknown")],
        ),
        AssertionError("invalid recovery calls must not get another model iteration"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do not retry unknown recovery calls")

    assert dispatch.call_count == 2
    assert result["api_calls"] == 3
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["guardrail"]["code"] == "recovery_no_safe_alternative_halt"
    unknown_result = next(
        message for message in result["messages"]
        if message.get("tool_call_id") == "c-unknown"
    )
    assert "recovery_effectful_tool_block" in unknown_result["content"]


def test_malformed_recovery_arguments_are_blocked_once_without_dispatch():
    agent = _make_agent(
        "web_search",
        "read_file",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("read_file", '{"path":', "c-bad-json")],
        ),
        AssertionError("malformed recovery calls must not get another model iteration"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do not dispatch malformed recovery calls")

    assert dispatch.call_count == 2
    assert result["api_calls"] == 3
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert result["guardrail"]["code"] == "recovery_no_safe_alternative_halt"
    malformed_result = next(
        message for message in result["messages"]
        if message.get("tool_call_id") == "c-bad-json"
    )
    assert "recovery_malformed_arguments_block" in malformed_result["content"]


def test_no_effect_recovery_does_not_bypass_iteration_budget():
    agent = _make_agent(
        "web_search",
        max_iterations=2,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"one"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", '{"query":"two"}', "c-2")],
        ),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("recover with no budget")

    assert dispatch.call_count == 2
    assert agent.client.chat.completions.create.call_count == 2
    assert result["api_calls"] == 2
    assert result["turn_exit_reason"] == "recovery_budget_exhausted"
    assert result["guardrail"]["code"] == "recovery_budget_exhausted"
    assert result["guardrail_recovery"]["state"] == "budget_exhausted"


def test_effect_capable_tool_threshold_stays_fail_closed_without_recovery():
    agent = _make_agent(
        "terminal",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("terminal", '{"command":"bad-1"}', "c-1")],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("terminal", '{"command":"bad-2"}', "c-2")],
        ),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do not recover mutations")

    assert dispatch.call_count == 2
    assert result["api_calls"] == 2
    assert result["guardrail"]["code"] == "same_tool_failure_halt"
    assert "guardrail_recovery" not in result


def test_multiple_no_effect_thresholds_halt_instead_of_guessing_a_pivot():
    agent = _make_agent(
        "web_search",
        "read_file",
        max_iterations=6,
        config=_hard_stop_config(
            warn_after={
                "exact_failure": 99,
                "same_tool_failure": 99,
                "idempotent_no_progress": 99,
            },
            hard_stop_after={
                "exact_failure": 99,
                "same_tool_failure": 2,
                "idempotent_no_progress": 99,
            },
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                _mock_tool_call("web_search", '{"query":"one"}', "c-w1"),
                _mock_tool_call("read_file", '{"path":"/tmp/one"}', "c-r1"),
            ],
        ),
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                _mock_tool_call("web_search", '{"query":"two"}', "c-w2"),
                _mock_tool_call("read_file", '{"path":"/tmp/two"}', "c-r2"),
            ],
        ),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as dispatch,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("two broken readers")

    assert dispatch.call_count == 4
    assert result["api_calls"] == 2
    assert result["guardrail"]["code"] == "multiple_guardrail_thresholds_halt"
    assert "guardrail_recovery" not in result


def test_default_run_conversation_warns_without_guardrail_halt():
    agent = _make_agent("web_search", max_iterations=10)
    same_args = {"query": "same"}
    responses = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", json.dumps(same_args), f"c{i}")],
        )
        for i in range(1, 4)
    ]
    responses.append(_mock_response(content="done", finish_reason="stop", tool_calls=None))
    agent.client.chat.completions.create.side_effect = responses

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as mock_hfc,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search repeatedly")

    assert mock_hfc.call_count == 3
    assert result["turn_exit_reason"].startswith("text_response")
    assert "guardrail" not in result
    assert result["final_response"] == "done"
    tool_contents = [m["content"] for m in result["messages"] if m.get("role") == "tool"]
    assert any("repeated_exact_failure_warning" in content for content in tool_contents)


def test_config_enabled_hard_stop_run_conversation_returns_controlled_guardrail_halt_without_top_level_error():
    agent = _make_agent("web_search", max_iterations=10, config=_hard_stop_config())
    same_args = {"query": "same"}
    responses = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", json.dumps(same_args), f"c{i}")],
        )
        for i in range(1, 10)
    ]
    agent.client.chat.completions.create.side_effect = responses

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})) as mock_hfc,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search repeatedly")

    assert mock_hfc.call_count == 2
    assert result["api_calls"] == 3
    assert result["api_calls"] < agent.max_iterations
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert "error" not in result
    assert result["completed"] is True
    assert "stopped retrying" in result["final_response"]
    assert result["guardrail"]["code"] == "repeated_exact_failure_block"
    assert result["guardrail"]["tool_name"] == "web_search"
    assert "guardrail_recovery" not in result

    assistant_tool_calls = [m for m in result["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    for assistant_msg in assistant_tool_calls:
        call_ids = [tc["id"] for tc in assistant_msg["tool_calls"]]
        following_results = [m for m in result["messages"] if m.get("role") == "tool" and m.get("tool_call_id") in call_ids]
        assert len(following_results) == len(call_ids)


def test_guardrail_halt_emits_final_response_through_stream_delta_callback():
    """Regression for #30770: when the guardrail halts the loop, the
    synthesized halt message must be pushed through ``stream_delta_callback``
    so SSE/TUI clients see why the agent stopped instead of a silent stream
    close.  Without this the chat-completions SSE writer drains an empty
    queue and emits a finish chunk with zero content (indistinguishable
    from a crash for Open WebUI and similar clients).
    """
    agent = _make_agent("web_search", max_iterations=10, config=_hard_stop_config())
    same_args = {"query": "same"}
    responses = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", json.dumps(same_args), f"c{i}")],
        )
        for i in range(1, 10)
    ]
    agent.client.chat.completions.create.side_effect = responses

    deltas: list = []
    agent.stream_delta_callback = lambda d: deltas.append(d)
    # The mocked client returns SimpleNamespace responses which aren't
    # iterable as streaming chunks; force the non-streaming code path so
    # the guardrail-halt branch is reached without engaging the real
    # streaming machinery.
    agent._disable_streaming = True

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"error": "boom"})),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search repeatedly")

    assert result["turn_exit_reason"] == "guardrail_halt"
    halt_text = result["final_response"]
    assert "stopped retrying" in halt_text

    # The halt message must have been pushed through the callback at least
    # once.  Empty-queue SSE writers were the bug — clients saw no content
    # delta before the finish chunk.
    text_deltas = [d for d in deltas if isinstance(d, str)]
    assert halt_text in text_deltas, (
        f"halt message was never streamed; callback only saw {deltas!r}"
    )
