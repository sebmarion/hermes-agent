import io
import threading
import types

import pytest

from agent.bestplan_orchestrator import DEFAULT_EXPLORER_COUNT, normalize_count
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def _prompt_spy(monkeypatch):
    calls = []

    def fake(rid, params):
        calls.append(dict(params))
        return server._ok(rid, {"status": "streaming"})

    monkeypatch.setitem(server._methods, "prompt.submit", fake)
    return calls


@pytest.mark.parametrize("arg", ["", "   ", "3"])
def test_bestplan_submit_rejects_empty_or_numeric_arg(monkeypatch, arg):
    calls = _prompt_spy(monkeypatch)
    handler = server._methods["bestplan.submit"]
    response = handler("rid-error", {"session_id": "sid-1", "arg": arg})
    assert response["error"]["code"] == 4004
    assert calls == []


@pytest.mark.parametrize(
    "arg,expected_count,expected_task",
    [
        ("repair it", DEFAULT_EXPLORER_COUNT, "repair it"),
        ("1 repair it", normalize_count("1"), "repair it"),
    ],
)
def test_bestplan_submit_forwards_normalized_task(
    monkeypatch, arg, expected_count, expected_task
):
    calls = _prompt_spy(monkeypatch)
    handler = server._methods["bestplan.submit"]
    response = handler("rid-ok", {"session_id": "sid-1", "arg": arg})
    assert len(calls) == 1
    forwarded = calls[0]
    assert forwarded["session_id"] == "sid-1"
    assert forwarded["text"] == expected_task
    assert forwarded["_bestplan_config"] == {"count": expected_count}
    assert forwarded["_bestplan_authority"] is server._BESTPLAN_SUBMIT_AUTHORITY
    assert response["result"]["output"] == "BestPlan started."


def test_compute_host_turn_frame_preserves_bestplan_config(monkeypatch):
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "cols": 80,
        "session_key": "key",
    }
    monkeypatch.setattr(server, "_session_cwd", lambda session: "/tmp")
    monkeypatch.setattr(server, "_session_source", lambda session: "test")
    frame = server._compute_host_turn_frame(
        "rid-frame", "sid-frame", session, "task"
    )
    assert "bestplan_config" not in frame
    frame2 = server._compute_host_turn_frame(
        "rid-frame",
        "sid-frame",
        session,
        "task",
        bestplan_config={"count": 2},
    )
    assert frame2["bestplan_config"] == {"count": 2}


def test_compute_host_run_real_turn_forwards_bestplan_config(monkeypatch):
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "session_key": "sk",
        "running": False,
        "attached_images": [],
        "agent": types.SimpleNamespace(),
    }
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *args, **kwargs: {})
    received = []

    def fake_run_prompt_submit(
        rid,
        sid,
        current_session,
        text,
        *,
        bestplan_config=None,
        **kwargs,
    ):
        received.append(bestplan_config)

    monkeypatch.setattr(server, "_run_prompt_submit", fake_run_prompt_submit)
    try:
        host._run_real_turn(
            {
                "sid": "sid",
                "request_id": "req1",
                "text": "hello",
                "bestplan_config": {"count": 2},
            }
        )
    finally:
        host.close()
    assert received == [{"count": 2}]


def test_capture_gateway_bestplan_result_commits_after_durable_receipt(monkeypatch):
    from tui_gateway.bestplan import capture_gateway_bestplan_result

    source = {"final_response": "plan"}
    captured = {
        "final_response": "receipt",
        "bestplan_capture": {
            "executable": True,
            "plan_id": "bp-gateway",
            "receipt_persisted": True,
        },
    }
    commits = []

    class Store:
        def commit_provisional_plan(self, plan_id):
            commits.append(plan_id)
            return plan_id == "bp-gateway"

        def close(self):
            raise AssertionError("injected store must not be closed")

    store = Store()
    host_agent = types.SimpleNamespace(_persist_session=lambda *args, **kwargs: True)

    def fake_capture(result, **kwargs):
        assert result is source
        assert kwargs["provisional"] is True
        assert kwargs["local_execution"] is True
        assert kwargs["store"] is store
        assert kwargs["host_agent"] is host_agent
        return captured

    monkeypatch.setattr("agent.bestplan_state.capture_bestplan_agent_result", fake_capture)

    returned = capture_gateway_bestplan_result(
        source,
        invocation_message="/bestplan fix it",
        session_id="s1",
        profile="coder",
        workspace="/tmp",
        host_agent=host_agent,
        store=store,
    )

    assert returned is captured
    assert commits == ["bp-gateway"]


def test_capture_gateway_bestplan_result_rejects_undurable_receipt(monkeypatch):
    from tui_gateway.bestplan import capture_gateway_bestplan_result

    source = {"final_response": "plan"}
    commits = []

    class Store:
        def commit_provisional_plan(self, plan_id):
            commits.append(plan_id)
            return True

        def close(self):
            raise AssertionError("injected store must not be closed")

    store = Store()
    host_agent = types.SimpleNamespace(_persist_session=lambda *args, **kwargs: True)

    def fake_capture(result, **kwargs):
        return {
            "bestplan_capture": {
                "executable": True,
                "plan_id": "bp-gateway",
                "receipt_persisted": False,
            }
        }

    monkeypatch.setattr("agent.bestplan_state.capture_bestplan_agent_result", fake_capture)

    with pytest.raises(RuntimeError, match="receipt persistence"):
        capture_gateway_bestplan_result(
            source,
            invocation_message="/bestplan fix it",
            session_id="s1",
            profile="coder",
            workspace="/tmp",
            host_agent=host_agent,
            store=store,
        )

    assert commits == []


def _prompt_submit_session():
    return {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "session_key": "sk",
        "running": False,
        "attached_images": [],
        "agent": types.SimpleNamespace(),
    }


def _configure_prompt_submit(monkeypatch, session, *, isolated):
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_sess_nowait", lambda params, rid: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, current: None)
    monkeypatch.setattr(
        server, "_load_dashboard_process_isolation_config", lambda: {}
    )
    monkeypatch.setattr(
        server,
        "_session_uses_compute_host",
        lambda current, cfg=None: isolated,
    )
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *args, **kwargs: None)


def _trusted_prompt_params():
    return {
        "session_id": "sid",
        "text": "fix it",
        "_bestplan_config": {"count": 2},
        "_bestplan_authority": server._BESTPLAN_SUBMIT_AUTHORITY,
    }


def test_prompt_submit_forwards_trusted_config_to_compute_host(monkeypatch):
    session = _prompt_submit_session()
    _configure_prompt_submit(monkeypatch, session, isolated=True)
    received = []

    def fake_submit(rid, sid, current, text, *, bestplan_config=None, **kwargs):
        received.append(bestplan_config)
        return server._ok(rid, {"status": "streaming"})

    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", fake_submit)
    response = server._methods["prompt.submit"]("rid", _trusted_prompt_params())

    assert response["result"]["status"] == "streaming"
    assert received == [{"count": 2}]


def test_prompt_submit_does_not_trust_external_private_config(monkeypatch):
    session = _prompt_submit_session()
    _configure_prompt_submit(monkeypatch, session, isolated=True)
    received = []

    def fake_submit(rid, sid, current, text, *, bestplan_config=None, **kwargs):
        received.append(bestplan_config)
        return server._ok(rid, {"status": "streaming"})

    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", fake_submit)
    response = server._methods["prompt.submit"](
        "rid",
        {
            "session_id": "sid",
            "text": "ordinary",
            "_bestplan_config": {"count": 2},
        },
    )

    assert response["result"]["status"] == "streaming"
    assert received == [None]


def test_prompt_submit_forwards_trusted_config_to_inline_runner(monkeypatch):
    class InlineThread:
        def __init__(self, target=None, **kwargs):
            self.target = target

        def start(self):
            self.target()

    session = _prompt_submit_session()
    _configure_prompt_submit(monkeypatch, session, isolated=False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda current: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda current: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, current: None)
    monkeypatch.setattr(
        server, "_wait_agent_for_prompt", lambda current, rid, sid: None
    )
    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    received = []

    def fake_run(rid, sid, current, text, *, bestplan_config=None, **kwargs):
        received.append(bestplan_config)

    monkeypatch.setattr(server, "_run_prompt_submit", fake_run)
    response = server._methods["prompt.submit"]("rid", _trusted_prompt_params())

    assert response["result"]["status"] == "streaming"
    assert received == [{"count": 2}]


def test_busy_bestplan_is_queued_with_config_and_never_steered(monkeypatch):
    steered = []
    session = {
        "history_lock": threading.Lock(),
        "running": True,
        "attached_images": [],
        "agent": types.SimpleNamespace(steer=lambda text: steered.append(text) or True),
    }
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")

    response = server._handle_busy_submit(
        "rid",
        "sid",
        session,
        "fix it",
        None,
        bestplan_config={"count": 2},
    )

    assert response["result"]["status"] == "queued"
    assert steered == []
    assert session["queued_prompt"]["bestplan_config"] == {"count": 2}


def test_drain_queued_bestplan_forwards_config(monkeypatch):
    session = {
        "history_lock": threading.Lock(),
        "running": False,
        "queued_prompt": {
            "text": "fix it",
            "transport": None,
            "bestplan_config": {"count": 2},
        },
    }
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda current: False)
    received = []

    def fake_run(rid, sid, current, text, *, bestplan_config=None, **kwargs):
        received.append(bestplan_config)

    monkeypatch.setattr(server, "_run_prompt_submit", fake_run)

    assert server._drain_queued_prompt("rid", "sid", session) is True
    assert received == [{"count": 2}]
