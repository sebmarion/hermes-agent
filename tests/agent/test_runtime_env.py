"""Tests for task-local non-secret runtime configuration."""

import threading

from agent import runtime_env


def test_unscoped_runtime_env_reads_process_environment(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")

    assert runtime_env.get_runtime_env("TERMINAL_ENV") == "local"


def test_authoritative_scope_masks_process_environment(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "process.invalid")

    token = runtime_env.set_runtime_env(
        {"TERMINAL_ENV": "ssh"},
        authoritative=True,
    )
    try:
        assert runtime_env.get_runtime_env("TERMINAL_ENV") == "ssh"
        assert runtime_env.get_runtime_env("TERMINAL_SSH_HOST") is None
        assert runtime_env.get_runtime_env("TERMINAL_SSH_HOST", "fallback") == "fallback"
    finally:
        runtime_env.reset_runtime_env(token)

    assert runtime_env.get_runtime_env("TERMINAL_SSH_HOST") == "process.invalid"


def test_runtime_env_scopes_are_thread_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "process")
    barrier = threading.Barrier(2)
    observed = {}

    def _worker(label: str) -> None:
        token = runtime_env.set_runtime_env(
            {"TERMINAL_ENV": label},
            authoritative=True,
        )
        try:
            barrier.wait()
            observed[label] = runtime_env.get_runtime_env("TERMINAL_ENV")
        finally:
            runtime_env.reset_runtime_env(token)

    threads = [threading.Thread(target=_worker, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert observed == {"a": "a", "b": "b"}
    assert runtime_env.get_runtime_env("TERMINAL_ENV") == "process"
