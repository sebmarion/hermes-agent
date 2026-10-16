"""Regression tests for the root-owned backend autoreload hook."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/hermes-backend-autoreload.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_backend_autoreload", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reads_seb_owned_checkout_as_seb(monkeypatch, tmp_path, capsys) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="b671567a795b\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "STATE", tmp_path / "backend-promoted.sha")

    assert module.main(["--dry-run"]) == 0
    assert calls[0] == [
        "/usr/sbin/runuser",
        "-u",
        "seb",
        "--",
        "/usr/bin/git",
        "-C",
        module.REPO,
        "rev-parse",
        "HEAD",
    ]
    assert "WOULD restart" in capsys.readouterr().out


def test_defer_if_connected_never_restarts(monkeypatch, tmp_path, capsys) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="new-head\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "STATE", tmp_path / "backend-promoted.sha")
    monkeypatch.setattr(module, "_active_client_counts", lambda: (1, 1), raising=False)

    assert module.main(["--defer-if-connected"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "deferred": True,
        "reason": "backend has an active Desktop/TUI session; disconnect clients before promotion",
    }
    assert calls == [
        [
            "/usr/sbin/runuser",
            "-u",
            "seb",
            "--",
            "/usr/bin/git",
            "-C",
            module.REPO,
            "rev-parse",
            "HEAD",
        ]
    ]
    assert not (tmp_path / "backend-promoted.sha").exists()
