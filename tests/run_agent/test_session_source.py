import pytest

from gateway.session_context import _UNSET, _VAR_MAP, clear_session_vars, set_session_vars
from run_agent import _session_source_for_agent


@pytest.fixture(autouse=True)
def _reset_contextvars():
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def test_session_source_context_overrides_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)

    tokens = set_session_vars(source="tool")
    try:
        assert _session_source_for_agent("tui") == "tool"
    finally:
        clear_session_vars(tokens)


def test_session_source_falls_back_to_platform(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    assert _session_source_for_agent("tui") == "tui"


def test_session_source_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "webhook")

    assert _session_source_for_agent(None) == "webhook"


def test_session_source_marks_cron_child_cli_sessions_as_cron(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    assert _session_source_for_agent("cli") == "cron"


def test_session_source_explicit_source_wins_over_cron_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    assert _session_source_for_agent("cli") == "tool"
