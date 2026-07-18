"""Authoritative per-tool runtime cwd preparation and binding tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime_cwd import resolve_agent_cwd
from agent.tool_runtime_context import (
    bind_prepared_tool_runtime,
    get_prepared_tool_runtime,
    prepare_tool_runtime,
)
from hermes_cli.tool_policy import PreparedToolRuntime
from tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_runtime_registries(monkeypatch):
    from tools import file_tools, terminal_tool

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    terminal_tool._active_environments.clear()
    terminal_tool._task_env_overrides.clear()
    file_tools._file_ops_cache.clear()
    file_tools._last_known_cwd.clear()
    yield
    terminal_tool._active_environments.clear()
    terminal_tool._task_env_overrides.clear()
    file_tools._file_ops_cache.clear()
    file_tools._last_known_cwd.clear()


def test_local_cli_uses_canonical_process_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    runtime = prepare_tool_runtime("read_file", {"path": "notes.md"}, "", "")

    assert runtime == PreparedToolRuntime(
        effective_cwd=str(tmp_path.resolve()),
        effective_cwd_source="process_cwd",
        effective_cwd_authoritative=True,
    )


def test_explicit_terminal_workdir_wins(monkeypatch, tmp_path):
    launch = tmp_path / "launch"
    workdir = tmp_path / "explicit"
    launch.mkdir()
    workdir.mkdir()
    monkeypatch.chdir(launch)

    runtime = prepare_tool_runtime(
        "terminal",
        {"command": "pwd", "workdir": str(workdir)},
        "task-explicit",
        "session-explicit",
    )

    assert runtime.effective_cwd == str(workdir.resolve())
    assert runtime.effective_cwd_source == "explicit_workdir"
    assert runtime.effective_cwd_authoritative is True


def test_live_persistent_terminal_cwd_wins(monkeypatch, tmp_path):
    from tools import terminal_tool

    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.chdir(tmp_path)
    terminal_tool._active_environments["default"] = SimpleNamespace(
        cwd=str(live),
        cwd_owner="session-live",
    )

    runtime = prepare_tool_runtime(
        "write_file",
        {"path": "result.txt", "content": "ok"},
        "session-live",
        "session-live",
    )

    assert runtime.effective_cwd == str(live.resolve())
    assert runtime.effective_cwd_source == "live_terminal"
    assert runtime.effective_cwd_authoritative is True


def test_gateway_task_override_precedes_terminal_config(monkeypatch, tmp_path):
    from tools.terminal_tool import register_task_env_overrides

    configured = tmp_path / "configured"
    workspace = tmp_path / "gateway-workspace"
    configured.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(configured))
    register_task_env_overrides("gateway-session", {"cwd": str(workspace)})

    runtime = prepare_tool_runtime(
        "read_file",
        {"path": "README.md"},
        "default",
        "gateway-session",
    )

    assert runtime.effective_cwd == str(workspace.resolve())
    assert runtime.effective_cwd_source == "task_override"
    assert runtime.effective_cwd_authoritative is True


def test_terminal_config_precedes_process_cwd(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    launch = tmp_path / "launch"
    configured.mkdir()
    launch.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(configured))
    monkeypatch.chdir(launch)

    runtime = prepare_tool_runtime("search_files", {"path": "."}, "", "")

    assert runtime.effective_cwd == str(configured.resolve())
    assert runtime.effective_cwd_source == "terminal_config"
    assert runtime.effective_cwd_authoritative is True


@pytest.mark.parametrize("backend", ["ssh", "docker"])
def test_remote_paths_are_unmapped_and_never_host_resolved(monkeypatch, backend):
    from tools.file_tools import _resolve_base_dir, _resolve_path_for_task

    monkeypatch.setenv("TERMINAL_ENV", backend)

    def reject_host_resolve(*_args, **_kwargs):
        raise AssertionError("remote/container cwd must not call Path.resolve()")

    monkeypatch.setattr(Path, "resolve", reject_host_resolve)
    runtime = prepare_tool_runtime(
        "terminal",
        {"command": "pwd", "workdir": "/remote/project/../repo"},
        "remote-task",
        "remote-session",
    )
    with bind_prepared_tool_runtime(runtime):
        base = _resolve_base_dir("remote-task")
        target = _resolve_path_for_task("notes.txt", "remote-task")
        agent_cwd = resolve_agent_cwd()

    assert runtime.effective_cwd == "/remote/repo"
    assert runtime.effective_cwd_source == "remote_unmapped"
    assert runtime.effective_cwd_authoritative is False
    assert str(base) == "/remote/repo"
    assert str(target) == "/remote/repo/notes.txt"
    assert str(agent_cwd) == "/remote/repo"


def test_absolute_file_target_is_preserved_as_an_argument(monkeypatch, tmp_path):
    from tools.file_tools import _resolve_path_for_task

    launch = tmp_path / "launch"
    elsewhere = tmp_path / "elsewhere"
    launch.mkdir()
    elsewhere.mkdir()
    target = elsewhere / "absolute.txt"
    args = {"path": str(target)}
    original = dict(args)
    monkeypatch.chdir(launch)

    runtime = prepare_tool_runtime("read_file", args, "task-absolute", "")
    with bind_prepared_tool_runtime(runtime):
        resolved = _resolve_path_for_task(args["path"], "task-absolute")

    assert args == original
    assert resolved == target.resolve()
    assert runtime.effective_cwd == str(launch.resolve())


def test_preserved_absolute_target_keeps_sensitive_symlink_guard(tmp_path):
    from tools.file_tools import _check_sensitive_path

    alias = tmp_path / "system-alias"
    alias.symlink_to("/etc", target_is_directory=True)

    assert _check_sensitive_path(str(alias / "hosts")) is not None


def test_prepared_runtime_freezes_policy_and_handler_cwd(monkeypatch, tmp_path):
    from tools import terminal_tool
    from tools.file_tools import _resolve_base_dir
    from tools.terminal_tool import _resolve_command_cwd

    approved = tmp_path / "approved"
    raced = tmp_path / "raced"
    approved.mkdir()
    raced.mkdir()
    env = SimpleNamespace(cwd=str(approved), cwd_owner="session-race")
    terminal_tool._active_environments["default"] = env
    runtime = prepare_tool_runtime(
        "terminal",
        {"command": "pwd"},
        "session-race",
        "session-race",
    )
    policy_cwd = runtime.effective_cwd
    env.cwd = str(raced)

    registry = ToolRegistry()

    def handler(_args, **kwargs):
        bound = get_prepared_tool_runtime()
        return json.dumps(
            {
                "bound": bound.effective_cwd if bound else None,
                "agent": str(resolve_agent_cwd()),
                "file": str(_resolve_base_dir(kwargs["task_id"])),
                "terminal": _resolve_command_cwd(
                    workdir=None,
                    env=env,
                    default_cwd=str(raced),
                ),
            }
        )

    registry.register(
        name="probe",
        toolset="test",
        schema={"name": "probe", "parameters": {"type": "object"}},
        handler=handler,
    )
    observed = json.loads(
        registry.dispatch(
            "probe",
            {},
            task_id="session-race",
            session_id="session-race",
            prepared_runtime=runtime,
        )
    )

    assert policy_cwd == str(approved.resolve())
    assert observed == {
        "bound": policy_cwd,
        "agent": policy_cwd,
        "file": policy_cwd,
        "terminal": policy_cwd,
    }
    assert get_prepared_tool_runtime() is None


def test_background_handler_receives_frozen_cwd(monkeypatch, tmp_path):
    from tools import terminal_tool
    from tools.terminal_tool import _resolve_command_cwd

    approved = tmp_path / "approved-bg"
    raced = tmp_path / "raced-bg"
    approved.mkdir()
    raced.mkdir()
    env = SimpleNamespace(cwd=str(approved), cwd_owner="session-bg")
    terminal_tool._active_environments["default"] = env
    runtime = prepare_tool_runtime(
        "terminal",
        {"command": "server", "background": True},
        "session-bg",
        "session-bg",
    )
    env.cwd = str(raced)
    registry = ToolRegistry()

    def background_handler(args, **_kwargs):
        assert args["background"] is True
        spawned_cwd = _resolve_command_cwd(
            workdir=None,
            env=env,
            default_cwd=str(raced),
        )
        return json.dumps({"spawned_cwd": spawned_cwd})

    registry.register(
        name="background-probe",
        toolset="test",
        schema={"name": "background-probe", "parameters": {"type": "object"}},
        handler=background_handler,
    )
    result = json.loads(
        registry.dispatch(
            "background-probe",
            {"background": True},
            task_id="session-bg",
            session_id="session-bg",
            prepared_runtime=runtime,
        )
    )

    assert result == {"spawned_cwd": str(approved.resolve())}
    assert get_prepared_tool_runtime() is None


def test_two_concurrent_sessions_keep_runtime_cwds_isolated(tmp_path):
    from tools.terminal_tool import register_task_env_overrides

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    register_task_env_overrides("session-a", {"cwd": str(workspace_a)})
    register_task_env_overrides("session-b", {"cwd": str(workspace_b)})
    barrier = threading.Barrier(2)
    registry = ToolRegistry()

    def handler(_args, **_kwargs):
        barrier.wait(timeout=2)
        runtime = get_prepared_tool_runtime()
        return json.dumps({"cwd": runtime.effective_cwd if runtime else None})

    registry.register(
        name="concurrent-probe",
        toolset="test",
        schema={"name": "concurrent-probe", "parameters": {"type": "object"}},
        handler=handler,
    )

    def dispatch(session_id):
        return json.loads(
            registry.dispatch(
                "concurrent-probe",
                {},
                task_id="default",
                session_id=session_id,
            )
        )["cwd"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(dispatch, "session-a")
        future_b = executor.submit(dispatch, "session-b")
        observed = {future_a.result(timeout=3), future_b.result(timeout=3)}

    assert observed == {str(workspace_a.resolve()), str(workspace_b.resolve())}
    assert get_prepared_tool_runtime() is None


def test_async_registry_handler_sees_bound_runtime(tmp_path):
    from tools.terminal_tool import register_task_env_overrides

    workspace = tmp_path / "async-workspace"
    workspace.mkdir()
    register_task_env_overrides("async-session", {"cwd": str(workspace)})
    registry = ToolRegistry()

    async def handler(_args, **_kwargs):
        await asyncio.sleep(0)
        runtime = get_prepared_tool_runtime()
        return json.dumps({"cwd": runtime.effective_cwd if runtime else None})

    registry.register(
        name="async-probe",
        toolset="test",
        schema={"name": "async-probe", "parameters": {"type": "object"}},
        handler=handler,
        is_async=True,
    )
    result = json.loads(
        registry.dispatch(
            "async-probe",
            {},
            task_id="default",
            session_id="async-session",
        )
    )

    assert result == {"cwd": str(workspace.resolve())}
    assert get_prepared_tool_runtime() is None


def test_registry_resets_runtime_after_handler_exception(tmp_path):
    registry = ToolRegistry()

    def handler(_args, **_kwargs):
        assert get_prepared_tool_runtime() is not None
        raise RuntimeError("handler failed")

    registry.register(
        name="raising-probe",
        toolset="test",
        schema={"name": "raising-probe", "parameters": {"type": "object"}},
        handler=handler,
    )
    result = json.loads(
        registry.dispatch(
            "raising-probe",
            {},
            task_id="default",
            session_id="raising-session",
        )
    )

    assert "handler failed" in result["error"]
    assert get_prepared_tool_runtime() is None


def test_context_copied_inside_handler_expires_after_dispatch():
    registry = ToolRegistry()
    captured: dict[str, contextvars.Context] = {}

    def handler(_args, **_kwargs):
        assert get_prepared_tool_runtime() is not None
        captured["context"] = contextvars.copy_context()
        return "{}"

    registry.register(
        name="copy-probe",
        toolset="test",
        schema={"name": "copy-probe", "parameters": {"type": "object"}},
        handler=handler,
    )
    registry.dispatch("copy-probe", {}, task_id="default", session_id="copy")

    assert captured["context"].run(get_prepared_tool_runtime) is None
    assert get_prepared_tool_runtime() is None


def test_shared_terminal_live_cwd_is_used_only_by_its_owner(tmp_path):
    from tools import terminal_tool
    from tools.terminal_tool import register_task_env_overrides

    live_a = tmp_path / "live-a"
    workspace_b = tmp_path / "workspace-b"
    live_a.mkdir()
    workspace_b.mkdir()
    register_task_env_overrides("session-a", {"cwd": str(live_a)})
    register_task_env_overrides("session-b", {"cwd": str(workspace_b)})
    terminal_tool._active_environments["default"] = SimpleNamespace(
        cwd=str(live_a),
        cwd_owner="session-a",
    )

    runtime_a = prepare_tool_runtime("read_file", {}, "default", "session-a")
    runtime_b = prepare_tool_runtime("read_file", {}, "default", "session-b")

    assert runtime_a.effective_cwd == str(live_a.resolve())
    assert runtime_a.effective_cwd_source == "live_terminal"
    assert runtime_b.effective_cwd == str(workspace_b.resolve())
    assert runtime_b.effective_cwd_source == "task_override"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_forked_child_rejects_parent_process_binding(tmp_path):
    parent_runtime = PreparedToolRuntime(
        effective_cwd=str(tmp_path),
        effective_cwd_source="process_cwd",
        effective_cwd_authoritative=True,
    )
    read_fd, write_fd = os.pipe()
    with bind_prepared_tool_runtime(parent_runtime):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - assertion is read by the parent
            try:
                os.close(read_fd)
                inherited = get_prepared_tool_runtime()
                os.write(write_fd, b"none" if inherited is None else b"inherited")
            finally:
                os._exit(0)

        os.close(write_fd)
        observed = os.read(read_fd, 32)
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == b"none"
    assert get_prepared_tool_runtime() is None
