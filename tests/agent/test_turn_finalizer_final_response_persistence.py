import json
from types import SimpleNamespace

from agent.turn_finalizer import finalize_turn


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self.trajectory_messages = None
        self.external_memory_kwargs = None

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, messages, *_args, **_kwargs):
        self.trajectory_messages = list(messages)

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)
        self.persisted_conversation_history = conversation_history

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **kwargs):
        self.external_memory_kwargs = kwargs


def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1] == {"role": "assistant", "content": "Done."}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": "Done."}


def test_bestplan_envelope_is_removed_before_first_persistence(monkeypatch):
    hook_calls = []

    def capture_hook(name, **kwargs):
        hook_calls.append((name, kwargs))
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture_hook)
    agent = FakeAgent()
    envelope = (
        "<<<HERMES_BESTPLAN_RECEIPT_V1>>>\n"
        '{"version":1,"body_sha256":"host-validates-authority"}\n'
        "<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>\n"
        "Plan narrative.\n"
        "<<<HERMES_BESTPLAN_V1>>>\n"
        '{"version":1,"manifest":{"slices":[]}}\n'
        "<<<END_HERMES_BESTPLAN_V1>>>"
    )
    messages = [
        {"role": "user", "content": "/bestplan test"},
        {"role": "assistant", "content": envelope},
    ]

    result = finalize_turn(
        agent,
        final_response=envelope,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="/bestplan test",
        original_user_message="/bestplan test",
        _should_review_memory=False,
        _turn_exit_reason="text_response",
    )

    persisted = agent.persisted_messages[-1]["content"]
    assert persisted == "Plan narrative."
    assert "HERMES_BESTPLAN" not in persisted
    assert agent.trajectory_messages[-1]["content"] == "Plan narrative."
    assert "HERMES_BESTPLAN" not in json.dumps(
        agent.external_memory_kwargs, sort_keys=True
    )
    assert all(
        "HERMES_BESTPLAN" not in json.dumps(kwargs, sort_keys=True)
        for _name, kwargs in hook_calls
    )
    # Host capture still receives the raw response after this first write.
    assert result["final_response"] == envelope


def test_absent_conversation_history_does_not_break_persistence(monkeypatch):
    """Fresh private BestPlan lanes pass no prior conversation history."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "inspect read-only"},
        {"role": "assistant", "content": "Plan narrative."},
    ]

    result = finalize_turn(
        agent,
        final_response="Plan narrative.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task",
        turn_id="turn",
        user_message="inspect read-only",
        original_user_message="inspect read-only",
        _should_review_memory=False,
        _turn_exit_reason="text_response",
    )

    assert not any(
        error.startswith("persist_session:")
        for error in result.get("cleanup_errors", [])
    )
    assert agent.persisted_messages == messages
    assert agent.persisted_conversation_history is None
