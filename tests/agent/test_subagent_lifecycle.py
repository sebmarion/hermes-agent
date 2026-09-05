"""Contract tests for the public plugin subagent lifecycle API."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
    bind_subagent_parent,
    get_active_subagent_parent,
)


class FakeChild:
    def __init__(self, ident="sa-test"):
        self._subagent_id = ident
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self.provider = "test"
        self.model = "test-model"
        self.interrupted = False
        self.interrupt_kind = None
        self.interrupt_message = None
        self.tool_reason = None

    def interrupt(self, _reason):
        self.interrupted = True
        self.interrupt_kind = "soft"

    def hard_interrupt(self, reason, *, tool_reason=None):
        self.interrupted = True
        self.interrupt_kind = "hard"
        self.interrupt_message = reason
        self.tool_reason = tool_reason


@pytest.fixture
def lifecycle(monkeypatch):
    parent = SimpleNamespace(session_id="parent-1", enabled_toolsets=["file"])
    counter = iter(range(1000))

    def build(**_kwargs):
        return FakeChild(f"sa-{next(counter)}")

    def run(_index, _goal, child, _parent):
        for _ in range(20):
            if child.interrupted:
                return {
                    "status": "interrupted",
                    "summary": None,
                    "api_calls": 0,
                    "duration_seconds": 0,
                }
            time.sleep(0.002)
        return {
            "status": "completed",
            "summary": "safe summary",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    return SubagentLifecycleService(lambda: parent)






def test_cancel_is_cooperative_and_forged_handle_is_unknown(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    assert lifecycle.cancel(handle, reason="test").accepted
    terminal = lifecycle.wait(handle, timeout_seconds=1)
    assert terminal.state is SubagentState.CANCELLED
    forged = handle.__class__(**{**handle.to_dict(), "capability": "forged"})
    assert lifecycle.status(forged).state is SubagentState.UNKNOWN
    assert lifecycle.result(forged).error_classification == "UNKNOWN_HANDLE"
    other_parent = SimpleNamespace(session_id="different-parent")
    other_service = SubagentLifecycleService(lambda: other_parent)
    assert other_service.status(handle).state is SubagentState.UNKNOWN


def test_cancel_uses_explicit_hard_interrupt(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    record = lifecycle._record(handle)
    assert record is not None and record.agent is not None

    assert lifecycle.cancel(handle, reason="explicit user cancel").accepted

    assert record.agent.interrupt_kind == "hard"
    assert "explicit user cancel" in record.agent.interrupt_message
    assert record.agent.tool_reason == "subagent cancellation requested"
    lifecycle.wait(handle, timeout_seconds=1)








def test_public_lifecycle_forwards_lane_provider_and_api_mode(monkeypatch):
    """A lane pinning provider+api_mode must reach the child builder, not
    fall through to the parent session's provider (404 'model not found')."""
    parent = SimpleNamespace(session_id="parent-lanes")
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return FakeChild("sa-lane-provider")

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr(
        "tools.delegate_tool_config._resolve_delegation_credentials",
        lambda _cfg, _parent: {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "responses",
            "base_url": "https://example.invalid/v1",
            "api_key": None,
        },
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.0,
        },
    )

    service = SubagentLifecycleService(lambda: parent)
    request = SubagentLaunchRequest(
        goal="lane pinned provider",
        model="gpt-5.6-sol",
        provider="openai-codex",
        api_mode="responses",
    )
    service.launch(request)
    assert captured["override_provider"] == "openai-codex"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["override_api_mode"] == "responses"
    assert captured["override_api_key"] is None


def test_public_lifecycle_ignores_blank_provider_pin(monkeypatch):
    """Blank/None provider/model pins mean 'inherit parent'; pass no override."""
    parent = SimpleNamespace(session_id="parent-inherit")
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return FakeChild("sa-inherit")

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.0,
        },
    )

    service = SubagentLifecycleService(lambda: parent)
    service.launch(SubagentLaunchRequest(goal="inherit provider"))
    assert captured.get("override_provider") is None
    assert captured.get("override_model") is None
    assert captured.get("override_api_mode") is None


def test_public_lifecycle_forwards_finite_timeout_as_child_run_budget(monkeypatch):
    parent = SimpleNamespace(session_id="parent-budget")
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return FakeChild("sa-budget")

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.0,
        },
    )

    service = SubagentLifecycleService(lambda: parent)
    service.launch(SubagentLaunchRequest(goal="bounded", timeout_seconds=360.0))

    assert captured["run_budget_seconds"] == 360.0


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "360"],
)
def test_public_lifecycle_rejects_invalid_timeout(invalid_timeout):
    parent = SimpleNamespace(session_id="parent-invalid-budget")
    service = SubagentLifecycleService(lambda: parent)

    with pytest.raises(
        SubagentLifecycleError,
        match="timeout_seconds must be a finite positive number",
    ):
        service.launch(
            SubagentLaunchRequest(goal="bounded", timeout_seconds=invalid_timeout)
        )


def test_public_lifecycle_runs_host_aggregation(monkeypatch):
    memory = Mock()
    parent = SimpleNamespace(
        session_id="parent-aggregate",
        enabled_toolsets=["file"],
        _memory_manager=memory,
        _current_turn_id="turn-1",
        session_estimated_cost_usd=1.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = FakeChild("sa-aggregate")
    child.session_id = "child-session"
    hook = Mock()

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "aggregated",
            "api_calls": 1,
            "duration_seconds": 0.25,
            "_child_role": "leaf",
            "_child_cost_usd": 2.5,
        },
    )
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="aggregate me"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    memory.on_delegation.assert_called_once_with(
        task="aggregate me", result="aggregated", child_session_id="child-session"
    )
    hook.assert_called_once_with(
        "subagent_stop",
        parent_session_id="parent-aggregate",
        parent_turn_id="turn-1",
        child_session_id="child-session",
        child_role="leaf",
        child_summary="aggregated",
        child_status="completed",
        # Redacted tool history rides the shared finalization pipeline
        # (#62011/#72403); empty here because the fabricated result carries
        # no tool_trace.
        tool_call_history=[],
        duration_ms=250,
    )
    assert parent.session_estimated_cost_usd == 3.5
    assert parent.session_cost_source == "subagent"
    assert parent.session_cost_status == "estimated"


def test_public_lifecycle_preserves_machine_summary_above_delegate_budget(monkeypatch):
    parent = SimpleNamespace(
        session_id="parent-machine-result",
        enabled_toolsets=["file"],
    )
    child = FakeChild("sa-machine-result")
    machine_result = '{"schema":"machine","payload":"' + ("x" * 10_000) + '"}'

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": machine_result,
            "api_calls": 1,
            "duration_seconds": 0.25,
        },
    )
    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"max_summary_chars": 100},
    )

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="return machine payload"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    result = service.result(handle)
    assert result.summary == machine_result




def test_agent_turn_binds_and_clears_lifecycle_parent(monkeypatch):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    observed = []

    def run_conversation(parent, *_args, **_kwargs):
        observed.append(get_active_subagent_parent())
        return {"final_response": "ok"}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run_conversation)

    assert agent.run_conversation("hello") == {"final_response": "ok"}
    assert observed == [agent]
    assert get_active_subagent_parent() is None
