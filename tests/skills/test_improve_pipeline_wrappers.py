"""Tests for operational wrapper failure isolation."""
from __future__ import annotations

import importlib.util
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[2] / "scripts/upstream_merge_wrapper.py"


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


def test_upstream_gate_uses_bounded_relevant_suites() -> None:
    wrapper = _load_wrapper()
    command = wrapper._build_command()
    test_paths = [
        command[i + 1]
        for i, value in enumerate(command[:-1])
        if value == "--test" and command[i + 1] != str(wrapper.PYTHON)
    ]
    assert "tests/skills" in test_paths
    assert "tests/gateway/test_scale_to_zero_watcher.py" in test_paths
    assert "tests/plugins/test_teams_pipeline_plugin.py" in test_paths
    assert "tests/tools/test_memory_tool.py" in test_paths
    assert "tests" not in test_paths
