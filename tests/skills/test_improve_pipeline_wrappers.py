"""Tests for operational wrapper failure isolation."""
from __future__ import annotations

import importlib.util
from pathlib import Path


WRAPPER = Path.home() / ".hermes/scripts/upstream_merge_wrapper.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("upstream_merge_wrapper_test", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_notification_timeout_does_not_escape_merge_wrapper(monkeypatch) -> None:
    wrapper = _load_wrapper()

    def timeout(*args, **kwargs):
        raise wrapper.subprocess.TimeoutExpired(cmd="notify", timeout=90)

    monkeypatch.setattr(wrapper.subprocess, "run", timeout)
    wrapper._notify("halted", "merge halted")