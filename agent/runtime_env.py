"""Task-local non-secret runtime configuration.

Long-lived multi-profile hosts cannot safely publish per-turn terminal and
tool settings through ``os.environ`` because that mapping is process-global.
This module provides a small ContextVar-backed overlay for those settings.
Unscoped callers retain the legacy process-environment behavior.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class _RuntimeEnvScope:
    values: Mapping[str, str]
    authoritative: bool


_RUNTIME_ENV: ContextVar[Optional[_RuntimeEnvScope]] = ContextVar(
    "_RUNTIME_ENV",
    default=None,
)


def set_runtime_env(
    values: Optional[Mapping[str, str]],
    *,
    authoritative: bool = False,
) -> Token:
    """Install runtime settings for the current context and return a token.

    An authoritative scope masks process-environment values that are absent
    from ``values``. A non-authoritative scope behaves as an overlay and falls
    back to ``os.environ`` for missing names.
    """
    return _RUNTIME_ENV.set(
        _RuntimeEnvScope(dict(values or {}), bool(authoritative))
    )


def reset_runtime_env(token: Token) -> None:
    """Restore the runtime scope that preceded ``token``."""
    _RUNTIME_ENV.reset(token)


def runtime_env_scope_active() -> bool:
    """Return whether the current context has an installed runtime scope."""
    return _RUNTIME_ENV.get() is not None


def get_runtime_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a runtime setting from the current scope or process env."""
    scope = _RUNTIME_ENV.get()
    if scope is not None:
        if name in scope.values:
            return scope.values[name]
        if scope.authoritative:
            return default
    return os.environ.get(name, default)
