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
