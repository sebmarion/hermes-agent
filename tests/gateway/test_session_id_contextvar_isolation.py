"""Regression tests: HERMES_SESSION_ID must be concurrency-safe.

Companion to the #24100 fix (commit b0f44d3fa) which removed the
process-global ``os.environ["HERMES_SESSION_KEY"]`` write from the gateway
per-turn path.  The same bug class exists for ``HERMES_SESSION_ID``:

``set_current_session_id`` (gateway/session_context.py) writes BOTH the
``_SESSION_ID`` ContextVar (task-local, safe) AND
``os.environ["HERMES_SESSION_ID"]`` (process-global, NOT concurrency-safe).
``set_session_vars`` writes the ContextVar only (safe).

When compression rotates the session id mid-turn — or two gateway turns
overlap — the ``os.environ`` write clobbers every other concurrent
session's id.  A tool worker thread whose ContextVar is unset (or a
path that still reads ``os.environ`` directly) then resolves to the
wrong session — mixing threads / conversations.

These tests assert the concurrency contract: the ContextVar is the
single source of truth, and the process-global ``os.environ`` write must
not leak a concurrent session's id.
"""

import os
import threading

import pytest

from gateway.session_context import (
    _SESSION_ID,
    _UNSET,
    clear_session_vars,
    get_session_env,
    set_current_session_id,
    set_session_vars,
)


@pytest.fixture(autouse=True)
def _clean_session_id_env():
    """Ensure no HERMES_SESSION_ID leaks between tests."""
    saved = os.environ.pop("HERMES_SESSION_ID", None)
    # Reset ALL session ContextVars to the "never set" sentinel so each test
    # starts from a clean slate. contextvars propagate within asyncio tasks /
    # the test process, so a prior test's set_session_vars() call can bleed
    # into the next test file (e.g. test_kanban_tools) if not reset here.
    from gateway.session_context import _VAR_MAP

    for _var in _VAR_MAP.values():
        _var.set(_UNSET)
    _SESSION_ID.set(_UNSET)
    try:
        yield
    finally:
        os.environ.pop("HERMES_SESSION_ID", None)
        if saved is not None:
            os.environ["HERMES_SESSION_ID"] = saved
        for _var in _VAR_MAP.values():
            _var.set(_UNSET)
        _SESSION_ID.set(_UNSET)


class TestSessionIdContextvarIsolation:
    """The ContextVar (not os.environ) must drive resolution."""

    def test_contextvar_wins_over_clobbered_environ(self):
        """get_session_env("HERMES_SESSION_ID") honors the contextvar,
        not a stale process-global os.environ value written by a
        concurrent session (the #24100 pattern, for SESSION_ID)."""
        # Simulate a concurrent session B having written process-global env.
        os.environ["HERMES_SESSION_ID"] = "session-B"

        tokens = set_session_vars(session_id="session-A")
        try:
            assert get_session_env("HERMES_SESSION_ID") == "session-A"
        finally:
            clear_session_vars(tokens)

    def test_unset_contextvar_does_not_fall_back_to_clobbered_environ(self):
        """After clear_session_vars, the resolver must NOT fall back to a
        concurrent session's clobbered os.environ value."""
        os.environ["HERMES_SESSION_ID"] = "session-B-stale"

        tokens = set_session_vars(session_id="session-A")
        try:
            assert get_session_env("HERMES_SESSION_ID") == "session-A"
        finally:
            clear_session_vars(tokens)

        # After clearing, resolution must NOT return the stale env value.
        assert get_session_env("HERMES_SESSION_ID") != "session-B-stale", (
            "resolver leaked a concurrent session's clobbered os.environ "
            "value — session-id thread mixing regression"
        )

    def test_set_current_session_id_does_not_write_process_global_environ(self):
        """set_current_session_id (used by compression rotation and
        agent_init) must NOT write the process-global os.environ, because
        concurrent gateway turns would clobber each other's session id —
        the exact #24100 bug class.

        The ContextVar is task-local and inherited by tool worker threads,
        so it is sufficient for concurrency safety. Tools already prefer
        the ContextVar via get_session_env() with os.environ as a fallback
        for CLI/cron/test processes that never bind the contextvar.
        """
        # Pre-existing env value from an unrelated CLI/cron process.
        os.environ["HERMES_SESSION_ID"] = "cli-session"

        set_current_session_id("gateway-session-A")

        # The ContextVar must be set…
        assert _SESSION_ID.get() == "gateway-session-A"
        # …but os.environ must NOT be clobbered (process-global, unsafe).
        assert os.environ.get("HERMES_SESSION_ID") == "cli-session", (
            "set_current_session_id wrote the process-global os.environ, "
            "which clobbers concurrent gateway sessions' ids — the #24100 "
            "bug class for HERMES_SESSION_ID"
        )

    def test_two_concurrent_sessions_do_not_clobber_environ(self):
        """Two threads calling set_current_session_id concurrently must
        not race on os.environ[\"HERMES_SESSION_ID\"]. Each thread's
        ContextVar is independent; neither touches os.environ."""
        os.environ["HERMES_SESSION_ID"] = "baseline"

        results = {}

        def worker(key):
            set_current_session_id(key)
            results[key] = get_session_env("HERMES_SESSION_ID")

        ta = threading.Thread(target=worker, args=("session-A",))
        tb = threading.Thread(target=worker, args=("session-B",))
        ta.start()
        tb.start()
        ta.join(timeout=5)
        tb.join(timeout=5)

        assert results.get("session-A") == "session-A"
        assert results.get("session-B") == "session-B"
        # os.environ must be untouched by both gateway-style sessions.
        assert os.environ.get("HERMES_SESSION_ID") == "baseline", (
            "a concurrent set_current_session_id write leaked into "
            "process-global os.environ"
        )

    def test_no_os_environ_writes_in_compression_or_init_paths(self):
        """Grep-level guard: the compaction rotation/rollback fallbacks
        and agent_init must NOT contain ``os.environ["HERMES_SESSION_ID"]``
        writes (the #24100 bug class).  The ACP adapter is exempt (it
        runs as a separate process per session in production)."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]  # repo root
        files_to_check = [
            root / "agent" / "conversation_compression.py",
            root / "agent" / "agent_init.py",
            root / "gateway" / "session_context.py",
        ]
        pattern = 'os.environ["HERMES_SESSION_ID"]'
        offenders = []
        for f in files_to_check:
            if not f.exists():
                continue
            for i, line in enumerate(f.read_text().splitlines(), 1):
                stripped = line.split("#")[0]  # ignore comment-only lines
                if pattern in stripped and "NOT" not in stripped and "deliberately" not in stripped:
                    offenders.append(f"{f.name}:{i}: {line.strip()}")
        assert not offenders, (
            "Found os.environ[\"HERMES_SESSION_ID\"] writes in compaction/"
            "init/gateway paths — these are process-global and clobber "
            "concurrent sessions (#24100 class):\n" + "\n".join(offenders)
        )
