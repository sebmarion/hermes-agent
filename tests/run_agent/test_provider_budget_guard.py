"""Integration tests for the final provider-request budget guard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.conversation_loop as conversation_loop
import run_agent
from run_agent import AIAgent


def _tool_defs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _chat_response(
    content: str = "ok",
    *,
    finish_reason: str = "stop",
    tool_calls=None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        reasoning=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _receipt(decision: str, reason: str) -> dict:
    return {
        "resolved_model": "test/model",
        "resolved_provider": "openrouter",
        "compressor_model": "test/model",
        "compressor_provider": "openrouter",
        "context_length": 8_192,
        "threshold_tokens": 6_000,
        "estimated_input_tokens": 7_000 if decision == "reject" else 100,
        "margin_tokens": 1_024,
        "estimated_input_with_margin_tokens": 8_024 if decision == "reject" else 1_124,
        "explicit_output_tokens": 1_000,
        "window_input_ceiling": 7_192,
        "effective_input_ceiling": 6_000,
        "category_estimated_tokens": {
            "messages": 100,
            "input": 0,
            "instructions": 0,
            "tools": 0,
            "total": 100,
        },
        "decision": decision,
        "reason": reason,
    }


def _receipt_with_estimate(estimated_tokens: int) -> dict:
    receipt = _receipt("admit", "within_effective_input_ceiling")
    receipt["estimated_input_tokens"] = estimated_tokens
    receipt["estimated_input_with_margin_tokens"] = estimated_tokens + 1_024
    receipt["category_estimated_tokens"]["total"] = estimated_tokens
    return receipt


def _context_error(message: str = "Request entity too large") -> Exception:
    error = Exception(message)
    error.status_code = 413
    return error


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance.client = MagicMock()
    instance._cached_system_prompt = "You are helpful."
    instance._use_prompt_caching = False
    instance.tool_delay = 0
    instance.compression_enabled = True
    instance.save_trajectories = False
    instance._api_max_retries = 1
    return instance


@pytest.mark.parametrize("streaming", [False, True])
def test_local_budget_rejection_blocks_both_transports(agent, monkeypatch, streaming):
    rejected = _receipt("reject", "estimated_input_plus_margin_exceeds_ceiling")
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: rejected,
        raising=False,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: streaming)
    agent._disable_streaming = not streaming

    streaming_transport = MagicMock(return_value=_chat_response())
    nonstream_transport = MagicMock(return_value=_chat_response())
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", streaming_transport)
    monkeypatch.setattr(agent, "_interruptible_api_call", nonstream_transport)
    compress = MagicMock(
        side_effect=lambda messages, system_message, **_kwargs: (
            list(messages),
            system_message or agent._cached_system_prompt,
        )
    )
    monkeypatch.setattr(agent, "_compress_context", compress)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("irreducible " + "x" * 4_000)

    streaming_transport.assert_not_called()
    nonstream_transport.assert_not_called()
    assert compress.call_count == 1
    assert result["compression_exhausted"] is True
    assert any(
        message.get("role") == "user"
        and str(message.get("content") or "").startswith("irreducible ")
        for message in result["messages"]
    )
    assert not any(
        message.get("role") == "assistant"
        and message.get("content") == result["final_response"]
        for message in result["messages"]
    )
    assert agent._last_provider_admission_receipt == rejected


def test_responses_admission_includes_all_middleware_and_final_preflight(
    agent, monkeypatch
):
    agent.api_mode = "codex_responses"
    agent._disable_streaming = True
    measured = []
    preflight_calls = 0

    class _Transport:
        def build_kwargs(self, *args, **kwargs):
            messages = kwargs.get("messages")
            if messages is None and args:
                messages = args[0]
            return {
                "model": agent.model,
                "input": list(messages or []),
                "max_output_tokens": 1_000,
            }

        def preflight_kwargs(self, request, **_kwargs):
            nonlocal preflight_calls
            preflight_calls += 1
            mutated = dict(request)
            mutated[f"preflight_{preflight_calls}"] = preflight_calls
            return mutated

    transport = _Transport()
    monkeypatch.setattr(agent, "_get_transport", lambda: transport)
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _request_middleware(request, **_context):
        mutated = dict(request)
        mutated["request_middleware_marker"] = 1
        return SimpleNamespace(payload=mutated, original_payload=request, trace=[])

    def _execution_middleware(request, next_call, **_context):
        mutated = dict(request)
        mutated["execution_middleware_marker"] = 1
        return next_call(mutated)

    monkeypatch.setattr(
        "hermes_cli.middleware.apply_llm_request_middleware",
        _request_middleware,
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        _execution_middleware,
    )

    def _admit(_agent, request):
        measured.append(dict(request))
        return _receipt("admit", "within_effective_input_ceiling")

    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        _admit,
        raising=False,
    )

    def _transport_call(_request):
        assert measured, "admission must run immediately before transport"
        raise RuntimeError("stop after boundary assertion")

    monkeypatch.setattr(agent, "_interruptible_api_call", _transport_call)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation("measure the final Responses payload")

    assert len(measured) == 1
    assert measured[0]["request_middleware_marker"] == 1
    assert measured[0]["execution_middleware_marker"] == 1
    assert measured[0]["preflight_1"] == 1
    assert measured[0]["preflight_2"] == 2


def test_old_tool_history_is_persisted_then_rebuilt_before_dispatch(
    agent, monkeypatch
):
    old_tool_result = "old tool output " + "x" * 10_000
    bounded_tool_result = "[tool result pruned before dispatch]"
    history = [
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_old",
            "content": old_tool_result,
        },
        {"role": "assistant", "content": "old answer"},
    ]
    prune_calls = 0

    def _prune(messages):
        nonlocal prune_calls
        prune_calls += 1
        if prune_calls > 1:
            return messages, 0
        projected = [dict(message) for message in messages]
        projected[2] = {**projected[2], "content": bounded_tool_result}
        return projected, 1

    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        _prune,
    )
    persisted = []

    def _persist_projection(_agent, previous, projected):
        persisted.append((previous, projected))
        return projected, True

    monkeypatch.setattr(
        conversation_loop,
        "persist_in_place_projection",
        _persist_projection,
        raising=False,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
        raising=False,
    )

    dispatched = []

    def _transport(request):
        dispatched.append(request)
        return _chat_response("bounded dispatch")

    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", _transport)
    compress = MagicMock()
    monkeypatch.setattr(agent, "_compress_context", compress)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "current request",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert len(persisted) == 1
    assert prune_calls == 2
    assert len(dispatched) == 1
    assert bounded_tool_result in str(dispatched[0])
    assert old_tool_result not in str(dispatched[0])
    compress.assert_not_called()


def test_repaired_history_uses_pre_repair_snapshot_for_dispatch_persistence(
    agent, monkeypatch
):
    """Repair and pruning must commit as one projection against DB history.

    The durable compare-and-swap snapshot is the history as it existed before
    sequence repair.  Passing the already-merged history as that snapshot makes
    a legitimate durable prefix look stale and terminates the user turn before
    dispatch.
    """
    old_tool_result = "old tool output " + "x" * 10_000
    bounded_tool_result = "[tool result pruned after sequence repair]"
    history = [
        {"role": "user", "content": "first persisted request"},
        {"role": "user", "content": "second persisted request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_old",
            "content": old_tool_result,
        },
        {"role": "assistant", "content": "old answer"},
    ]
    prune_calls = 0

    def _prune(messages):
        nonlocal prune_calls
        prune_calls += 1
        if prune_calls > 1:
            return messages, 0
        projected = [dict(message) for message in messages]
        tool_index = next(
            index
            for index, message in enumerate(projected)
            if message.get("tool_call_id") == "call_old"
        )
        projected[tool_index] = {
            **projected[tool_index],
            "content": bounded_tool_result,
        }
        return projected, 1

    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        _prune,
    )
    persisted = []

    def _persist_projection(
        _agent,
        previous,
        projected,
        *,
        durable_snapshot_messages=None,
    ):
        persisted.append(
            (
                [dict(message) for message in previous],
                [dict(message) for message in projected],
                [dict(message) for message in durable_snapshot_messages],
                projected is not previous,
            )
        )
        return projected, True

    monkeypatch.setattr(
        conversation_loop,
        "persist_in_place_projection",
        _persist_projection,
        raising=False,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
        raising=False,
    )
    dispatched = []
    agent._disable_streaming = True
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda request: dispatched.append(request) or _chat_response("continued"),
    )
    monkeypatch.setattr(agent, "_compress_context", MagicMock())

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "current request",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert len(persisted) == 1
    repaired, projected, durable_snapshot, _used_distinct_projection = persisted[0]
    assert [message["content"] for message in durable_snapshot[:2]] == [
        "first persisted request",
        "second persisted request",
    ]
    assert repaired[0]["content"] == (
        "first persisted request\n\nsecond persisted request"
    )
    assert any(
        message.get("content") == bounded_tool_result for message in projected
    )
    assert len(dispatched) == 1


def test_repair_only_projection_is_persisted_before_provider_dispatch(
    agent, monkeypatch
):
    history = [
        {"role": "user", "content": "first persisted request"},
        {"role": "user", "content": "second persisted request"},
        {"role": "assistant", "content": "old answer"},
    ]
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    persisted = []

    def _persist_projection(
        _agent,
        previous,
        projected,
        *,
        durable_snapshot_messages=None,
    ):
        persisted.append(
            (
                [dict(message) for message in previous],
                [dict(message) for message in projected],
                [dict(message) for message in durable_snapshot_messages],
                projected is not previous,
            )
        )
        return projected, True

    monkeypatch.setattr(
        conversation_loop,
        "persist_in_place_projection",
        _persist_projection,
        raising=False,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
        raising=False,
    )
    dispatched = []

    def _transport(request):
        assert len(persisted) == 1, "repair must commit before provider transport"
        persisted_projection = persisted[0][1]
        expected_user_idx = next(
            index
            for index, message in enumerate(persisted_projection)
            if message.get("role") == "user"
            and message.get("content") == "current request"
        )
        assert agent._persist_user_message_idx == expected_user_idx
        dispatched.append(request)
        return _chat_response("continued")

    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", _transport)
    monkeypatch.setattr(agent, "_compress_context", MagicMock())

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "current request",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert len(persisted) == 1
    repaired, projected, durable_snapshot, used_distinct_projection = persisted[0]
    assert used_distinct_projection is True
    assert projected == repaired
    assert [message["content"] for message in durable_snapshot[:2]] == [
        "first persisted request",
        "second persisted request",
    ]
    assert repaired[0]["content"] == (
        "first persisted request\n\nsecond persisted request"
    )
    assert len(dispatched) == 1


def test_local_budget_compaction_rebuilds_then_dispatches(agent, monkeypatch):
    receipts = [
        _receipt("reject", "estimated_input_plus_margin_exceeds_ceiling"),
        _receipt("admit", "within_effective_input_ceiling"),
    ]
    measured = []

    def _admit(_agent, request):
        measured.append(request)
        return receipts.pop(0)

    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        _admit,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _compress(_messages, _system_message, **_kwargs):
        agent._last_compaction_in_place = True
        return ([{"role": "user", "content": "bounded current request"}], "bounded system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    budget_type = type(agent.iteration_budget)
    original_refund = budget_type.refund
    refund_calls = []

    def _tracked_refund(budget):
        refund_calls.append(budget)
        return original_refund(budget)

    monkeypatch.setattr(budget_type, "refund", _tracked_refund)
    dispatched = []
    agent._disable_streaming = True
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda request: dispatched.append(request) or _chat_response("after rebuild"),
    )

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("oversized " + "x" * 8_000)

    assert result["completed"] is True
    assert result["final_response"] == "after rebuild"
    assert result["api_calls"] == 1
    assert agent.iteration_budget.used == 1
    assert len(refund_calls) == 1
    assert len(measured) == 2
    assert len(dispatched) == 1
    assert "bounded current request" in str(dispatched[0])
    assert "oversized " not in str(dispatched[0])


def test_local_rebuild_preserves_copilot_user_initiator_until_transport(
    agent, monkeypatch
):
    agent.base_url = "https://api.githubcopilot.com"
    receipts = [
        _receipt("reject", "estimated_input_plus_margin_exceeds_ceiling"),
        _receipt("admit", "within_effective_input_ceiling"),
    ]
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: receipts.pop(0),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _compress(_messages, _system_message, **_kwargs):
        agent._last_compaction_in_place = True
        return ([{"role": "user", "content": "bounded request"}], "system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    dispatched = []
    agent._disable_streaming = True
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda request: dispatched.append(request) or _chat_response("done"),
    )

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("oversized " + "x" * 8_000)

    assert result["completed"] is True
    assert len(dispatched) == 1
    assert dispatched[0]["extra_headers"]["x-initiator"] == "user"
    assert agent._is_user_initiated_turn is False


def test_compression_exhausted_finalizes_when_session_persistence_fails(
    agent, monkeypatch
):
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "reject", "estimated_input_plus_margin_exceeds_ceiling"
        ),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    monkeypatch.setattr(
        agent,
        "_compress_context",
        lambda messages, system_message, **_kwargs: (messages, system_message),
    )
    cleanup = MagicMock()
    agent._disable_streaming = True

    with (
        patch.object(agent, "_persist_session", side_effect=OSError("disk unavailable")),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources", cleanup),
    ):
        result = agent.run_conversation("irreducible " + "x" * 4_000)

    assert result["compression_exhausted"] is True
    assert result["failed"] is True
    assert result["final_response"].startswith("Context budget rejected locally")
    assert "persist_session: disk unavailable" in result["cleanup_errors"]
    cleanup.assert_called_once()


def test_local_budget_accepts_persisted_shrinking_static_fallback(
    agent, monkeypatch
):
    receipts = [
        _receipt("reject", "estimated_input_plus_margin_exceeds_ceiling"),
        _receipt("admit", "within_effective_input_ceiling"),
    ]
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: receipts.pop(0),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _static_fallback(_messages, _system_message, **_kwargs):
        agent.context_compressor._last_summary_error = "summary provider unavailable"
        agent.context_compressor._last_summary_fallback_used = True
        agent._last_compaction_in_place = True
        return (
            [{"role": "user", "content": "bounded deterministic checkpoint"}],
            "bounded system",
        )

    monkeypatch.setattr(agent, "_compress_context", _static_fallback)
    transport = MagicMock(return_value=_chat_response("continued"))
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("oversized " + "x" * 8_000)

    assert result["completed"] is True
    assert result["final_response"] == "continued"
    assert transport.call_count == 1


def test_fallback_readmits_with_rebound_identity_without_resetting_attempt_cap(
    agent, monkeypatch
):
    admission_count = 0
    admitted_identities = []

    def _admission(_agent, _request):
        nonlocal admission_count
        admission_count += 1
        admitted_identities.append(
            (
                agent.provider,
                agent.model,
                agent.context_compressor.provider,
                agent.context_compressor.model,
            )
        )
        if admission_count == 3:
            return _receipt("admit", "within_effective_input_ceiling")
        receipt = _receipt(
            "reject", "estimated_input_plus_margin_exceeds_ceiling"
        )
        receipt["resolved_provider"] = agent.provider
        receipt["resolved_model"] = agent.model
        receipt["compressor_provider"] = agent.context_compressor.provider
        receipt["compressor_model"] = agent.context_compressor.model
        return receipt

    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        _admission,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    compress_calls = 0

    def _compress(messages, system_message, **_kwargs):
        nonlocal compress_calls
        compress_calls += 1
        content = str(messages[-1].get("content") or "")
        reduced = content[: max(1, len(content) // 2)]
        agent._last_compaction_in_place = True
        return ([{"role": "user", "content": reduced}], system_message or "system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    fallback_activated = False

    def _activate_fallback(*_args, **_kwargs):
        nonlocal fallback_activated
        if fallback_activated:
            return False
        fallback_activated = True
        agent.provider = "fallback-provider"
        agent.model = "fallback/model"
        agent.context_compressor.update_model(
            model=agent.model,
            context_length=agent.context_compressor.context_length,
            base_url=agent.base_url,
            api_key=agent.api_key,
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
        return True

    monkeypatch.setattr(agent, "_try_activate_fallback", _activate_fallback)
    monkeypatch.setattr(agent, "_has_pending_fallback", lambda: not fallback_activated)
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", lambda _request: None)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("large " + "x" * 16_000)

    assert result["compression_exhausted"] is True
    assert compress_calls == 3
    assert any(identity[0] == "fallback-provider" for identity in admitted_identities)
    assert all(provider == compressor_provider for provider, _, compressor_provider, _ in admitted_identities)
    assert all(model == compressor_model for _, model, _, compressor_model in admitted_identities)


def test_turn_preflight_and_final_guard_share_three_compaction_cap(
    agent, monkeypatch
):
    agent.context_compressor.context_length = 2_000
    agent.context_compressor.threshold_tokens = 200
    monkeypatch.setattr(
        agent.context_compressor,
        "should_compress",
        lambda _tokens: True,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    monkeypatch.setattr(
        "agent.turn_context.estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 5_000,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "reject", "estimated_input_plus_margin_exceeds_ceiling"
        ),
    )

    compaction_calls = 0

    def _compress(_messages, _system_message, **_kwargs):
        nonlocal compaction_calls
        compaction_calls += 1
        agent._last_compaction_in_place = True
        return (
            [{"role": "user", "content": f"compaction pass {compaction_calls}"}],
            "system",
        )

    monkeypatch.setattr(agent, "_compress_context", _compress)
    transport = MagicMock()
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 50}
        for index in range(40)
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("current turn", conversation_history=history)

    assert compaction_calls == 3
    transport.assert_not_called()
    assert result["compression_exhausted"] is True


def test_turn_preflight_durable_failure_dispatches_original_projection(
    agent, monkeypatch, tmp_path
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(agent.session_id, "cli", model=agent.model)
    agent._session_db = db
    agent._session_db_created = True
    agent._cached_system_prompt = "original system"
    agent.context_compressor.context_length = 2_000
    agent.context_compressor.threshold_tokens = 200
    monkeypatch.setattr(
        agent.context_compressor,
        "should_compress",
        lambda _tokens: True,
    )
    agent.context_compressor._previous_summary = "durable summary"
    agent.context_compressor.compression_count = 4
    agent.context_compressor._last_compression_savings_pct = 73.0
    agent.context_compressor._ineffective_compression_count = 1

    def _candidate_compression(_messages, **_kwargs):
        agent.context_compressor._previous_summary = "unpersisted candidate"
        agent.context_compressor.compression_count = 5
        agent.context_compressor._last_compression_savings_pct = 22.0
        agent.context_compressor._ineffective_compression_count = 2
        agent.context_compressor._last_compression_made_progress = True
        return [{"role": "user", "content": "must not be dispatched"}]

    monkeypatch.setattr(
        agent.context_compressor,
        "compress",
        _candidate_compression,
    )
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    monkeypatch.setattr(
        "agent.turn_context.estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 5_000,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
    )
    monkeypatch.setattr(agent, "_build_system_prompt", lambda _message: "rebuilt system")
    monkeypatch.setattr(agent, "commit_memory_session", lambda _messages: None)
    monkeypatch.setattr(
        db,
        "archive_and_compact",
        MagicMock(side_effect=OSError("durable compaction failed")),
    )
    dispatched = []
    agent._disable_streaming = True
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda request: dispatched.append(request) or _chat_response("continued"),
    )
    original_text = "original oversized request " + "x" * 4_000

    try:
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(original_text)
    finally:
        db.close()

    assert result["completed"] is True
    assert len(dispatched) == 1
    assert original_text in str(dispatched[0])
    assert "must not be dispatched" not in str(dispatched[0])
    assert agent._cached_system_prompt == "original system"
    assert agent._last_compaction_in_place is False
    assert agent.context_compressor._previous_summary == "durable summary"
    assert agent.context_compressor.compression_count == 4
    assert agent.context_compressor._last_compression_savings_pct == 73.0
    assert agent.context_compressor._ineffective_compression_count == 1
    assert agent.context_compressor._last_compression_made_progress is False


def test_provider_overflow_after_shared_cap_finalizes_without_fourth_compaction(
    agent, monkeypatch
):
    agent.context_compressor.context_length = 2_000
    agent.context_compressor.threshold_tokens = 200
    monkeypatch.setattr(
        agent.context_compressor,
        "should_compress",
        lambda _tokens: True,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    monkeypatch.setattr(
        "agent.turn_context.estimate_request_tokens_rough",
        lambda *_args, **_kwargs: 5_000,
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
    )
    compaction_calls = 0

    def _compress(messages, system_message, **_kwargs):
        nonlocal compaction_calls
        compaction_calls += 1
        agent._last_compaction_in_place = True
        return (list(messages[len(messages) // 2 :]), system_message or "system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    transport = MagicMock(side_effect=_context_error())
    cleanup = MagicMock()
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 50}
        for index in range(40)
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources", cleanup),
    ):
        result = agent.run_conversation(
            "current turn", conversation_history=history
        )

    assert compaction_calls == 3
    assert transport.call_count == 1
    assert result["api_calls"] == 1
    assert result["compression_exhausted"] is True
    assert agent._retry_status_buffer == []
    assert agent._pending_fallback_notice is None
    cleanup.assert_called_once()


def test_iteration_exhaustion_never_uses_unguarded_summary_transport(
    agent, monkeypatch
):
    agent.max_iterations = 1
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt(
            "admit", "within_effective_input_ceiling"
        ),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    tool_call = SimpleNamespace(
        id="call_once",
        type="function",
        function=SimpleNamespace(name="terminal", arguments="{}"),
    )
    transport = MagicMock(
        return_value=_chat_response(
            "", finish_reason="tool_calls", tool_calls=[tool_call]
        )
    )
    summary = MagicMock(return_value="unguarded provider summary")
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)
    monkeypatch.setattr(agent, "_handle_max_iterations", summary)

    with (
        patch.object(run_agent, "handle_function_call", return_value='{"ok": true}'),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("use one tool")

    assert transport.call_count == 1
    summary.assert_not_called()
    assert result["completed"] is False
    assert result["partial"] is True
    assert "iteration limit" in result["final_response"].lower()
    assert "start a new turn" in result["final_response"].lower()


def test_provider_context_retry_requires_five_percent_remeasured_shrink(
    agent, monkeypatch
):
    receipts = [_receipt_with_estimate(1_000), _receipt_with_estimate(960)]
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: receipts.pop(0),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _compress(_messages, _system_message, **_kwargs):
        agent._last_compaction_in_place = True
        return ([{"role": "user", "content": "small"}], "system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    transport = MagicMock(side_effect=[_context_error(), _chat_response("must not dispatch")])
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("provider oversized " + "x" * 8_000)

    assert transport.call_count == 1
    assert result["compression_exhausted"] is True


def test_provider_context_rejection_gets_only_one_compact_and_retry(
    agent, monkeypatch
):
    estimates = iter((1_000, 900, 800))
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt_with_estimate(next(estimates)),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    compress = MagicMock()

    def _compress(messages, system_message, **_kwargs):
        content = str(messages[-1].get("content") or "")
        agent._last_compaction_in_place = True
        return (
            [{"role": "user", "content": content[: max(1, len(content) // 2)]}],
            system_message or "system",
        )

    compress.side_effect = _compress
    monkeypatch.setattr(agent, "_compress_context", compress)
    transport = MagicMock(
        side_effect=[_context_error(), _context_error(), _chat_response("third call forbidden")]
    )
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("provider oversized " + "x" * 8_000)

    assert transport.call_count == 2
    assert compress.call_count == 1
    assert result["api_calls"] == 2
    assert agent.iteration_budget.used == 2
    assert result["compression_exhausted"] is True


def test_provider_context_retry_does_not_adopt_failed_durable_compaction(
    agent, monkeypatch
):
    estimates = iter((1_000, 800))
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt_with_estimate(next(estimates)),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    original_user_text = "provider oversized " + "x" * 8_000
    agent._session_db = MagicMock()

    def _failed_persist(_messages, _system_message, **_kwargs):
        agent._last_compaction_in_place = False
        return ([{"role": "user", "content": "must not be adopted"}], "system")

    monkeypatch.setattr(agent, "_compress_context", _failed_persist)
    transport = MagicMock(
        side_effect=[_context_error(), _chat_response("retry forbidden")]
    )
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(original_user_text)

    assert transport.call_count == 1
    assert result["compression_exhausted"] is True
    assert any(
        message.get("role") == "user" and message.get("content") == original_user_text
        for message in result["messages"]
    )
    assert "must not be adopted" not in str(result["messages"])


def test_long_context_tier_shares_provider_context_retry_cap(agent, monkeypatch):
    estimates = iter((1_000, 800, 700))
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: _receipt_with_estimate(next(estimates)),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )
    agent.context_compressor.context_length = 1_000_000

    compress = MagicMock()

    def _compress(messages, system_message, **_kwargs):
        content = str(messages[-1].get("content") or "")
        agent._last_compaction_in_place = True
        return (
            [{"role": "user", "content": content[: max(1, len(content) // 2)]}],
            system_message or "system",
        )

    compress.side_effect = _compress
    monkeypatch.setattr(agent, "_compress_context", compress)

    long_context_error = Exception(
        "Extra usage is required for long context requests over 200k tokens"
    )
    long_context_error.status_code = 429
    transport = MagicMock(
        side_effect=[
            long_context_error,
            long_context_error,
            _chat_response("third call forbidden"),
        ]
    )
    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", transport)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("long context " + "x" * 8_000)

    assert transport.call_count == 2
    assert compress.call_count == 1
    assert result["compression_exhausted"] is True


def test_completed_tool_is_not_reexecuted_and_pair_survives_provider_retry(
    agent, monkeypatch
):
    tool_call = SimpleNamespace(
        id="call_once",
        type="function",
        function=SimpleNamespace(name="terminal", arguments="{}"),
    )
    first = _chat_response(
        "",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    receipts = iter(
        (
            _receipt_with_estimate(500),
            _receipt_with_estimate(1_000),
            _receipt_with_estimate(800),
        )
    )
    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        lambda *_args, **_kwargs: next(receipts),
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    def _compress(messages, _system_message, **_kwargs):
        agent._last_compaction_in_place = True
        return (list(messages[2:]), "system")

    monkeypatch.setattr(agent, "_compress_context", _compress)
    requests = []
    responses = iter((first, _context_error(), _chat_response("done")))

    def _transport(request):
        requests.append(request)
        outcome = next(responses)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    agent._disable_streaming = True
    monkeypatch.setattr(agent, "_interruptible_api_call", _transport)
    executor = MagicMock(return_value='{"ok": true}')

    history = [
        {"role": "user", "content": "old " + "x" * 10_000},
        {"role": "assistant", "content": "old answer"},
    ]
    with (
        patch.object(run_agent, "handle_function_call", executor),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "run exactly once",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert executor.call_count == 1
    assert len(requests) == 3
    retried = str(requests[-1])
    assert retried.count("call_once") >= 2
    assert "tool_call_id" in retried
