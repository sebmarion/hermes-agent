"""Regression tests for async delegation registration ordering."""

import json
import socket
import threading
from contextlib import ExitStack
from types import SimpleNamespace as NS

import pytest

from hermes_state import SessionDB
from tools import async_delegation as producer
from tools import delegate_tool as delegate


def _invoke_real_delegate(tmp_path, monkeypatch, *, reject_build):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")

    db = SessionDB(tmp_path / "state.db")
    parent_id = db.create_session("synthetic-parent", source="desktop")
    child_id = db.create_session(
        "synthetic-child", source="delegate", parent_session_id=parent_id
    )
    parent = NS(
        session_id=parent_id,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _current_turn_id="synthetic-turn",
    )
    child = NS(
        session_id=child_id,
        _session_init_model_config={
            "_origin": {
                "version": 1,
                "launch_id": "synthetic-launch",
                "created_session_id": child_id,
                "parent_session_id": parent_id,
            }
        },
        _delegate_role="leaf",
        _subagent_id="synthetic-subagent",
    )
    events = []
    scheduled = []
    real_bind = producer.bind_child_delegation
    real_persist = producer._persist_dispatch

    def bind(*args, **kwargs):
        result = real_bind(*args, **kwargs)
        events.append(("bind", result))
        return result

    def persist(record):
        events.append(("persist", record["delegation_id"]))
        return real_persist(record)

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network/model call")

    try:
        with ExitStack() as stack:
            stack.enter_context(
                monkeypatch.context()
            )
            monkeypatch.setattr(
                delegate,
                "_resolve_delegation_credentials",
                lambda *args, **kwargs: {
                    "model": "synthetic/model",
                    "provider": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            )
            if reject_build:
                monkeypatch.setattr(
                    delegate,
                    "_build_child_preserving_parent_tools",
                    lambda **kwargs: (_ for _ in ()).throw(
                        ValueError("Synthetic invalid explicit child pin")
                    ),
                )
            else:
                monkeypatch.setattr(
                    delegate,
                    "_build_child_preserving_parent_tools",
                    lambda **kwargs: child,
                )
            monkeypatch.setattr(producer, "bind_child_delegation", bind)
            monkeypatch.setattr(producer, "_persist_dispatch", persist)
            monkeypatch.setattr(
                producer, "_get_executor", lambda *args, **kwargs: NS(submit=scheduled.append)
            )
            monkeypatch.setattr(producer, "_ensure_stale_monitor", lambda: None)
            stack.enter_context(
                __import__("unittest.mock", fromlist=["patch"]).patch.object(
                    socket.socket, "connect", no_network
                )
            )
            stack.enter_context(
                __import__("unittest.mock", fromlist=["patch"]).patch(
                    "gateway.session_context.async_delivery_supported", return_value=True
                )
            )
            response = json.loads(
                delegate.delegate_task(
                    goal="Synthetic child work",
                    background=True,
                    parent_agent=parent,
                )
            )
    finally:
        db.close()

    rows = []
    with producer._transaction() as conn:
        rows = conn.execute(
            "SELECT delegation_id, state FROM async_delegations"
        ).fetchall()
    delegation_id = response.get("delegation_id")
    if delegation_id:
        with producer._records_lock:
            producer._records.pop(delegation_id, None)
    return response, events, scheduled, rows


def test_rejected_child_build_creates_no_async_row(tmp_path, monkeypatch):
    response, events, scheduled, rows = _invoke_real_delegate(
        tmp_path, monkeypatch, reject_build=True
    )

    assert response["error"] == "Synthetic invalid explicit child pin"
    assert events == []
    assert scheduled == []
    assert rows == []


def test_successful_dispatch_persists_then_binds_before_schedule(tmp_path, monkeypatch):
    response, events, scheduled, rows = _invoke_real_delegate(
        tmp_path, monkeypatch, reject_build=False
    )

    assert response["status"] == "dispatched"
    assert [stage for stage, _value in events] == ["persist", "bind"]
    assert events[1][1] is True
    assert len(scheduled) == 1
    assert len(rows) == 1
    assert rows[0][0] == response["delegation_id"]


def test_persist_dispatch_rejects_duplicate_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    db = SessionDB(tmp_path / "state.db")
    record = {
        "delegation_id": "duplicate-delegation",
        "session_key": "parent",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": "parent",
        "dispatched_at": 1.0,
    }
    try:
        producer._persist_dispatch(record)
        with pytest.raises(Exception):
            producer._persist_dispatch(record)
    finally:
        db.close()
