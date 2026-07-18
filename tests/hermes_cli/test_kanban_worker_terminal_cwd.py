"""Tests: kanban worker spawn pins TERMINAL_CWD to the task workspace.

Regression coverage for #34619 and #41312 (same root cause): ``_default_spawn``
launched the worker subprocess with ``cwd=workspace`` and set
``HERMES_KANBAN_WORKSPACE``, but did NOT set ``TERMINAL_CWD``. Because
``TERMINAL_CWD`` takes precedence over the process cwd in both
``tools/file_tools.py::_resolve_base_dir`` (relative ``write_file`` paths) and
``agent_init``'s context-file loader (``AGENTS.md`` discovery), workers inherited
the dispatching gateway's cwd — relative writes landed in the gateway user's
home (#41312) and the wrong profile's ``AGENTS.md`` was loaded (#34619).
Pinning ``TERMINAL_CWD`` to the workspace fixes both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_REAL_POPEN = subprocess.Popen


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_cwd",
        title="cwd pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _capture_spawn_env(kb, monkeypatch, workspace: str) -> dict:
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(_make_task(kb), workspace)
    return captured


def test_terminal_cwd_pinned_to_workspace(monkeypatch, tmp_path):
    """A real, absolute workspace dir is pinned as TERMINAL_CWD."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))

    assert captured["env"]["TERMINAL_CWD"] == str(workspace)
    # The subprocess cwd and TERMINAL_CWD must agree — both anchor the workspace.
    assert captured["cwd"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace)


def test_terminal_cwd_not_pinned_for_nonexistent_workspace(monkeypatch, tmp_path):
    """A non-directory workspace must NOT clobber the inherited TERMINAL_CWD.

    file_tools rejects relative / sentinel TERMINAL_CWD values, so writing a
    meaningless (nonexistent) path would be worse than leaving the inherited
    one. The guard requires an existing absolute dir.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TERMINAL_CWD", "/pre/existing/anchor")

    from hermes_cli import kanban_db as kb

    missing = tmp_path / "does-not-exist"

    captured = _capture_spawn_env(kb, monkeypatch, str(missing))

    # Inherited value is preserved (not overwritten with a bogus path).
    assert captured["env"]["TERMINAL_CWD"] == "/pre/existing/anchor"


def test_worker_process_prepares_its_own_workspace_runtime(monkeypatch, tmp_path):
    """The spawned worker derives cwd locally; no parent ContextVar is reused."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text(
        "toolsets:\n  - kanban\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "worker-workspace"
    workspace.mkdir()
    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))
    child_env = dict(captured["env"])
    child_env["TERMINAL_ENV"] = "local"
    repo_root = Path(__file__).resolve().parents[2]
    inherited_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), inherited_pythonpath) if part
    )
    probe = (
        "import json; "
        "from agent.tool_runtime_context import "
        "get_prepared_tool_runtime, prepare_tool_runtime; "
        "before = get_prepared_tool_runtime(); "
        "runtime = prepare_tool_runtime('read_file', {}, 't_cwd', 'worker'); "
        "print(json.dumps({'before': before is None, "
        "'cwd': runtime.effective_cwd, "
        "'source': runtime.effective_cwd_source, "
        "'authoritative': runtime.effective_cwd_authoritative}))"
    )
    proc = _REAL_POPEN(
        [sys.executable, "-c", probe],
        cwd=captured["cwd"],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(timeout=10)

    assert proc.returncode == 0, stderr
    observed = json.loads(stdout.strip().splitlines()[-1])
    assert observed == {
        "before": True,
        "cwd": str(workspace.resolve()),
        "source": "terminal_config",
        "authoritative": True,
    }
