"""Desktop/TUI heartbeat delivery tests."""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import goals

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
    goals._DB_CACHE.clear()
    reset_hermes_home_override(token)


@pytest.fixture()
def resumed_session(server):
    sid = "desktop-ui-session"
    session_key = "persisted-desktop-session"
    session = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = session
    return sid, session_key, session


def test_resumed_desktop_session_fires_persisted_due_heartbeat(server, resumed_session):
    sid, session_key, session = resumed_session
    from hermes_cli.heartbeat import HeartbeatManager, save_heartbeat

    manager = HeartbeatManager(session_key)
    state = manager.set("check the deployment", interval_seconds=60)
    state.created_at = time.time() - 61
    save_heartbeat(session_key, state)

    fired = {}

    def fake_submit(rid, sid_, session_, text, **kwargs):
        fired.update(rid=rid, sid=sid_, session=session_, text=text)

    with patch.object(server, "_run_prompt_submit", fake_submit), patch.object(
        server, "_emit"
    ):
        server._maybe_fire_tui_heartbeat(sid, session)

    assert fired["sid"] == sid
    assert fired["session"] is session
    assert "check the deployment" in fired["text"]
    assert session["running"] is True
    reloaded = HeartbeatManager(session_key).state
    assert reloaded is not None
    assert reloaded.fire_count == 1


def test_session_notification_poller_drives_heartbeat_after_restart(
    server, resumed_session
):
    sid, _, session = resumed_session
    stop = threading.Event()
    called = threading.Event()

    def fake_heartbeat(sid_, session_):
        assert sid_ == sid
        assert session_ is session
        called.set()

    with patch.object(server, "_maybe_fire_tui_heartbeat", fake_heartbeat):
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, sid, session),
            daemon=True,
        )
        thread.start()
        observed = called.wait(0.2)
        stop.set()
        thread.join(timeout=1)

    assert observed is True


def test_named_profile_heartbeat_uses_profile_database(server, resumed_session, tmp_path):
    sid, session_key, session = resumed_session
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    session["profile_home"] = str(profile_home)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.heartbeat import HeartbeatManager, save_heartbeat

    token = set_hermes_home_override(str(profile_home))
    try:
        state = HeartbeatManager(session_key).set("profile check", 60)
        state.created_at = time.time() - 61
        save_heartbeat(session_key, state)
    finally:
        reset_hermes_home_override(token)

    fired = {}
    with patch.object(
        server,
        "_run_prompt_submit",
        side_effect=lambda rid, sid_, session_, text, **kwargs: fired.update(text=text) or True,
    ), patch.object(server, "_emit"):
        server._maybe_fire_tui_heartbeat(sid, session)

    assert "profile check" in fired["text"]
    token = set_hermes_home_override(str(profile_home))
    try:
        assert HeartbeatManager(session_key).state.fire_count == 1
    finally:
        reset_hermes_home_override(token)


def test_failed_heartbeat_dispatch_is_retryable(server, resumed_session):
    sid, session_key, session = resumed_session
    from hermes_cli.heartbeat import HeartbeatManager, save_heartbeat

    state = HeartbeatManager(session_key).set("retry check", 60)
    state.created_at = time.time() - 61
    save_heartbeat(session_key, state)

    with patch.object(server, "_run_prompt_submit", return_value=False), patch.object(
        server, "_emit"
    ):
        server._maybe_fire_tui_heartbeat(sid, session)

    reloaded = HeartbeatManager(session_key).state
    assert reloaded is not None
    assert reloaded.fire_count == 0
    assert reloaded.last_fired_at == 0.0
    assert session["running"] is False


def test_heartbeat_does_not_emit_duplicate_message_start(server, resumed_session):
    sid, session_key, session = resumed_session
    from hermes_cli.heartbeat import HeartbeatManager, save_heartbeat

    state = HeartbeatManager(session_key).set("single start", 60)
    state.created_at = time.time() - 61
    save_heartbeat(session_key, state)
    events = []
    with patch.object(server, "_run_prompt_submit", return_value=True), patch.object(
        server, "_emit", side_effect=lambda event, *args: events.append(event)
    ):
        server._maybe_fire_tui_heartbeat(sid, session)
    assert events.count("message.start") == 0
