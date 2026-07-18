"""Prepare and bind one authoritative cwd snapshot per tool dispatch.

The runtime snapshot is process-bound: a forked child can inherit Python
memory, but it cannot reuse the parent's cwd authorization. Each process must
prepare its own runtime from its local task/session inputs.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.tool_policy import PreparedToolRuntime


@dataclass(slots=True)
class _ProcessBoundRuntime:
    pid: int
    runtime: PreparedToolRuntime
    active: bool = True


_PREPARED_TOOL_RUNTIME: ContextVar[_ProcessBoundRuntime | None] = ContextVar(
    "HERMES_PREPARED_TOOL_RUNTIME",
    default=None,
)


def get_prepared_tool_runtime() -> PreparedToolRuntime | None:
    """Return the current process's bound runtime, never an inherited one."""
    bound = _PREPARED_TOOL_RUNTIME.get()
    if bound is None or bound.pid != os.getpid() or not bound.active:
        return None
    return bound.runtime


@contextmanager
def bind_prepared_tool_runtime(
    runtime: PreparedToolRuntime,
) -> Iterator[PreparedToolRuntime]:
    """Bind *runtime* for the duration of one approved handler invocation."""
    if not isinstance(runtime, PreparedToolRuntime):
        raise TypeError("runtime must be a PreparedToolRuntime")
    bound = _ProcessBoundRuntime(pid=os.getpid(), runtime=runtime)
    token = _PREPARED_TOOL_RUNTIME.set(bound)
    try:
        yield runtime
    finally:
        bound.active = False
        _PREPARED_TOOL_RUNTIME.reset(token)


def _runtime_task_key(task_id: str | None, session_id: str | None) -> str:
    raw_task = str(task_id or "").strip()
    raw_session = str(session_id or "").strip()
    if raw_task and raw_task != "default":
        return raw_task
    return raw_session or raw_task or "default"


def _terminal_backend(task_key: str) -> str:
    try:
        from tools.file_tools import _terminal_env_type_for_task

        backend = _terminal_env_type_for_task(task_key)
    except Exception:
        backend = os.getenv("TERMINAL_ENV", "local")
    return str(backend or "local").strip().lower()


def _terminal_config(backend: str) -> dict[str, object]:
    try:
        from tools.terminal_tool import _get_env_config

        config = _get_env_config()
        return config if type(config) is dict else {}
    except Exception:
        if backend == "local":
            return {}
        if backend == "ssh":
            return {"cwd": "~"}
        return {"cwd": "/root"}


def _live_terminal_cwd(task_key: str) -> str | None:
    try:
        from tools.file_tools import _get_live_tracking_cwd

        value = _get_live_tracking_cwd(task_key)
    except Exception:
        return None
    return str(value).strip() if value else None


def _task_override_cwd(task_key: str, backend: str) -> str | None:
    if backend == "local":
        try:
            from tools.file_tools import _registered_task_cwd_override

            value = _registered_task_cwd_override(task_key)
        except Exception:
            return None
        return str(value).strip() if value else None

    try:
        from tools.terminal_tool import (
            _CONTAINER_BACKENDS,
            _is_unusable_container_cwd,
            resolve_task_overrides,
        )

        overrides = resolve_task_overrides(task_key)
        value = overrides.get("cwd") if type(overrides) is dict else None
        if (
            backend in _CONTAINER_BACKENDS
            and value
            and _is_unusable_container_cwd(str(value))
        ):
            return None
    except Exception:
        return None
    return str(value).strip() if value else None


def _preserved_live_cwd(task_key: str) -> str | None:
    try:
        from tools.file_tools import _last_known_cwd_for

        value = _last_known_cwd_for(task_key)
    except Exception:
        return None
    return str(value).strip() if value else None


def _configured_cwd(backend: str, config: dict[str, object]) -> str | None:
    if backend == "local":
        try:
            from tools.file_tools import _configured_terminal_cwd

            value = _configured_terminal_cwd()
        except Exception:
            return None
    else:
        value = config.get("cwd")
    return str(value).strip() if value else None


def _select_runtime_cwd(
    tool_name: str,
    args: Mapping[str, object],
    task_key: str,
    backend: str,
    config: dict[str, object],
) -> tuple[str, str]:
    if tool_name == "terminal":
        workdir = args.get("workdir")
        if type(workdir) is str and workdir.strip():
            return workdir.strip(), "explicit_workdir"

    live = _live_terminal_cwd(task_key)
    if live:
        return live, "live_terminal"

    override = _task_override_cwd(task_key, backend)
    if override:
        return override, "task_override"

    preserved = _preserved_live_cwd(task_key)
    if preserved:
        return preserved, "live_terminal"

    configured = _configured_cwd(backend, config)
    if configured:
        return configured, "terminal_config"

    return os.getcwd(), "process_cwd"


def _canonical_local_cwd(raw_cwd: str) -> str:
    try:
        from tools.file_tools import _expand_tilde

        expanded = _expand_tilde(raw_cwd)
    except Exception:
        expanded = os.path.expanduser(raw_cwd)
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return str(path.resolve())


def _normalize_remote_cwd(raw_cwd: str) -> str:
    return posixpath.normpath(raw_cwd)


def prepare_tool_runtime(
    tool_name: str,
    args: Mapping[str, object],
    task_id: str | None,
    session_id: str | None,
) -> PreparedToolRuntime:
    """Snapshot the cwd that the named tool handler must execute against."""
    if type(tool_name) is not str or not tool_name.strip():
        raise TypeError("tool_name must be a non-empty string")
    if not isinstance(args, Mapping):
        raise TypeError("args must be a mapping")

    task_key = _runtime_task_key(task_id, session_id)
    backend = _terminal_backend(task_key)
    config = _terminal_config(backend)
    raw_cwd, source = _select_runtime_cwd(
        tool_name.strip(),
        args,
        task_key,
        backend,
        config,
    )

    if backend != "local":
        return PreparedToolRuntime(
            effective_cwd=_normalize_remote_cwd(raw_cwd),
            effective_cwd_source="remote_unmapped",
            effective_cwd_authoritative=False,
        )

    return PreparedToolRuntime(
        effective_cwd=_canonical_local_cwd(raw_cwd),
        effective_cwd_source=source,
        effective_cwd_authoritative=True,
    )


__all__ = [
    "bind_prepared_tool_runtime",
    "get_prepared_tool_runtime",
    "prepare_tool_runtime",
]
