from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent.bestplan_state import PlanState, try_resolve_go
from cli import _capture_cli_bestplan_result
from tests.agent.test_bestplan_host_ingress import _config, _envelope, _store


def _result():
    response = "Plan for review.\n\n" + _envelope()
    return {
        "final_response": response,
        "messages": [
            {"role": "user", "content": "/bestplan fix it"},
            {"role": "assistant", "content": response},
        ],
    }


def _real_agent(db, session_id):
    """Build the real AIAgent persistence seam without an LLM client."""
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_messages = []
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent._cached_system_prompt = "test system prompt"
    agent._session_init_model_config = None
    agent._parent_session_id = None
    agent._session_json_enabled = False
    agent._pending_cli_user_message = None
    agent._session_persist_lock = threading.RLock()
    return agent


def _stored_roles_and_content(db, session_id):
    return [
        (message["role"], message["content"])
        for message in db.get_messages_as_conversation(session_id)
    ]


def test_cli_persist_failure_leaves_capture_inert(tmp_path):
    store = _store(tmp_path)
    host = SimpleNamespace(
        api_mode="chat_completions",
        _persist_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected CLI receipt persistence failure")
        ),
    )

    with pytest.raises(OSError, match="receipt persistence"):
        _capture_cli_bestplan_result(
            _result(),
            invocation_message="/bestplan fix it",
            session_id="s1",
            profile="coder",
            workspace="/tmp/work",
            host_agent=host,
            baseline_fingerprint="base-1",
            store=store,
        )

    rows = store.list_for_session("s1", open_only=False)
    assert len(rows) == 1
    assert rows[0]["state"] == PlanState.PROVISIONAL
    assert store.list_for_session("s1", open_only=True) == []
    resolved = try_resolve_go(
        "go",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
    )
    assert resolved.resolved is False
    assert resolved.status == "no_plan"


def test_cli_promotes_only_after_receipt_persistence(tmp_path):
    store = _store(tmp_path)
    persisted = []
    host = SimpleNamespace(
        api_mode="chat_completions",
        _persist_session=lambda messages, history, **_kwargs: (
            persisted.append((messages, history)) or True
        ),
    )

    captured = _capture_cli_bestplan_result(
        _result(),
        invocation_message="/bestplan fix it",
        session_id="s1",
        profile="coder",
        workspace="/tmp/work",
        host_agent=host,
        baseline_fingerprint="base-1",
        store=store,
    )

    assert persisted and persisted[0][0] == captured["messages"]
    rows = store.list_for_session("s1", open_only=False)
    assert len(rows) == 1
    assert rows[0]["state"] == PlanState.PENDING


def test_cli_real_sessiondb_rewrites_canonical_assistant_before_promotion(tmp_path):
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    session_id = "cli-bestplan-receipt"
    db.create_session(session_id=session_id, source="cli")
    store = _store(tmp_path)
    host = _real_agent(db, session_id)
    result = _result()

    # Reproduce the real handoff: run_conversation has already persisted and
    # stamped the model's raw assistant response before the CLI replaces it
    # with the host-rendered executable receipt.
    host._persist_session(result["messages"], [])
    assert result["messages"][-1]["_db_persisted"] is True

    captured = _capture_cli_bestplan_result(
        result,
        invocation_message="/bestplan fix it",
        session_id=session_id,
        profile="coder",
        workspace="/tmp/work",
        host_agent=host,
        baseline_fingerprint="base-1",
        store=store,
    )

    assert _stored_roles_and_content(db, session_id) == [
        (message["role"], message["content"])
        for message in captured["messages"]
    ]
    assert "Bestplan executable receipt:" in captured["messages"][-1]["content"]
    rows = store.list_for_session(session_id, open_only=False)
    assert len(rows) == 1
    assert rows[0]["state"] == PlanState.PENDING


def test_cli_real_sessiondb_write_failure_keeps_plan_provisional(tmp_path):
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    session_id = "cli-bestplan-write-failure"
    db.create_session(session_id=session_id, source="cli")
    store = _store(tmp_path)
    host = _real_agent(db, session_id)
    result = _result()
    host._persist_session(result["messages"], [])

    # A closed real SessionDB makes the receipt rewrite fail at SQLite while
    # the independent BestplanStore connection can still retain PROVISIONAL.
    db.close()
    with pytest.raises(RuntimeError, match="receipt persistence"):
        _capture_cli_bestplan_result(
            result,
            invocation_message="/bestplan fix it",
            session_id=session_id,
            profile="coder",
            workspace="/tmp/work",
            host_agent=host,
            baseline_fingerprint="base-1",
            store=store,
        )

    rows = store.list_for_session(session_id, open_only=False)
    assert len(rows) == 1
    assert rows[0]["state"] == PlanState.PROVISIONAL
    assert store.list_for_session(session_id, open_only=True) == []
    resolved = try_resolve_go(
        "go",
        session_id=session_id,
        profile="coder",
        workspace="/tmp/work",
        baseline_fingerprint="base-1",
        parent_agent=SimpleNamespace(),
        config=_config(),
        store=store,
    )
    assert resolved.resolved is False
    assert resolved.status == "no_plan"
