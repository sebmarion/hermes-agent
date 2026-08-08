"""Tests for scoped process mirroring of durable session IDs."""

from gateway.session_context import (
    _SESSION_ID,
    _UNSET,
    get_session_env,
    set_current_session_id,
    suppress_process_session_id_mirroring,
)


def test_legacy_session_rotation_still_updates_process_mirror(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "before")
    token = _SESSION_ID.set(_UNSET)
    try:
        set_current_session_id("after")

        assert get_session_env("HERMES_SESSION_ID") == "after"
        assert __import__("os").environ["HERMES_SESSION_ID"] == "after"
    finally:
        _SESSION_ID.reset(token)


def test_scoped_suppression_keeps_process_mirror_unchanged(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "process-baseline")
    token = _SESSION_ID.set(_UNSET)
    try:
        with suppress_process_session_id_mirroring():
            set_current_session_id("webui-rotated")

            assert get_session_env("HERMES_SESSION_ID") == "webui-rotated"
            assert __import__("os").environ["HERMES_SESSION_ID"] == "process-baseline"

        set_current_session_id("legacy-rotated")
        assert __import__("os").environ["HERMES_SESSION_ID"] == "legacy-rotated"
    finally:
        _SESSION_ID.reset(token)
