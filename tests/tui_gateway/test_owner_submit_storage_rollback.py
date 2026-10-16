"""A rejected owner prompt must not lend its authority to a later ordinary turn."""

import sqlite3

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.prompt_turn import _TurnRun


class _RecordingAgent:
    def __init__(self, session_id):
        self.session_id = session_id
        self.turns = []

    def run_conversation(
        self, prompt, conversation_history=None, stream_callback=None,
        persist_user_message=None, persist_user_display_kind=None,
        persist_user_display_metadata=None, task_id=None,
    ):
        self.turns.append({
            "text": prompt,
            "owner_admit": self._owner_admit,
            "chief_automation": self._chief_automation_turn,
            "display_kind": persist_user_display_kind,
            "display_metadata": persist_user_display_metadata,
        })
        return {"final_response": "reply", "messages": []}


def _session(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    response = server.handle_request({
        "id": "create", "method": "session.create",
        "params": {"cols": 96, "source": "desktop"},
    })
    assert "result" in response, response
    sid = response["result"]["session_id"]
    session = server._sessions[sid]
    agent = _RecordingAgent(response["result"]["stored_session_id"])
    session["agent"] = agent
    return sid, session, agent


def _claim(sid, session, text, params):
    error, _fields = server._lock_in_submit_turn(
        "submit", sid, session, text, params, False, None, None, None,
    )
    assert error is None, error


def _invoke(sid, session, agent, text):
    turn = _TurnRun(agent, None, None, True)
    server._invoke_agent(
        sid, session, turn, text, text, None, [], None, None, text=text,
    )
    return agent.turns[-1]


def _owner_params(callback):
    return {
        "_owner_admit": callback,
        "_owner_action_id": "owner-action",
        "_owner_request_id": "owner-request",
        "_chief_automation": True,
        "_owner_durable_session_id": "durable-owner",
        "profile": "owner-profile",
        "work_generation": 7,
    }


def test_failed_owner_persist_does_not_authorize_a_later_ordinary_turn(
    monkeypatch, tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, session, agent = _session(monkeypatch, db)
    owner_admit = lambda: None
    try:
        _claim(sid, session, "rejected owner reply", _owner_params(owner_admit))

        def disk_full(*_args, **_kwargs):
            raise sqlite3.OperationalError("database or disk is full")

        with monkeypatch.context() as failure:
            failure.setattr(db, "append_message", disk_full)
            error = server._persist_session_row_for_submit(
                "owner-submit", session, "rejected owner reply", None,
            )
        assert error["error"]["code"] == 5070
        assert session["running"] is False
        assert session["inflight_turn"] is None

        _claim(sid, session, "ordinary follow-up", {})
        assert server._persist_session_row_for_submit(
            "ordinary-submit", session, "ordinary follow-up", None,
        ) is None
        ordinary = _invoke(sid, session, agent, "ordinary follow-up")

        assert ordinary == {
            "text": "ordinary follow-up",
            "owner_admit": None,
            "chief_automation": False,
            "display_kind": None,
            "display_metadata": None,
        }
        assert [row["content"] for row in db.get_messages(agent.session_id)] == [
            "ordinary follow-up",
        ]
        assert not any(key in session for key in (
            "_owner_admit_callback", "_owner_action_id", "_owner_request_id",
            "_owner_source_user_message_row_id", "_owner_durable_session_id",
            "_owner_profile", "_chief_automation",
        ))
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_successful_owner_persist_retains_its_own_admission(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, session, agent = _session(monkeypatch, db)
    owner_admit = lambda: None
    try:
        _claim(sid, session, "owner reply", _owner_params(owner_admit))
        assert server._persist_session_row_for_submit(
            "owner-submit", session, "owner reply", None,
        ) is None
        owner = _invoke(sid, session, agent, "owner reply")

        assert owner == {
            "text": "owner reply",
            "owner_admit": owner_admit,
            "chief_automation": True,
            "display_kind": "chief_reply",
            "display_metadata": {
                "owner_action_id": "owner-action",
                "owner_request_id": "owner-request",
                "owner_source_user_message_row_id": 7,
            },
        }
    finally:
        server._sessions.pop(sid, None)
        db.close()
