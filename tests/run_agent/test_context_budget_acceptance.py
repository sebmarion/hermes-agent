"""Acceptance coverage for bounded context continuity across compaction."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.conversation_loop as conversation_loop
import run_agent
from agent.chat_completion_helpers import build_provider_request_admission_receipt
from tests.run_agent.test_provider_budget_guard import (
    _chat_response,
    _tool_defs,
)


_TOOL_RESULT_BYTES = 2_490_000
_TOOL_RESULT_COUNT = 24
_MARKERS = (
    "OBJECTIVE::finish the bounded-context rollout",
    "CONSTRAINT::never repeat an already-completed tool side effect",
    "COMPLETED_WORK::the guarded tool mutation ran exactly once",
    "NEXT_ACTION::continue from the admitted compacted request",
)
_RAW_TASK = "\n".join(_MARKERS)
_CHECKPOINT = "[BOUNDED CONTINUITY CHECKPOINT]\n" + "\n".join(_MARKERS)
_OVER_BUDGET_TOOL_RESULT = "OVER_BUDGET_TOOL_RESULT::mutation-complete"
_FAILURE_WINDOW_MARKER = "failure_window_pass_3_recovered_action"


def _make_agent(*, session_db=None, session_id=None, fallback_model=None):
    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = run_agent.AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id=session_id,
            fallback_model=fallback_model,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.compression_enabled = True
    agent.compression_in_place = True
    agent._compression_feasibility_checked = True
    agent._cached_system_prompt = "bounded system prompt"
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent._api_max_retries = 1
    agent.tool_delay = 0
    agent.save_trajectories = False
    return agent


def _append_tool_heavy_tail(db, session_id: str, pass_number: int) -> None:
    """Append exactly 2.49 MB of tool results split over real tool pairs."""
    base_size, remainder = divmod(_TOOL_RESULT_BYTES, _TOOL_RESULT_COUNT)
    written = 0
    for index in range(_TOOL_RESULT_COUNT):
        call_id = f"pass-{pass_number}-call-{index}"
        tool_name = (
            _FAILURE_WINDOW_MARKER
            if pass_number == 3 and index == 0
            else "terminal"
        )
        db.append_message(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        )
        size = base_size + (1 if index < remainder else 0)
        label = f"pass={pass_number};tool={index};"
        content = label + "x" * (size - len(label))
        written += len(content.encode("utf-8"))
        db.append_message(
            session_id=session_id,
            role="tool",
            content=content,
            tool_name=tool_name,
            tool_call_id=call_id,
        )
    assert written == _TOOL_RESULT_BYTES


def _compaction_summary_text(messages) -> str:
    summaries = [
        str(message.get("content") or "")
        for message in messages
        if "CONTEXT COMPACTION — REFERENCE ONLY"
        in str(message.get("content") or "")
    ]
    assert len(summaries) == 1
    return summaries[0]


def test_four_tool_heavy_in_place_compactions_survive_durable_restarts(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    session_id = "context-budget-acceptance"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id, "cli", model="test/model")
    db.append_message(session_id=session_id, role="user", content=_CHECKPOINT)
    archived_ids: set[int] = set()
    summary_calls = 0
    agent = None

    def _summary_provider(**_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        if summary_calls == 3:
            raise RuntimeError("injected summary provider failure")
        return _chat_response(_CHECKPOINT)

    try:
        with patch(
            "agent.context_compressor.call_llm",
            side_effect=_summary_provider,
        ):
            for pass_number in range(1, 5):
                _append_tool_heavy_tail(db, session_id, pass_number)
                messages = db.get_messages_as_conversation(session_id)
                active_before = {
                    row["id"] for row in db.get_messages(session_id)
                }
                assert active_before.isdisjoint(archived_ids)
                assert all(str(messages).count(marker) == 1 for marker in _MARKERS)

                if pass_number < 4:
                    agent = _make_agent(session_db=db, session_id=session_id)
                else:
                    # Keep the pass-3 compressor alive across the durable DB
                    # reopen. This is the same-process failure -> next-compaction
                    # path that must carry the deterministic fallback forward.
                    assert agent is not None
                    agent._session_db = db
                    agent._session_db_created = True
                agent.context_compressor.protect_first_n = 0
                agent.context_compressor.protect_last_n = 2
                agent.context_compressor.abort_on_summary_failure = False
                agent.context_compressor.update_model(
                    model=agent.model,
                    context_length=128_000,
                    base_url=agent.base_url,
                    api_key=agent.api_key,
                    provider=agent.provider,
                    api_mode=agent.api_mode,
                )
                agent.context_compressor.tail_token_budget = 1
                agent._build_system_prompt = MagicMock(
                    return_value="bounded system prompt"
                )
                agent.commit_memory_session = MagicMock()
                agent._emit_warning = MagicMock()

                oversized = build_provider_request_admission_receipt(
                    agent,
                    {
                        "model": agent.model,
                        "messages": messages,
                        "tools": _tool_defs(),
                        "max_tokens": 1_024,
                    },
                )
                assert oversized["decision"] == "reject"
                assert oversized["estimated_input_tokens"] > 600_000

                compacted, system_prompt = compress_context(
                    agent,
                    messages,
                    system_message="bounded system prompt",
                    approx_tokens=oversized["estimated_input_tokens"],
                )

                assert agent.session_id == session_id
                assert agent._last_compaction_in_place is True
                assert system_prompt == "bounded system prompt"
                assert all(str(compacted).count(marker) == 1 for marker in _MARKERS)
                bounded = build_provider_request_admission_receipt(
                    agent,
                    {
                        "model": agent.model,
                        "messages": compacted,
                        "tools": _tool_defs(),
                        "max_tokens": 1_024,
                    },
                )
                assert bounded["decision"] == "admit"
                if pass_number == 3:
                    assert agent.context_compressor._last_summary_fallback_used is True
                    assert "injected summary provider failure" in (
                        agent._emit_warning.call_args.args[0]
                    )
                if pass_number >= 3:
                    assert (
                        _compaction_summary_text(compacted).count(
                            _FAILURE_WINDOW_MARKER
                        )
                        == 1
                    )

                archived_ids.update(active_before)
                all_rows = db.get_messages(session_id, include_inactive=True)
                archived = {
                    row["id"]: row
                    for row in all_rows
                    if row["id"] in archived_ids
                }
                assert set(archived) == archived_ids
                assert all(
                    row["active"] == 0 and row["compacted"] == 1
                    for row in archived.values()
                )

                # Reopen SQLite after every boundary: the next pass starts from
                # the durable active projection, never an in-memory shortcut.
                db.close()
                db = SessionDB(db_path=db_path)
                reloaded = db.get_messages_as_conversation(session_id)
                assert all(str(reloaded).count(marker) == 1 for marker in _MARKERS)
                if pass_number >= 3:
                    assert (
                        _compaction_summary_text(reloaded).count(
                            _FAILURE_WINDOW_MARKER
                        )
                        == 1
                    )
                assert {
                    row["id"] for row in db.get_messages(session_id)
                }.isdisjoint(archived_ids)

        assert summary_calls == 3
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.get_session(session_id)["message_count"] == len(reloaded)
    finally:
        db.close()


def test_real_guard_and_provider_fallback_do_not_replay_tool_side_effect(
    monkeypatch,
):
    fallback = {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "fallback-key",
    }
    agent = _make_agent(fallback_model=fallback)
    agent.context_compressor.protect_first_n = 0
    agent.context_compressor.protect_last_n = 2
    agent.context_compressor.update_model(
        model=agent.model,
        context_length=256_000,
        base_url=agent.base_url,
        api_key=agent.api_key,
        provider=agent.provider,
        api_mode=agent.api_mode,
    )

    oversized_tool_result = _OVER_BUDGET_TOOL_RESULT + "x" * (
        _TOOL_RESULT_BYTES - len(_OVER_BUDGET_TOOL_RESULT)
    )
    oversized_request = {
        "model": agent.model,
        "messages": [
            {"role": "user", "content": _RAW_TASK},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-once",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-once",
                "content": oversized_tool_result,
            },
        ],
        "tools": _tool_defs(),
        "max_tokens": 1_024,
    }
    oversized_receipt = build_provider_request_admission_receipt(
        agent, oversized_request
    )
    assert oversized_receipt["decision"] == "reject"
    assert oversized_receipt["estimated_input_tokens"] > 600_000

    real_admission = conversation_loop.build_provider_request_admission_receipt
    admissions: list[tuple[dict, str, str, dict]] = []

    def _record_admission(active_agent, request):
        receipt = real_admission(active_agent, request)
        admissions.append(
            (receipt, active_agent.provider, active_agent.model, request)
        )
        return receipt

    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        _record_admission,
    )

    tool_call = SimpleNamespace(
        id="call-once",
        type="function",
        function=SimpleNamespace(name="terminal", arguments="{}"),
    )
    transported: list[tuple[str, dict]] = []

    def _transport(request):
        assert admissions
        receipt, _provider, _model, admitted_request = admissions[-1]
        assert admitted_request is request
        assert receipt["decision"] == "admit"
        assert len(str(request)) < _TOOL_RESULT_BYTES
        transported.append((agent.provider, request))
        if len(transported) == 1:
            return _chat_response("", tool_calls=[tool_call])
        if len(transported) == 2:
            rate_limit = Exception("Error code: 429 - rate limit exceeded")
            rate_limit.status_code = 429
            rate_limit.body = {"error": {"message": "rate limit exceeded"}}
            raise rate_limit
        return _chat_response("continued from bounded checkpoint")

    fallback_client = MagicMock()
    fallback_client.base_url = fallback["base_url"]
    fallback_client.api_key = fallback["api_key"]
    monkeypatch.setattr(agent, "_interruptible_api_call", _transport)
    side_effect = MagicMock(return_value=oversized_tool_result)
    history = [
        {"role": "user", "content": _CHECKPOINT},
        {"role": "assistant", "content": "checkpoint acknowledged"},
        {"role": "user", "content": "bounded prior turn one"},
        {"role": "assistant", "content": "bounded prior answer one"},
        {"role": "user", "content": "bounded prior turn two"},
        {"role": "assistant", "content": "bounded prior answer two"},
    ]

    with (
        patch("agent.context_compressor.call_llm", return_value=_chat_response(_CHECKPOINT)),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, None),
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch.object(run_agent, "handle_function_call", side_effect),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "execute the guarded tool once",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert result["final_response"] == "continued from bounded checkpoint"
    assert side_effect.call_count == 1
    assert [provider for provider, _request in transported] == [
        "openrouter",
        "openrouter",
        "groq",
    ]
    assert len(admissions) == len(transported) == 3
    assert all(receipt["decision"] == "admit" for receipt, *_rest in admissions)
    assert all(
        len(str(request)) < _TOOL_RESULT_BYTES
        for _provider, request in transported
    )
    assert all(marker in str(transported[-1][1]) for marker in _MARKERS)
    assert agent.provider == "groq"
    assert agent.model == "llama-3.3-70b-versatile"
    assert agent.context_compressor.provider == agent.provider
    assert agent.context_compressor.model == agent.model
    assert agent._last_provider_admission_receipt["resolved_provider"] == "groq"
