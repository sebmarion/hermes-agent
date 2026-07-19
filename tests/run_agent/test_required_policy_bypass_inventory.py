"""Negative probes for required-policy coverage claims and bypass boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import middleware as middleware_mod
from hermes_cli.middleware import run_tool_execution_middleware
from hermes_cli.tool_policy import PolicyDecisionCode
from tools.registry import ToolRegistry


def _no_execution_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SimpleNamespace(_middleware={})
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)


def _required_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.plugins._get_required_policies_for_module",
        lambda: {"governor": ["tool_dispatch"]},
    )


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("execute_code", {"code": "print('nested operation')"}),
        ("terminal", {"command": "python -c \"print('opaque')\""}),
    ],
)
def test_outer_opaque_tools_are_gated_once_without_nested_visibility_claims(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: dict,
) -> None:
    _no_execution_middleware(monkeypatch)
    _required_config(monkeypatch)
    policy_inputs = []
    handler_calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )
    registry = ToolRegistry()
    registry.register(
        name=tool_name,
        toolset="test",
        schema={"name": tool_name, "parameters": {"type": "object"}},
        handler=lambda received, **_kwargs: handler_calls.append(received) or "handled",
    )

    result = run_tool_execution_middleware(
        tool_name,
        args,
        lambda final_args: registry.dispatch(
            tool_name,
            final_args,
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        ),
        original_args=args,
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    assert result == "handled"
    assert len(policy_inputs) == 1
    assert policy_inputs[0].tool_name == tool_name
    assert policy_inputs[0].effective_args == args
    assert handler_calls == [args]
    payload = policy_inputs[0].to_callback_payload()
    assert all("gitnexus" not in key.lower() for key in payload)
    assert all("classification" not in key.lower() for key in payload)
    assert set(payload) == {
        "tool_name",
        "original_args",
        "effective_args",
        "task_id",
        "session_id",
        "turn_id",
        "tool_call_id",
        "effective_cwd",
        "effective_cwd_source",
        "effective_cwd_authoritative",
        "policy_binding",
    }


def test_tool_search_bridge_gates_underlying_tool_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from model_tools import handle_function_call
    from tools import tool_search

    _no_execution_middleware(monkeypatch)
    policy_inputs = []
    dispatch_calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.get_pre_tool_call_block_message",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: False)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr(
        tool_search,
        "resolve_underlying_call",
        lambda _args: ("web_search", {"q": "test"}, None),
    )
    monkeypatch.setattr(
        tool_search,
        "scoped_deferrable_names",
        lambda _defs: frozenset({"web_search"}),
    )
    monkeypatch.setattr(
        "model_tools.registry.dispatch",
        lambda name, args, **_kwargs: (
            dispatch_calls.append((name, args)) or "handled"
        ),
    )

    result = handle_function_call(
        "tool_call",
        {"name": "web_search", "arguments": {"q": "test"}},
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )

    assert result == "handled"
    assert [item.tool_name for item in policy_inputs] == ["web_search"]
    assert dispatch_calls == [("web_search", {"q": "test"})]


def test_direct_registry_call_is_a_stable_negative_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_config(monkeypatch)
    registry = ToolRegistry()
    handler_calls = []
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

    assert result["policy_code"] == PolicyDecisionCode.BINDING_MISSING
    assert handler_calls == []


def test_registry_binding_failure_records_typed_block_when_collector_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_config(monkeypatch)
    registry = ToolRegistry()
    handler_calls = []
    registry.register(
        name="probe",
        toolset="test",
        schema={"name": "probe", "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: handler_calls.append(args) or "handled",
    )

    with middleware_mod.bind_required_policy_block_collector() as collector:
        result = json.loads(
            registry.dispatch(
                "probe",
                {"value": 1},
                task_id="task-typed",
                session_id="session-typed",
                turn_id="turn-typed",
                tool_call_id="call-typed",
            )
        )
        record = collector.get("call-typed")

    assert result["policy_code"] == PolicyDecisionCode.BINDING_MISSING
    assert record is not None
    assert record.tool_call_id == "call-typed"
    assert record.block.to_result() == result
    assert handler_calls == []


def test_outer_delegate_authorization_cannot_authorize_nested_registry_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_execution_middleware(monkeypatch)
    _required_config(monkeypatch)
    policy_inputs = []
    handler_calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins.authorize_required_tool_policies",
        lambda policy_input: policy_inputs.append(policy_input),
    )
    registry = ToolRegistry()
    registry.register(
        name="child_tool",
        toolset="test",
        schema={"name": "child_tool", "parameters": {"type": "object"}},
        handler=lambda args, **_kwargs: handler_calls.append(args) or "handled",
    )

    outer_result = run_tool_execution_middleware(
        "delegate_task",
        {"prompt": "child"},
        lambda _outer_args: registry.dispatch(
            "child_tool",
            {"value": 1},
            task_id="child-task",
            session_id="child-session",
            turn_id="child-turn",
            tool_call_id="child-call",
        ),
        original_args={"prompt": "child"},
        task_id="parent-task",
        session_id="parent-session",
        turn_id="parent-turn",
        tool_call_id="parent-call",
    )

    assert json.loads(outer_result)["policy_code"] == (
        PolicyDecisionCode.BINDING_MISMATCH
    )
    assert handler_calls == []

    child_result = run_tool_execution_middleware(
        "child_tool",
        {"value": 1},
        lambda child_args: registry.dispatch(
            "child_tool",
            child_args,
            task_id="child-task",
            session_id="child-session",
            turn_id="child-turn",
            tool_call_id="child-call",
        ),
        original_args={"value": 1},
        task_id="child-task",
        session_id="child-session",
        turn_id="child-turn",
        tool_call_id="child-call",
    )

    assert child_result == "handled"
    assert [item.tool_name for item in policy_inputs] == [
        "delegate_task",
        "child_tool",
    ]
    assert handler_calls == [{"value": 1}]
