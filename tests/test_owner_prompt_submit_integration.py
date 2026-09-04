import threading
from types import SimpleNamespace

from hermes_cli import plugins as plugins_mod
from tui_gateway import server
from tui_gateway.owner_inbox import live_owner_dispatch
from tui_gateway.transport import bind_transport, current_transport, reset_transport


def test_real_prompt_submit_busy_race_has_no_admission(monkeypatch):
    import tui_gateway.methods_prompt as prompt
    transport = object()
    session = {"session_key": "durable-1", "running": False,
               "history_lock": threading.RLock(), "transport": transport}
    handler = server._methods["prompt.submit"]
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-1": session})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, s: None)
    def submit(params, live_session):
        token = bind_transport(live_session["transport"])
        try:
            with live_session["history_lock"]:
                live_session["running"] = True
            return handler(params["_owner_request_id"], params)
        finally:
            reset_transport(token)
    owner = live_owner_dispatch(server, submit)
    admitted = []
    result = owner.submit("default", "durable-1", "a1", "hello", 3,
                          admit=lambda: admitted.append(True))
    assert result["error"]["code"] == 4091
    assert admitted == []
    assert current_transport() is None


def test_real_prompt_submit_idle_path_admits_inside_claim(monkeypatch):
    import time
    transport = object()
    session = {"session_key": "durable-2", "running": False,
               "history_lock": threading.RLock(), "history": [],
               "history_version": 0, "transport": transport}
    handler = server._methods["prompt.submit"]
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-2": session})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, s: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s, c: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda s: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda s: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, s: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda s, rid, sid: {"error": {"message": "stub"}})
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    admitted = []
    def admit():
        with session["history_lock"]:
            admitted.append(session["running"])
        return True
    token = bind_transport(transport)
    try:
        result = handler("r1", {"session_id": "live-2", "text": "go",
                                 "_owner_admit": admit})
    finally:
        reset_transport(token)
    time.sleep(0.05)
    assert result["result"]["status"] == "streaming"
    assert admitted == []
    assert session.get("_owner_admit_callback") is None
    assert session["running"] is False


def test_prompt_storage_failure_precedes_owner_admission(monkeypatch):
    session = {"session_key": "durable-fail", "running": False,
               "history_lock": threading.RLock(), "history": [],
               "history_version": 0}
    handler = server._methods["prompt.submit"]
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-fail": session})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, s: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda s: False)
    observer_calls = []
    monkeypatch.setattr(plugins_mod, "fire_pre_prompt_admission", lambda **kw: observer_calls.append(kw))
    started = []
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: started.append(True))
    admitted = []
    prepared = []
    result = handler("r2", {"session_id": "live-fail", "text": "go",
                              "_owner_prepare": lambda: prepared.append(True),
                              "_owner_admit": lambda: admitted.append(True) or True})
    assert result["error"]["code"] == 5072
    assert admitted == []
    assert prepared == [True]
    assert observer_calls and observer_calls[0]["is_owner_reply"] is False
    assert started == []
    assert session["running"] is False


def test_chief_automation_turn_does_not_reset_genuine_activity_clock():
    from run_agent import AIAgent

    agent = SimpleNamespace(
        _last_activity_ts=None, _last_activity_desc=None,
        _last_activity_provenance=None, _chief_automation_turn=True,
        _persist_session_activity_if_due=lambda: None,
    )
    AIAgent._touch_activity(agent, "Chief progress summary")
    assert getattr(agent, "_genuine_activity_ts", None) is None
    agent._chief_automation_turn = False
    AIAgent._touch_activity(agent, "real owned work")
    assert agent._genuine_activity_ts == agent._last_activity_ts