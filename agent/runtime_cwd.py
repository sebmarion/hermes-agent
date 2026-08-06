"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory
(design #19214/#19242: `terminal.cwd` is bridged once to `TERMINAL_CWD` at
gateway/cron startup). The local-CLI backend deliberately leaves it unset and
relies on the launch dir. Reading it in one place keeps the system prompt, the
tool surfaces, and context-file discovery agreeing on where the agent lives.

Multi-session gateways can pin a logical cwd via the `_SESSION_CWD`
contextvar; CLI/cron fall through to `TERMINAL_CWD`/launch cwd.
"""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator, NamedTuple

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)


class _SessionCwdBinding(NamedTuple):
    path: str
    non_host_namespace: bool

# The Python package/source root (this file lives at <root>/agent/runtime_cwd.py).
# When a backend is launched from, or self-spawns into, this tree (the desktop
# app default), an os.getcwd() fallback would inject this repo's contributor
# AGENTS.md as authoritative project context. Context discovery must never
# resolve here.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_install_tree(p: Path) -> bool:
    # True only when p IS the package root or sits inside it. Ancestors of the
    # package root (a user home that happens to contain the checkout, a --user
    # site-packages parent) are legitimate workspaces and must not be blocked.
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(
    cwd: str | None,
    *,
    non_host_namespace: bool = False,
) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set(
        _SessionCwdBinding(
            path=(cwd or "").strip(),
            non_host_namespace=bool(non_host_namespace),
        )
    )


def clear_session_cwd() -> None:
    _SESSION_CWD.set(_SessionCwdBinding("", False))


def _session_cwd_binding() -> _SessionCwdBinding:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return _SessionCwdBinding("", False)
    if isinstance(value, _SessionCwdBinding):
        return value
    # Compatibility for a context captured before this module learned the
    # namespace bit. Such values were necessarily host-path bindings.
    return _SessionCwdBinding(str(value).strip(), False)


def _session_cwd_override() -> str:
    return _session_cwd_binding().path


def get_session_cwd_override() -> str | None:
    """Return only the request-local cwd override, without global fallback.

    Callers that must distinguish request identity from process-global
    ``TERMINAL_CWD`` use this accessor instead of ``resolve_context_cwd()``,
    which intentionally includes the environment fallback for prompt context
    discovery. An unset or explicitly cleared ContextVar is absent.
    """
    value = _session_cwd_override()
    return value or None


def session_cwd_uses_non_host_namespace() -> bool:
    """Whether the bounded request cwd belongs to a remote filesystem."""
    binding = _session_cwd_binding()
    return bool(binding.path and binding.non_host_namespace)


@contextmanager
def bind_session_cwd(
    cwd: str | None,
    *,
    non_host_namespace: bool = False,
) -> Iterator[None]:
    """Bind one logical cwd for a bounded child/request execution scope."""
    token = set_session_cwd(
        cwd,
        non_host_namespace=non_host_namespace,
    )
    try:
        yield
    finally:
        _SESSION_CWD.reset(token)


def _non_host_bound_cwd() -> Path | None:
    binding = _session_cwd_binding()
    if not binding.path or not binding.non_host_namespace:
        return None
    raw = binding.path
    if "\x00" in raw or raw in {".", "./"}:
        raise RuntimeError(f"invalid non-host working directory binding: {raw!r}")
    # SSH owns tilde expansion. Every container-style namespace is absolute;
    # both forms were validated by the terminal resolver before binding.
    if raw == "~" or raw.startswith("~/"):
        return Path(raw)
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"invalid non-host working directory binding: {raw!r}")
    return path


def resolve_agent_cwd() -> Path:
    try:
        from agent.tool_runtime_context import get_prepared_tool_runtime

        prepared = get_prepared_tool_runtime()
    except Exception:
        prepared = None
    if prepared is not None:
        return Path(prepared.effective_cwd)
    non_host_cwd = _non_host_bound_cwd()
    if non_host_cwd is not None:
        return non_host_cwd
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
        logger.warning("configured working directory does not exist: %s", override)
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        logger.warning("TERMINAL_CWD does not exist: %s", raw)
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    try:
        from agent.tool_runtime_context import get_prepared_tool_runtime

        prepared = get_prepared_tool_runtime()
    except Exception:
        prepared = None
    if prepared is not None:
        return Path(prepared.effective_cwd)
    # None means "no configured cwd": build_context_files_prompt then falls back
    # to the launch dir (os.getcwd()), correct for a local CLI launched inside a
    # real project. A configured path is validated here (previously it was passed
    # through unchecked, diverging from resolve_agent_cwd). An explicitly
    # configured path is otherwise honored verbatim — including the Hermes
    # source tree itself, which is a legitimate workspace when the user is
    # developing Hermes (per-surface policy for fallback-picked directories
    # lives in build_context_files_prompt; see #64590).
    non_host_cwd = _non_host_bound_cwd()
    if non_host_cwd is not None:
        return non_host_cwd
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            logger.warning("configured working directory does not exist: %s", override)
        else:
            return p
        return None
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_dir():
            logger.warning("TERMINAL_CWD does not exist: %s", raw)
        else:
            return p
    return None
