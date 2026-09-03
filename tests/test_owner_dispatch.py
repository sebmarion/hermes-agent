import threading

from tui_gateway.owner_inbox import OwnerDispatch, live_owner_dispatch
from tui_gateway import server
from tui_gateway.transport import bind_transport, current_transport, reset_transport


def test_owner_dispatch_rejects_busy_and_exactly_scopes_session():
    session = {"running": False, "history_lock": threading.RLock()}
    calls = []
    dispatch = OwnerDispatch(
        profile_name="default",
        lookup=lambda profile, sid: session if sid == "s1" else None,
        submit=lambda params, session: calls.append(params) or {"ok": True},
    )

    assert dispatch.submit("default", "s1", "a1", "hello", 3)["ok"]
    assert len(calls) == 1
    session["running"] = True
    assert dispatch.submit("default", "s1", "a2", "again", 3)["status"] == "busy"
    assert dispatch.submit("other", "s1", "a3", "wrong", 3)["status"] == "waiting"
    assert len(calls) == 1


def test_real_prompt_registry_maps_durable_id_and_preserves_transport(monkeypatch):
    transport = object()
    session = {"session_key": "durable-1", "running": False,
               "history_lock": threading.RLock(), "transport": transport}
    seen = []
    def handler(rid, params):
        assert current_transport() is transport
        seen.append((rid, params))
        return {"ok": True}
    monkeypatch.setitem(server._methods, "prompt.submit", handler)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-1": session})
    def submit(params, live_session):
        token = bind_transport(live_session["transport"])
        try:
            return server._methods["prompt.submit"](params["_owner_request_id"], params)
        finally:
            reset_transport(token)
    owner = live_owner_dispatch(server, submit)
    result = owner.submit("default", "durable-1", "a1", "hello", 3)
    assert result["status"] == "submitted"
    assert seen[0][1]["session_id"] == "live-1"
    session["running"] = True
    admitted = []
    result = owner.submit("default", "durable-1", "a2", "busy", 3,
                          admit=lambda: admitted.append(True))
    assert result["status"] == "busy"
    assert admitted == []


def test_owner_lookup_accepts_agent_durable_session_id(monkeypatch):
    session = {"session_key": "", "running": False,
               "history_lock": threading.RLock(),
               "agent": type("Agent", (), {"session_id": "durable-agent"})()}
    monkeypatch.setattr(server, "_sessions", {"live-agent": session})
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    dispatch = live_owner_dispatch(server, lambda params, s: {"ok": True})
    assert dispatch.lookup("default", "durable-agent") is session


def test_compression_continuation_prefers_active_agent_id(monkeypatch):
    session = {"session_key": "ended-parent", "agent": type("A", (), {
        "session_id": "active-continuation"})(), "running": False,
        "history_lock": threading.RLock()}
    monkeypatch.setattr(server, "_sessions", {"live-compressed": session})
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    dispatch = live_owner_dispatch(server, lambda params, s: {"ok": True})
    assert dispatch.lookup("default", "active-continuation") is session


def test_owner_snapshot_exposes_authoritative_pending_input(monkeypatch):
    session = {"session_key": "durable-pending", "running": False,
               "last_active": 123.0, "history_lock": threading.RLock()}
    monkeypatch.setattr(server, "_sessions", {"live-pending": session})
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_pending", {"r1": ("live-pending", object())})
    monkeypatch.setattr(server, "_pending_prompt_payloads",
                        {"r1": ("clarify.request", {"question": "ship?"})})
    dispatch = live_owner_dispatch(server, lambda params, s: {"ok": True})
    snap = dispatch.snapshot("default", "durable-pending")
    assert snap["last_active"] == 123.0
    assert snap["pending_input"] is True
    assert snap["pending_payload"]["question"] == "ship?"


def test_snapshot_uses_authoritative_agent_activity_summary(monkeypatch):
    class Agent:
        session_id = "durable-activity"
        def get_activity_summary(self):
            return {"last_activity_at": 123.5,
                    "last_activity_provenance": "tool",
                    "current_tool": "exec"}
    session = {"session_key": "durable-activity", "running": False,
               "history_lock": threading.RLock(), "agent": Agent()}
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {"live-activity": session})
    dispatch = live_owner_dispatch(server, lambda params, session: {"ok": True})
    snap = dispatch.snapshot("default", "durable-activity")
    assert snap["last_active"] == 123.5
    assert snap["active_tools"] is True