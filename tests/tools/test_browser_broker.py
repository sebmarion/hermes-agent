"""Zeus browser broker (browserd) routing on the browser_exec session layer."""

from unittest.mock import Mock

import pytest

from tools import browser_cdp_tool
from tools import browser_session as session
from tools import browser_session_lifecycle as lifecycle
from tools import browser_tool_broker as broker
from tools import browser_use_cli


def _lease(**extra):
    info = {
        "session_name": "broker_test", "bb_session_id": None,
        "cdp_url": "http://127.0.0.1:9400",
        "features": {"local": True, "browserd": True},
        "browserd_lease_id": "lease-1", "browserd_session": "hermes-test",
    }
    info.update(extra)
    return info


def test_broker_precedes_cloud(monkeypatch):
    provider = Mock()
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_session_cloud.get_cloud_provider", lambda: provider)
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "acquire", lambda key, persistent, ttl: _lease())
    info = session._create_session_for_key("task-1", False)
    assert info["features"]["browserd"] is True
    assert info["cdp_url"] == "http://127.0.0.1:9400"
    provider.create_session.assert_not_called()


def test_private_sidecar_broker_is_ephemeral(monkeypatch):
    seen = {}
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
    monkeypatch.setattr(broker, "enabled", lambda: True)

    def acquire(key, persistent, ttl):
        seen.update(key=key, persistent=persistent, ttl=ttl)
        return _lease()

    monkeypatch.setattr(broker, "acquire", acquire)
    session._create_session_for_key("task::local", True)
    assert seen == {"key": "task::local", "persistent": False, "ttl": 300}


def test_broker_rejects_direct_cdp_override(monkeypatch):
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "http://127.0.0.1:9334")
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "acquire", Mock(side_effect=AssertionError("must not acquire after policy violation")))
    with pytest.raises(RuntimeError, match="direct CDP overrides are disabled"):
        session._create_session_for_key("task-direct", False)


def test_direct_cdp_still_supported_without_broker(monkeypatch):
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "http://127.0.0.1:9444")
    monkeypatch.setattr(broker, "enabled", lambda: False)
    info = session._create_session_for_key("task-direct", False)
    assert info["features"]["cdp_override"] is True
    assert info["cdp_url"] == "http://127.0.0.1:9444"


@pytest.mark.parametrize("persistent,close_worker", [(True, False), (False, True)])
def test_broker_teardown_releases_lease(monkeypatch, persistent, close_worker):
    info = _lease(browserd_persistent=persistent, launcher_key="must-not-stop")
    released = []
    monkeypatch.setattr(broker, "release", lambda got, close_worker=True: released.append((got, close_worker)))
    monkeypatch.setattr("tools.browser_launcher.stop_browser", Mock(side_effect=AssertionError("broker owns Chrome")))
    lifecycle.release_session_backend("task", info)
    assert released == [(info, close_worker)]


def test_healthy_lease_is_renewed_on_reuse(monkeypatch):
    renewed = []
    monkeypatch.setattr(broker, "renew", lambda info, ttl=300: renewed.append(ttl) or True)
    assert session._backend_dead(_lease()) is False
    assert renewed == [300]


def test_unhealthy_lease_is_replaced(monkeypatch):
    def renew(info, ttl=300):
        raise RuntimeError("browser broker unavailable: ConnectionRefusedError")

    monkeypatch.setattr(broker, "renew", renew)
    assert session._backend_dead(_lease()) is True


def test_browser_cdp_has_no_direct_endpoint_under_broker(monkeypatch):
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "http://127.0.0.1:9334")
    monkeypatch.setattr(broker, "enabled", lambda: True)
    assert browser_cdp_tool._resolve_cdp_endpoint() == ""


def test_browser_exec_rejects_operator_cdp_env_under_broker(monkeypatch):
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(browser_use_cli, "_resolve_local_cdp", Mock(side_effect=AssertionError("must not lease")))
    err = browser_use_cli._route_backend({"BU_CDP_URL": "http://127.0.0.1:9334"}, "", "task", False)
    assert "direct CDP overrides are disabled" in err


def test_browser_exec_leases_through_session_layer_under_broker(monkeypatch):
    calls = []
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(browser_use_cli, "_resolve_real_profile_cdp",
                        Mock(side_effect=AssertionError("real profile must not bypass the broker")))
    monkeypatch.setattr(browser_use_cli, "_resolve_local_cdp",
                        lambda env, task_id, session_name="", sidecar=False: calls.append((task_id, sidecar)))
    assert browser_use_cli._route_backend({}, "", "task", False) is None
    assert browser_use_cli._route_backend({}, "", "task", False, force_local_sidecar=True) is None
    assert calls == [("task", False), ("task", True)]


def test_configured_broker_session_is_shared(monkeypatch):
    monkeypatch.setattr(broker, "_configured_session", lambda: "hermes-shared-main")
    assert broker._session_key("task-a") == "hermes-shared-main"
    assert broker._session_key("task-b") == "hermes-shared-main"


def test_invalid_broker_session_rejected(monkeypatch):
    monkeypatch.setattr(broker, "_cfg", lambda key: "../bad" if key == "broker_session" else "")
    with pytest.raises(RuntimeError, match="invalid browser.broker_session"):
        broker._configured_session()
