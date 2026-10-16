"""Tests for stdio MCP helper children in the spawn ledger (#61514).

Covers ``register_child`` (the ledger mirror of ``register_self`` for
subprocesses that never import Hermes code), the live-spawner protection
contract, dead-spawner reap eligibility through BOTH consumers (the updater's
``_ledger_reapable_backend_pids`` rung and the startup
``reap_orphaned_mcp_helpers`` sweep), and prune-on-write of exited children.

Uses REAL subprocesses (``sleep``) and the real psutil so the
``(pid, create_time)`` identity pair is exercised end-to-end, with the ledger
redirected to a tmp path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from hermes_cli import process_identity as pi

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="uses POSIX sleep children"
)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / pi.LEDGER_FILENAME
    with patch.object(pi, "_ledger_path", return_value=path):
        yield path


@pytest.fixture
def child():
    """A real live child process of THIS test process."""
    proc = subprocess.Popen(["sleep", "300"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _dead_process_identity() -> tuple[int, float]:
    """(pid, create_time) of a real process that is provably dead."""
    proc = subprocess.Popen(["sleep", "300"])
    create = psutil.Process(proc.pid).create_time()
    proc.kill()
    proc.wait()
    # Ensure the corpse is fully reaped (no zombie ambiguity).
    deadline = time.time() + 5
    while time.time() < deadline and psutil.pid_exists(proc.pid):
        time.sleep(0.05)
    return proc.pid, create


# ---------------------------------------------------------------------------
# register_child — entry shape
# ---------------------------------------------------------------------------

def test_register_child_entry_shape(ledger, child):
    assert pi.register_child(child.pid, "mcp-helper") is True
    entries = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(entries) == 1
    e = entries[0]
    assert e["pid"] == child.pid
    assert e["purpose"] == "mcp-helper"
    assert e["install"] == pi.install_id()
    assert e["spawner_pid"] == os.getpid()
    assert e["spawner_create"] == pytest.approx(
        psutil.Process(os.getpid()).create_time(), abs=2.0
    )
    assert e["create_time"] == pytest.approx(
        psutil.Process(child.pid).create_time(), abs=2.0
    )
    assert e["registered_at"] == pytest.approx(time.time(), abs=30.0)
    # Visible through the live-verified reader too.
    live = pi.ledger_entries()
    assert [x["pid"] for x in live] == [child.pid]


def test_register_child_rejects_dead_or_invalid_pids(ledger):
    dead_pid, _ = _dead_process_identity()
    assert pi.register_child(dead_pid, "mcp-helper") is False
    assert pi.register_child(0, "mcp-helper") is False
    assert pi.register_child(-5, "mcp-helper") is False
    assert pi.register_child(True, "mcp-helper") is False
    assert pi.register_child("junk", "mcp-helper") is False  # type: ignore[arg-type]
    assert not ledger.exists() or json.loads(ledger.read_text()) == []


def test_register_child_rejects_non_finite_creation_time(ledger, monkeypatch):
    fake_process = lambda _pid: type(
        "FakeProcess",
        (),
        {
            "create_time": lambda self: float("nan"),
            "cmdline": lambda self: ["helper"],
        },
    )()
    monkeypatch.setattr(psutil, "Process", fake_process)

    assert pi.register_child(123, "mcp-helper") is False
    assert not ledger.exists()


@pytest.mark.parametrize("use_callback", [True, False], ids=["callback", "default"])
@pytest.mark.parametrize(
    "stored_create",
    [
        pytest.param("missing", id="missing"),
        pytest.param(None, id="null"),
        pytest.param("junk", id="malformed-string"),
        pytest.param(False, id="bool"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_reap_rejects_invalid_child_identity(
    ledger, use_callback, stored_create
):
    child_pid = 61001
    spawner_pid = 61002
    events = []

    class NoSuchProcess(Exception):
        pass

    class TimeoutExpired(Exception):
        pass

    class Child:
        def create_time(self):
            return 200.0

        def terminate(self):
            events.append(("terminate", child_pid))

        def wait(self, timeout):
            return None

        def kill(self):
            events.append(("kill", child_pid))

    def process(pid):
        if pid == spawner_pid:
            raise NoSuchProcess(pid)
        assert pid == child_pid
        return Child()

    fake_psutil = SimpleNamespace(
        Process=process,
        NoSuchProcess=NoSuchProcess,
        TimeoutExpired=TimeoutExpired,
    )
    entry = {
        "pid": child_pid,
        "purpose": "mcp-helper",
        "install": pi.install_id(),
        "spawner_pid": spawner_pid,
        "spawner_create": 100.0,
    }
    if stored_create != "missing":
        entry["create_time"] = stored_create
    ledger.write_text(json.dumps([entry]), encoding="utf-8")
    callbacks = []

    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        kwargs = {"kill_fn": callbacks.append} if use_callback else {}
        reaped = pi.reap_orphaned_mcp_helpers(**kwargs)

    assert reaped == []
    assert callbacks == []
    assert events == []


def test_reap_rechecks_child_identity_before_callback(monkeypatch):
    child_pid = 61001
    entry = {
        "pid": child_pid,
        "purpose": "mcp-helper",
        "install": pi.install_id(),
        "create_time": 200.0,
        "spawner_pid": 61002,
        "spawner_create": 100.0,
    }
    callbacks = []
    events = []

    class Child:
        def create_time(self):
            events.append(("create_time", child_pid))
            return 202.0

        def terminate(self):
            events.append(("terminate", child_pid))

    fake_psutil = SimpleNamespace(Process=lambda _pid: Child())
    monkeypatch.setattr(pi, "ledger_entries", lambda **_kwargs: [entry])
    monkeypatch.setattr(pi, "spawner_is_dead", lambda _entry: True)

    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        assert pi.reap_orphaned_mcp_helpers(kill_fn=callbacks.append) == []

    assert callbacks == []
    assert events == [("create_time", child_pid)]


def test_reap_destructive_boundary_rejects_boolean_pid(monkeypatch):
    callbacks = []
    fake_psutil = SimpleNamespace(
        Process=lambda _pid: SimpleNamespace(create_time=lambda: 200.0),
        NoSuchProcess=RuntimeError,
    )
    monkeypatch.setattr(
        pi,
        "ledger_entries",
        lambda **_kwargs: [
            {
                "pid": True,
                "create_time": 200.0,
                "purpose": "mcp-helper",
                "spawner_pid": 61002,
                "spawner_create": 100.0,
            }
        ],
    )
    monkeypatch.setattr(pi, "spawner_is_dead", lambda _entry: True)

    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        reaped = pi.reap_orphaned_mcp_helpers(kill_fn=callbacks.append)

    assert reaped == []
    assert callbacks == []


def test_mcp_helper_is_reapable_purpose():
    assert "mcp-helper" in pi.REAPABLE_PURPOSES
    # Interactive purposes still excluded.
    assert "chat" not in pi.REAPABLE_PURPOSES


# ---------------------------------------------------------------------------
# Spawner liveness gate
# ---------------------------------------------------------------------------

def test_live_spawner_protection(ledger, child):
    """A helper whose spawner (this process) lives is never reap-eligible."""
    pi.register_child(child.pid, "mcp-helper")
    (entry,) = pi.ledger_entries()
    assert pi.spawner_is_dead(entry) is False

    killed: list[int] = []
    reaped = pi.reap_orphaned_mcp_helpers(kill_fn=killed.append)
    assert reaped == [] and killed == []
    assert psutil.pid_exists(child.pid)


def _orphan_entry_for(child_pid: int) -> None:
    """Rewrite the child's ledger entry so its spawner is a real dead process."""
    path = pi._ledger_path()
    dead_pid, dead_create = _dead_process_identity()
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        if e["pid"] == child_pid:
            e["spawner_pid"] = dead_pid
            e["spawner_create"] = dead_create
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_dead_spawner_reap_eligibility(ledger, child):
    pi.register_child(child.pid, "mcp-helper")
    _orphan_entry_for(child.pid)
    (entry,) = pi.ledger_entries()
    assert pi.spawner_is_dead(entry) is True

    killed: list[int] = []
    assert pi.reap_orphaned_mcp_helpers(kill_fn=killed.append) == [child.pid]
    assert killed == [child.pid]


def test_reap_selection_mixes_live_and_dead_spawners(ledger):
    """Only the orphan is selected; the live-spawner helper is untouched."""
    live_proc = subprocess.Popen(["sleep", "300"])
    orphan_proc = subprocess.Popen(["sleep", "300"])
    try:
        pi.register_child(live_proc.pid, "mcp-helper")
        pi.register_child(orphan_proc.pid, "mcp-helper")
        _orphan_entry_for(orphan_proc.pid)

        killed: list[int] = []
        reaped = pi.reap_orphaned_mcp_helpers(kill_fn=killed.append)
        assert reaped == [orphan_proc.pid]
        assert live_proc.pid not in killed
    finally:
        for p in (live_proc, orphan_proc):
            p.kill()
            p.wait()


def test_reap_actually_kills_orphan(ledger, child):
    """Default kill path really terminates the orphaned helper."""
    pi.register_child(child.pid, "mcp-helper")
    _orphan_entry_for(child.pid)
    assert pi.reap_orphaned_mcp_helpers() == [child.pid]
    deadline = time.time() + 5
    while time.time() < deadline and child.poll() is None:
        time.sleep(0.05)
    assert child.poll() is not None


def test_reap_ignores_non_mcp_purposes(ledger, child):
    pi.register_child(child.pid, "serve-child-not-helper")
    _orphan_entry_for(child.pid)
    assert pi.reap_orphaned_mcp_helpers() == []
    assert psutil.pid_exists(child.pid)


# ---------------------------------------------------------------------------
# Updater rung (_ledger_reapable_backend_pids) flow-through
# ---------------------------------------------------------------------------

def test_updater_ledger_rung_flows_mcp_helper(ledger, child):
    from hermes_cli import update_cmd

    pi.register_child(child.pid, "mcp-helper")
    matches = [(child.pid, "python", "sleep 300")]

    # Live spawner (this process) → never selected.
    assert update_cmd._ledger_reapable_backend_pids(matches) == []

    # Provably dead spawner → positively identified as reapable.
    _orphan_entry_for(child.pid)
    targets = update_cmd._ledger_reapable_backend_pids(matches)
    assert targets == [(child.pid, pytest.approx(psutil.Process(child.pid).create_time()))]


# ---------------------------------------------------------------------------
# Prune-on-write of exited children
# ---------------------------------------------------------------------------

def test_ledger_prunes_exited_children_on_next_write(ledger):
    doomed = subprocess.Popen(["sleep", "300"])
    pi.register_child(doomed.pid, "mcp-helper")
    doomed.kill()
    doomed.wait()
    deadline = time.time() + 5
    while time.time() < deadline and psutil.pid_exists(doomed.pid):
        time.sleep(0.05)

    survivor = subprocess.Popen(["sleep", "300"])
    try:
        pi.register_child(survivor.pid, "mcp-helper")
        entries = json.loads(ledger.read_text(encoding="utf-8"))
        pids = [e["pid"] for e in entries]
        assert survivor.pid in pids
        assert doomed.pid not in pids
    finally:
        survivor.kill()
        survivor.wait()
