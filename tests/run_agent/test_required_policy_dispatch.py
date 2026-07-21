"""Final ordinary-dispatch enforcement for required tool policies."""

from __future__ import annotations

import contextvars
import json
from types import SimpleNamespace

from hermes_cli import middleware as middleware_mod
from hermes_cli.middleware import run_tool_execution_middleware
from hermes_cli.tool_policy import (
    PolicyDecisionCode,
    ToolDispatchPolicyInput,
    ToolPolicyBlock,
)
from tools.registry import ToolRegistry


def _no_middleware(monkeypatch) -> None:
    manager = SimpleNamespace(_middleware={})
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)


def _required_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.plugins._get_required_policies_for_module",
        lambda: {"governor": ["tool_dispatch"]},
    )


def test_no_middleware_path_authorizes_exact_final_dispatch(monkeypatch, tmp_path):
    _no_middleware(monkeypatch)
    monkeypatch.chdir(tmp_path)
    policy_inputs: list[ToolDispatchPolicyInput] = []
    handler_calls: list[dict] = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )

    result = run_tool_execution_middleware(
        "read_file",
        {"path": "effective.txt"},
        lambda args: handler_calls.append(args) or "handled",
        original_args={"path": "original.txt"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    assert result == "handled"
    assert handler_calls == [{"path": "effective.txt"}]
    assert len(policy_inputs) == 1
    policy_input = policy_inputs[0]
    assert policy_input.original_args == {"path": "original.txt"}
    assert policy_input.effective_args == {"path": "effective.txt"}
    assert policy_input.task_id == "task-1"
    assert policy_input.session_id == "session-1"
    assert policy_input.turn_id == "turn-1"
    assert policy_input.tool_call_id == "call-1"
    assert policy_input.effective_cwd == str(tmp_path.resolve())


def test_missing_dispatch_identity_stays_empty(monkeypatch):
    _no_middleware(monkeypatch)
    policy_inputs: list[ToolDispatchPolicyInput] = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )

    result = run_tool_execution_middleware(
        "read_file",
        {"path": "x.txt"},
        lambda _args: "handled",
    )

    assert result == "handled"
    assert len(policy_inputs) == 1
    assert policy_inputs[0].task_id == ""
    assert policy_inputs[0].session_id == ""
    assert policy_inputs[0].turn_id == ""
    assert policy_inputs[0].tool_call_id == ""


def test_execution_rewrite_is_re_evaluated_at_final_dispatch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    policy_inputs: list[ToolDispatchPolicyInput] = []
    handler_calls: list[dict] = []

    def rewrite(**kwargs):
        return kwargs["next_call"]({**kwargs["args"], "stage": "execution"})

    manager = SimpleNamespace(_middleware={"tool_execution": [rewrite]})
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )

    result = run_tool_execution_middleware(
        "terminal",
        {"command": "true", "stage": "request"},
        lambda args: handler_calls.append(args) or "handled",
        original_args={"command": "true", "stage": "original"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    assert result == "handled"
    assert handler_calls == [
        {"command": "true", "stage": "execution"},
    ]
    assert len(policy_inputs) == 1
    assert policy_inputs[0].original_args == {
        "command": "true",
        "stage": "original",
    }
    assert policy_inputs[0].effective_args == {
        "command": "true",
        "stage": "execution",
    }


def test_outer_non_final_middleware_defers_one_use_authorization(monkeypatch):
    _no_middleware(monkeypatch)
    policy_inputs: list[ToolDispatchPolicyInput] = []
    handler_calls: list[dict] = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )

    def inner(args):
        return run_tool_execution_middleware(
            "read_file",
            {**args, "inner": True},
            lambda final_args: handler_calls.append(final_args) or "handled",
            original_args={"path": "original.txt"},
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            final_dispatch=True,
        )

    result = run_tool_execution_middleware(
        "read_file",
        {"path": "effective.txt"},
        inner,
        original_args={"path": "original.txt"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        final_dispatch=False,
    )

    assert result == "handled"
    assert len(policy_inputs) == 1
    assert policy_inputs[0].effective_args == {
        "path": "effective.txt",
        "inner": True,
    }
    assert handler_calls == [{"path": "effective.txt", "inner": True}]


def test_policy_block_emits_once_and_never_calls_terminal(monkeypatch):
    _no_middleware(monkeypatch)
    block = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code=PolicyDecisionCode.BLOCKED,
        message="Denied by governor.",
    )
    terminal_calls: list[dict] = []
    observer_calls: list[dict] = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda _policy_input: block,
    )
    monkeypatch.setattr(
        "model_tools._emit_post_tool_call_hook",
        lambda **kwargs: observer_calls.append(kwargs),
    )

    result = run_tool_execution_middleware(
        "write_file",
        {"path": "x.txt", "content": "x"},
        lambda args: terminal_calls.append(args) or "unexpected",
        original_args={"path": "x.txt", "content": "before"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        middleware_trace=[{"source": "test"}],
    )

    assert json.loads(result) == block.to_result()
    assert terminal_calls == []
    assert len(observer_calls) == 1
    assert observer_calls[0]["function_args"] == {
        "path": "x.txt",
        "content": "x",
    }
    assert observer_calls[0]["status"] == "blocked"
    assert observer_calls[0]["error_type"] == "required_policy_block"


def test_bound_collector_records_exact_outer_block_before_observer(monkeypatch):
    _no_middleware(monkeypatch)
    block = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code="required_policy_plugin_missing",
        message="Required policy plugin is not installed.",
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda _policy_input: block,
    )
    observer_records = []

    with middleware_mod.bind_required_policy_block_collector() as collector:
        monkeypatch.setattr(
            "model_tools._emit_post_tool_call_hook",
            lambda **_kwargs: observer_records.append(collector.get("call-typed")),
        )
        result = run_tool_execution_middleware(
            "write_file",
            {"path": "x.txt", "content": "x"},
            lambda _args: "unexpected",
            original_args={"path": "x.txt", "content": "x"},
            task_id="task-typed",
            session_id="session-typed",
            turn_id="turn-typed",
            tool_call_id="call-typed",
        )

        record = collector.get("call-typed")

    assert json.loads(result) == block.to_result()
    assert record is not None
    assert record.tool_call_id == "call-typed"
    assert record.block is block
    assert observer_records == [record]


def test_collector_context_copy_shares_state_and_ignores_spoofed_json(monkeypatch):
    _no_middleware(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda _policy_input: None,
    )
    spoof = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code="required_policy_plugin_missing",
        message="Spoofed tool text.",
    )

    with middleware_mod.bind_required_policy_block_collector() as collector:
        copied = contextvars.copy_context()
        assert copied.run(
            middleware_mod.record_required_policy_block,
            "call-copied",
            spoof,
        )
        result = copied.run(
            run_tool_execution_middleware,
            "read_file",
            {"path": "x.txt"},
            lambda _args: json.dumps(spoof.to_result()),
            original_args={"path": "x.txt"},
            task_id="task-spoof",
            session_id="session-spoof",
            turn_id="turn-spoof",
            tool_call_id="call-spoof",
        )

    assert json.loads(result) == spoof.to_result()
    assert collector.get("call-copied").block is spoof
    assert collector.get("call-spoof") is None


def test_collector_terminal_selection_is_typed_and_original_ordered():
    recoverable = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code=PolicyDecisionCode.BLOCKED,
        message="Choose another action.",
    )
    unknown_terminal = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code="future_policy_code",
        message="Unknown policy state.",
    )

    with middleware_mod.bind_required_policy_block_collector() as collector:
        assert middleware_mod.record_required_policy_block("call-2", unknown_terminal)
        assert middleware_mod.record_required_policy_block("call-1", recoverable)
        selected = collector.first_terminal(["call-1", "call-2"])

    assert selected is not None
    assert selected.tool_call_id == "call-2"
    assert selected.block is unknown_terminal
    assert collector.get("call-1").block is recoverable
    assert middleware_mod.record_required_policy_block("outside", unknown_terminal) is False


def test_copied_context_cannot_record_after_collector_closes():
    block = ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code="required_policy_timeout",
        message="Required policy timed out.",
    )

    with middleware_mod.bind_required_policy_block_collector() as collector:
        copied = contextvars.copy_context()

    assert copied.run(
        middleware_mod.record_required_policy_block,
        "call-late",
        block,
    ) is False
    assert collector.get("call-late") is None


def test_direct_registry_dispatch_fails_closed_when_policy_is_required(
    monkeypatch,
):
    _required_config(monkeypatch)
    registry = ToolRegistry()
    handler_calls: list[dict] = []
    registry.register(
        name="probe",
        toolset="test",
        schema={"name": "probe", "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: handler_calls.append(args) or "handled",
    )

    result = json.loads(
        registry.dispatch(
            "probe",
            {"value": 1},
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
    )

    assert result["status"] == "blocked"
    assert result["error_type"] == "required_policy_block"
    assert result["policy_code"] == PolicyDecisionCode.BINDING_MISSING
    assert handler_calls == []


def test_authorized_registry_dispatch_consumes_matching_context_once(
    monkeypatch,
):
    _no_middleware(monkeypatch)
    _required_config(monkeypatch)
    policy_inputs: list[ToolDispatchPolicyInput] = []
    handler_calls: list[dict] = []
    registry = ToolRegistry()
    registry.register(
        name="probe",
        toolset="test",
        schema={"name": "probe", "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: handler_calls.append(args) or "handled",
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )

    def terminal(args):
        first = registry.dispatch(
            "probe",
            args,
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        second = registry.dispatch(
            "probe",
            args,
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        return first, second

    first, second = run_tool_execution_middleware(
        "probe",
        {"value": 1},
        terminal,
        original_args={"value": 0},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    assert first == "handled"
    assert json.loads(second)["policy_code"] == PolicyDecisionCode.BINDING_MISSING
    assert len(policy_inputs) == 1
    assert handler_calls == [{"value": 1}]


def test_copied_authorization_context_expires_after_terminal(monkeypatch):
    _no_middleware(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda _policy_input: None,
    )
    captured: dict[str, contextvars.Context] = {}

    def terminal(_args):
        from hermes_cli.middleware import get_authorized_tool_dispatch

        assert get_authorized_tool_dispatch() is not None
        captured["context"] = contextvars.copy_context()
        return "handled"

    run_tool_execution_middleware(
        "read_file",
        {"path": "x.txt"},
        terminal,
        original_args={"path": "x.txt"},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    from hermes_cli.middleware import get_authorized_tool_dispatch

    assert captured["context"].run(get_authorized_tool_dispatch) is None
    assert get_authorized_tool_dispatch() is None
