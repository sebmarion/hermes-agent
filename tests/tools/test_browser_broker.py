from unittest.mock import Mock

import tools.browser_tool as browser_tool
from tools import browser_tool_broker as broker
from tools import browser_tool_session as session
from tools import browser_tool_lifecycle as lifecycle


def test_broker_precedes_cloud(monkeypatch):
    provider = Mock()
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(
        broker, "acquire",
        lambda task_id, persistent, ttl: {
            "session_name": "broker_test", "bb_session_id": None,
            "cdp_url": "http://127.0.0.1:9400",
            "features": {"local": True, "browserd": True},
            "browserd_lease_id": "lease-1", "browserd_session": "hermes-test",
        },
    )
    monkeypatch.setattr("tools.browser_tool_cloud._get_cloud_provider", lambda: provider)
    info = session._create_session_for_key("task-1", False)
    assert info["features"]["browserd"] is True
    assert info["cdp_url"] == "http://127.0.0.1:9400"
    provider.create_session.assert_not_called()


def test_private_sidecar_broker_is_ephemeral(monkeypatch):
    seen = {}
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr(broker, "enabled", lambda: True)
    def acquire(task_id, persistent, ttl):
        seen.update(task_id=task_id, persistent=persistent, ttl=ttl)
        return {"features": {"browserd": True}}
    monkeypatch.setattr(broker, "acquire", acquire)
    session._create_session_for_key("task::local", True)
    assert seen == {"task_id": "task::local", "persistent": False, "ttl": 300}


def test_broker_cleanup_releases_owner(monkeypatch):
    info = {
        "session_name": "broker_test", "bb_session_id": None,
        "features": {"browserd": True}, "browserd_lease_id": "lease-1",
        "browserd_session": "hermes-test",
    }
    released = []
    monkeypatch.setattr(browser_tool, "_active_sessions", {"task": info})
    monkeypatch.setattr(browser_tool, "_session_last_activity", {"task": 1.0})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
    monkeypatch.setattr(broker, "release", lambda got, close_worker=True: released.append((got, close_worker)))
    monkeypatch.setattr("tools.browser_tool_cdp._stop_cdp_supervisor", lambda *_: None)
    monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda *_: None)
    monkeypatch.setattr(lifecycle, "_kill_verified_daemon", lambda *_: False)
    lifecycle._cleanup_single_browser_session("task")
    assert released == [(info, True)]
    assert "task" not in browser_tool._active_sessions
