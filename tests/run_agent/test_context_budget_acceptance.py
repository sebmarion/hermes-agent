"""Acceptance coverage for bounded context continuity across compaction."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.conversation_loop as conversation_loop
import run_agent
from tests.run_agent.test_provider_budget_guard import (
    _chat_response,
    _receipt,
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


def _make_agent(*, session_db=None, session_id=None):
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
        db.append_message(
            session_id=session_id,
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
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
            tool_name="terminal",
            tool_call_id=call_id,
        )
    assert written == _TOOL_RESULT_BYTES


def test_four_tool_heavy_in_place_compactions_survive_durable_restarts(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    session_id = "context-budget-acceptance"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id, "cli", model="test/model")
    db.append_message(session_id=session_id, role="user", content=_CHECKPOINT)
    archived_ids: set[int] = set()

    try:
        for pass_number in range(1, 5):
            _append_tool_heavy_tail(db, session_id, pass_number)
            messages = db.get_messages_as_conversation(session_id)
            active_before = {
                row["id"] for row in db.get_messages(session_id)
            }
            assert active_before.isdisjoint(archived_ids)
            assert all(marker in str(messages) for marker in _MARKERS)

            agent = _make_agent(session_db=db, session_id=session_id)
            agent._build_system_prompt = MagicMock(
                return_value="bounded system prompt"
            )
            agent.commit_memory_session = MagicMock()
            agent._emit_warning = MagicMock()

            def _fallback_checkpoint(
                candidate,
                current_tokens=None,
                focus_topic=None,
                force=False,
            ):
                del current_tokens, focus_topic, force
                assert all(marker in str(candidate) for marker in _MARKERS)
                agent.context_compressor._last_compress_aborted = False
                agent.context_compressor._last_summary_error = (
                    "injected summary provider failure"
                    if pass_number == 3
                    else None
                )
                agent.context_compressor._last_summary_fallback_used = (
                    pass_number == 3
                )
                return [{"role": "user", "content": _CHECKPOINT}]

            agent.context_compressor.compress = _fallback_checkpoint
            compacted, system_prompt = compress_context(
                agent,
                messages,
                system_message="bounded system prompt",
                approx_tokens=700_000,
            )

            assert agent.session_id == session_id
            assert agent._last_compaction_in_place is True
            assert system_prompt == "bounded system prompt"
            assert compacted == [{"role": "user", "content": _CHECKPOINT}]
            if pass_number == 3:
                assert agent.context_compressor._last_summary_fallback_used is True
                assert "injected summary provider failure" in (
                    agent._emit_warning.call_args.args[0]
                )

            archived_ids.update(active_before)
            all_rows = db.get_messages(session_id, include_inactive=True)
            archived = {row["id"]: row for row in all_rows if row["id"] in archived_ids}
            assert set(archived) == archived_ids
            assert all(
                row["active"] == 0 and row["compacted"] == 1
                for row in archived.values()
            )

            # Reopen SQLite after every boundary: the next pass starts from the
            # durable active projection, never from an in-memory shortcut.
            db.close()
            db = SessionDB(db_path=db_path)
            reloaded = db.get_messages_as_conversation(session_id)
            assert [
                (message["role"], message["content"])
                for message in reloaded
            ] == [("user", _CHECKPOINT)]
            assert all(
                reloaded[0]["content"].count(marker) == 1
                for marker in _MARKERS
            )
            assert {
                row["id"] for row in db.get_messages(session_id)
            }.isdisjoint(archived_ids)

        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.get_session(session_id)["message_count"] == 1
    finally:
        db.close()


def test_provider_and_summary_fallback_do_not_replay_tool_side_effect(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(
        agent.context_compressor,
        "should_compress",
        lambda _tokens: False,
    )
    monkeypatch.setattr(
        agent.context_compressor,
        "prune_tool_results_for_dispatch",
        lambda messages: (messages, 0),
    )

    admissions: list[tuple[str, str, str, dict]] = []

    def _admit(_agent, request):
        serialized = str(request)
        over_budget = (
            _OVER_BUDGET_TOOL_RESULT in serialized
            and "[BOUNDED CONTINUITY CHECKPOINT]" not in serialized
        )
        decision = "reject" if over_budget else "admit"
        admissions.append((decision, agent.provider, agent.model, request))
        receipt = _receipt(
            decision,
            "estimated_input_plus_margin_exceeds_ceiling"
            if decision == "reject"
            else "within_effective_input_ceiling",
        )
        receipt.update(
            resolved_provider=agent.provider,
            resolved_model=agent.model,
            compressor_provider=agent.context_compressor.provider,
            compressor_model=agent.context_compressor.model,
        )
        return receipt

    monkeypatch.setattr(
        conversation_loop,
        "build_provider_request_admission_receipt",
        _admit,
    )

    compactions = 0

    def _summary_failure_fallback(messages, system_message, **_kwargs):
        nonlocal compactions
        compactions += 1
        assert _OVER_BUDGET_TOOL_RESULT in str(messages)
        assert all(marker in str(messages) for marker in _MARKERS)
        agent.context_compressor._last_summary_error = (
            "injected summary provider failure"
        )
        agent.context_compressor._last_summary_fallback_used = True
        agent.context_compressor._last_compress_aborted = False
        agent._last_compaction_in_place = True
        return ([{"role": "user", "content": _CHECKPOINT}], system_message)

    monkeypatch.setattr(agent, "_compress_context", _summary_failure_fallback)

    fallback_activations = 0

    def _activate_fallback(*_args, **_kwargs):
        nonlocal fallback_activations
        if fallback_activations:
            return False
        fallback_activations += 1
        agent.provider = "fallback-provider"
        agent.model = "fallback/model"
        agent._fallback_index = 1
        agent.context_compressor.update_model(
            model=agent.model,
            context_length=agent.context_compressor.context_length,
            base_url=agent.base_url,
            api_key=agent.api_key,
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
        return True

    agent._fallback_chain = [{"provider": "fallback-provider"}]
    agent._fallback_index = 0
    monkeypatch.setattr(agent, "_try_activate_fallback", _activate_fallback)

    tool_call = SimpleNamespace(
        id="call-once",
        type="function",
        function=SimpleNamespace(name="terminal", arguments="{}"),
    )
    transported: list[tuple[str, dict]] = []

    def _transport(request):
        assert admissions[-1][0] == "admit"
        serialized = str(request)
        assert not (
            _OVER_BUDGET_TOOL_RESULT in serialized
            and "[BOUNDED CONTINUITY CHECKPOINT]" not in serialized
        )
        transported.append((agent.provider, request))
        if len(transported) == 1:
            return _chat_response("", tool_calls=[tool_call])
        if len(transported) == 2:
            rate_limit = Exception("Error code: 429 - rate limit exceeded")
            rate_limit.status_code = 429
            rate_limit.body = {"error": {"message": "rate limit exceeded"}}
            raise rate_limit
        return _chat_response("continued from bounded checkpoint")

    monkeypatch.setattr(agent, "_interruptible_api_call", _transport)
    side_effect = MagicMock(
        return_value=_OVER_BUDGET_TOOL_RESULT + "x" * 8_000
    )

    with (
        patch.object(run_agent, "handle_function_call", side_effect),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(_RAW_TASK)

    assert result["completed"] is True
    assert result["final_response"] == "continued from bounded checkpoint"
    assert side_effect.call_count == 1
    assert compactions == 1
    assert fallback_activations == 1
    assert [decision for decision, _provider, _model, _request in admissions] == [
        "admit",
        "reject",
        "admit",
        "admit",
    ]
    assert [provider for _decision, provider, _model, _request in admissions] == [
        "openrouter",
        "openrouter",
        "openrouter",
        "fallback-provider",
    ]
    assert [provider for provider, _request in transported] == [
        "openrouter",
        "openrouter",
        "fallback-provider",
    ]
    assert all(
        not (
            _OVER_BUDGET_TOOL_RESULT in str(request)
            and "[BOUNDED CONTINUITY CHECKPOINT]" not in str(request)
        )
        for _provider, request in transported
    )
    assert all(marker in str(transported[-1][1]) for marker in _MARKERS)
    assert agent._last_provider_admission_receipt["resolved_provider"] == (
        "fallback-provider"
    )
    assert agent.context_compressor._last_summary_fallback_used is True
    assert agent.context_compressor._last_summary_error == (
        "injected summary provider failure"
    )
