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
        "browserd_session": "hermes-test", "browserd_persistent": True,
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
    assert released == [(info, False)]
    assert "task" not in browser_tool._active_sessions


def test_configured_broker_session_is_shared(monkeypatch):
    monkeypatch.setattr(broker, "_configured_session", lambda: "hermes-shared-main")
    assert broker._session_key("task-a") == "hermes-shared-main"
    assert broker._session_key("task-b") == "hermes-shared-main"


def test_invalid_broker_session_rejected(monkeypatch):
    def cfg(key, default, cast, source):
        return "../bad" if key == "broker_session" else default
    monkeypatch.setattr(broker._bt, "_browser_cfg", cfg)
    try:
        broker._configured_session()
        assert False, "invalid session should fail"
    except RuntimeError as exc:
        assert "invalid browser.broker_session" in str(exc)


def test_ephemeral_broker_cleanup_closes_worker(monkeypatch):
    info = {
        "session_name": "broker_ephemeral", "bb_session_id": None,
        "features": {"browserd": True}, "browserd_lease_id": "lease-e",
        "browserd_session": "hermes-e", "browserd_persistent": False,
    }
    released = []
    monkeypatch.setattr(browser_tool, "_active_sessions", {"task-e": info})
    monkeypatch.setattr(browser_tool, "_session_last_activity", {"task-e": 1.0})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
    monkeypatch.setattr(broker, "release", lambda got, close_worker=True: released.append((got, close_worker)))
    monkeypatch.setattr("tools.browser_tool_cdp._stop_cdp_supervisor", lambda *_: None)
    monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda *_: None)
    monkeypatch.setattr(lifecycle, "_kill_verified_daemon", lambda *_: False)
    lifecycle._cleanup_single_browser_session("task-e")
    assert released == [(info, True)]


def test_broker_rejects_direct_cdp_override(monkeypatch):
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "http://127.0.0.1:9334")
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "acquire", Mock(side_effect=AssertionError("must not acquire after policy violation")))
    try:
        session._create_session_for_key("task-direct", False)
        assert False, "direct CDP must fail closed while broker is configured"
    except RuntimeError as exc:
        assert "direct CDP overrides are disabled" in str(exc)


def test_direct_cdp_still_supported_without_broker(monkeypatch):
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "http://127.0.0.1:9444")
    monkeypatch.setattr(broker, "enabled", lambda: False)
    info = session._create_session_for_key("task-direct", False)
    assert info["features"]["cdp_override"] is True
    assert info["cdp_url"] == "http://127.0.0.1:9444"
