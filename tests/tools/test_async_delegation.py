"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry, format_process_notification


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    tracker = tmp_path / "async_delegations.json"

    def _test_persistence_path(explicit=None):
        return Path(explicit).expanduser().resolve() if explicit else tracker

    monkeypatch.setattr(ad, "_persistence_path", _test_persistence_path)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def test_active_for_session_counts_every_live_delegation_state():
    with ad._records_lock:
        ad._records.update(
            {
                "running": {
                    "status": "running",
                    "origin_ui_session_id": "desktop-sid",
                },
                "stalling": {
                    "status": "stalling",
                    "origin_ui_session_id": "desktop-sid",
                },
                "finalizing": {
                    "status": "finalizing",
                    "origin_ui_session_id": "desktop-sid",
                },
                "completed": {
                    "status": "completed",
                    "origin_ui_session_id": "desktop-sid",
                },
                "other-session": {
                    "status": "running",
                    "origin_ui_session_id": "other-sid",
                },
            }
        )

    assert ad.active_for_session("desktop-sid") == 3
    assert ad.active_for_session("other-sid") == 1
    assert ad.active_for_session("") == 0


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "User is a philosopher",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None,
                "error": "cancelled"}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stalled" in evt["error"]
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    consumer = r'''
import json
from tools.process_registry import process_registry
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [sys.executable, "-c", "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt


def test_recovery_marks_running_record_lost_when_owner_dead():
    """Recovery SHOULD mark a running record lost when its owner PID is dead."""
    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = str(pathlib.Path(tmpdir) / "async_delegations.json")
        now = time.time()
        record = {
            "delegation_id": "deleg_test_dead",
            "goal": "test",
            "session_key": "",
            "status": "running",
            "dispatched_at": now,
            "completed_at": None,
            "last_heartbeat_at": now,
            "heartbeat_count": 0,
            "delivery_status": "running",
            "owner_pid": 999999,  # almost certainly dead
        }
        data = {"version": 1, "records": {"deleg_test_dead": {
            "delegation_id": "deleg_test_dead",
            "record": record,
            "status": "running",
            "delivery_status": "running",
        }}}
        import json as _json
        pathlib.Path(tracker).write_text(_json.dumps(data), encoding="utf-8")

        ad._recovery_attempted = False
        result = ad.recover_async_delegations(tracker)

        assert result["lost"] == 1, "Recovery should mark a dead-owner record as lost"


# Review-round regressions: ownership proof, truthful interruption, and durability.

def _review_record(delegation_id, *, status="running", is_batch=False, interrupt_fn=None):
    now = time.time()
    record = {
        "delegation_id": delegation_id,
        "goal": "review lifecycle probe",
        "goals": ["a", "b"] if is_batch else None,
        "session_key": "",
        "status": status,
        "delivery_status": status,
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "interrupt_fn": interrupt_fn,
        "is_batch": is_batch,
        "owner_pid": os.getpid(),
    }
    ad._records[delegation_id] = record
    return record


def test_recovery_rejects_reused_owner_pid(tmp_path, monkeypatch):
    tracker = tmp_path / "owner-reused.json"
    now = time.time()
    record = {
        "delegation_id": "deleg_owner_reused",
        "goal": "ownership probe",
        "session_key": "",
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 4242,
        "owner_started_at": 111,
    }
    tracker.write_text(json.dumps({"version": 1, "records": {
        record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": "running",
            "delivery_status": "running",
        }
    }}))
    monkeypatch.setattr(ad, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(ad, "_process_start_time", lambda pid: 222)

    result = ad.recover_async_delegations(tracker)

    assert result["lost"] == 1
    stored = json.loads(tracker.read_text())["records"][record["delegation_id"]]
    assert stored["status"] == "lost"


def test_recovery_degrades_missing_owner_identity_to_unknown(tmp_path, monkeypatch):
    tracker = tmp_path / "owner-unknown.json"
    now = time.time()
    record = {
        "delegation_id": "deleg_owner_unknown",
        "goal": "ownership probe",
        "session_key": "",
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
    }
    tracker.write_text(json.dumps({"version": 1, "records": {
        record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": "running",
            "delivery_status": "running",
        }
    }}))
    monkeypatch.setattr(ad, "_pid_exists", lambda pid: True)

    result = ad.recover_async_delegations(tracker)

    assert result["lost"] == 0
    stored = json.loads(tracker.read_text())["records"][record["delegation_id"]]
    assert stored["status"] == "running"
    assert stored["record"]["owner_liveness"] == "unknown"


def test_recovery_marks_dead_interrupting_owner_lost(tmp_path, monkeypatch):
    tracker = tmp_path / "interrupting-dead.json"
    now = time.time()
    record = {
        "delegation_id": "deleg_interrupting_dead",
        "goal": "interrupting recovery probe",
        "session_key": "",
        "status": "interrupting",
        "delivery_status": "interrupting",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 4242,
        "owner_started_at": 111,
    }
    tracker.write_text(json.dumps({"version": 1, "records": {
        record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": "interrupting",
            "delivery_status": "interrupting",
        }
    }}))
    monkeypatch.setattr(ad, "_pid_exists", lambda pid: False)

    result = ad.recover_async_delegations(tracker)

    assert result["lost"] == 1
    stored = json.loads(tracker.read_text())["records"][record["delegation_id"]]
    assert stored["status"] == "lost"


def test_interrupting_record_is_never_pruned_as_terminal(monkeypatch):
    record = _review_record("deleg_interrupting_retained", status="interrupting")
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 0)

    with ad._records_lock:
        ad._prune_completed_locked()
        ad._cleanup_locked(now=time.time() + 365 * 24 * 60 * 60)

    assert ad._records[record["delegation_id"]]["status"] == "interrupting"


def test_recovery_preserves_other_live_process_then_loses_it_after_exit(tmp_path):
    tracker = tmp_path / "two-process-owner.json"
    ready = tmp_path / "owner-ready"
    stop = tmp_path / "owner-stop"
    repo = Path(__file__).resolve().parents[2]
    script = r'''
import json, os, sys, time
from pathlib import Path
from gateway.status import get_process_start_time
tracker, ready, stop = map(Path, sys.argv[1:])
now = time.time()
record = {
    "delegation_id": "deleg_other_process",
    "goal": "two-process probe",
    "session_key": "",
    "status": "running",
    "delivery_status": "running",
    "dispatched_at": now,
    "last_heartbeat_at": now,
    "owner_pid": os.getpid(),
    "owner_started_at": get_process_start_time(os.getpid()),
}
tracker.write_text(json.dumps({"version": 1, "records": {
    record["delegation_id"]: {
        "delegation_id": record["delegation_id"],
        "record": record,
        "status": "running",
        "delivery_status": "running",
    }
}}))
ready.write_text("ready")
while not stop.exists():
    time.sleep(0.02)
'''
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tracker), str(ready), str(stop)],
        cwd=repo,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "owner process did not publish its record"

        assert ad.recover_async_delegations(tracker)["lost"] == 0
        stored = json.loads(tracker.read_text())["records"]["deleg_other_process"]
        assert stored["status"] == "running"

        stop.write_text("stop", encoding="utf-8")
        owner.wait(timeout=5)
        assert ad.recover_async_delegations(tracker)["lost"] == 1
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)


def test_interrupt_callback_failure_does_not_claim_terminal_state():
    def broken_interrupt():
        raise RuntimeError("signal failed")

    record = _review_record("deleg_interrupt_raises", interrupt_fn=broken_interrupt)

    assert ad.interrupt_all(reason="test") == 0
    assert record["status"] == "running"
    assert record["interrupt_error"] == "RuntimeError: signal failed"
    assert _drain_for(record["delegation_id"], timeout=0.2) is None


def test_interrupt_without_callback_does_not_claim_terminal_state():
    record = _review_record("deleg_interrupt_missing")

    assert ad.interrupt_all(reason="test") == 0
    assert record["status"] == "running"
    assert record["interrupt_error"] == "interrupt callback unavailable"
    assert _drain_for(record["delegation_id"], timeout=0.2) is None


def test_running_batch_interrupt_waits_for_finalize_and_emits_once():
    record = _review_record(
        "deleg_batch_running", is_batch=True, interrupt_fn=lambda: None,
    )

    assert ad.interrupt_all(reason="test") == 1
    assert record["status"] == "interrupting"
    assert ad.active_count() == 1
    assert _drain_for(record["delegation_id"], timeout=0.2) is None

    ad._finalize_batch(
        record["delegation_id"],
        {"results": [{"status": "completed", "summary": "finished"}]},
        "completed",
    )
    event = _drain_for(record["delegation_id"])
    assert event is not None
    assert event["status"] == "completed"
    assert event["is_batch"] is True
    assert _drain_for(record["delegation_id"], timeout=0.2) is None


def test_scheduled_batch_interrupt_uses_batch_event_shape():
    record = _review_record(
        "deleg_batch_scheduled",
        status="scheduled",
        is_batch=True,
        interrupt_fn=lambda: None,
    )

    assert ad.interrupt_all(reason="test") == 1
    assert record["status"] == "interrupted"
    event = _drain_for(record["delegation_id"])
    assert event is not None
    assert event["status"] == "interrupted"
    assert event["is_batch"] is True
    assert event["goals"] == ["a", "b"]
    assert event["results"] == []


def test_single_dispatch_rejects_when_initial_persistence_fails(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(ad, "_persist_record", lambda *args, **kwargs: False)

    result = ad.dispatch_async_delegation(
        goal="must not run", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: ran.set() or {"status": "completed"},
        max_async_children=1,
    )

    assert result["status"] == "rejected"
    assert "persist" in result["error"].lower()
    assert not ran.wait(timeout=0.2)
    assert ad.active_count() == 0


def test_submit_failure_never_creates_durable_record(monkeypatch):
    class BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("pool closed")

    monkeypatch.setattr(ad, "_get_executor", lambda _n: BrokenExecutor())
    result = ad.dispatch_async_delegation(
        goal="never scheduled", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: {"status": "completed"},
        max_async_children=1,
    )

    assert result["status"] == "rejected"
    tracker = ad._persistence_path()
    records = json.loads(tracker.read_text()).get("records", {}) if tracker.exists() else {}
    assert records == {}


def test_completion_not_published_without_durable_terminal_state(monkeypatch):
    record = _review_record("deleg_terminal_persist_failure")
    monkeypatch.setattr(ad, "_persist_record", lambda *args, **kwargs: False)

    ad._finalize(
        record["delegation_id"],
        {"status": "completed", "summary": "done"},
        "completed",
    )

    assert _drain_for(record["delegation_id"], timeout=0.2) is None
    assert record["status"] == "completed"
    assert record["delivery_status"] == "pending"
    assert "persist" in record["delivery_error"].lower()


def test_standard_batch_rejects_when_scheduled_persistence_fails(monkeypatch):
    ran = threading.Event()
    real_persist = ad._persist_record

    def fail_scheduled(record, **kwargs):
        if kwargs.get("delivery_status") == "scheduled":
            return False
        return real_persist(record, **kwargs)

    monkeypatch.setattr(ad, "_persist_record", fail_scheduled)
    result = ad.dispatch_async_delegation_batch(
        goals=["a", "b"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: ran.set() or {"results": []},
        max_async_children=1,
    )

    assert result["status"] == "rejected"
    assert not ran.wait(timeout=0.2)


def test_standard_batch_rejects_when_running_persistence_fails(monkeypatch):
    ran = threading.Event()
    real_persist = ad._persist_record

    def fail_running(record, **kwargs):
        if kwargs.get("delivery_status") == "running":
            return False
        return real_persist(record, **kwargs)

    monkeypatch.setattr(ad, "_persist_record", fail_running)
    result = ad.dispatch_async_delegation_batch(
        goals=["a", "b"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: ran.set() or {"results": []},
        max_async_children=1,
    )

    assert result["status"] == "rejected"
    assert not ran.wait(timeout=0.2)


def test_interrupt_race_during_running_checkpoint_keeps_terminal_event(monkeypatch):
    """A running->interrupting race must not delete the accepted record."""
    running_checkpoint_entered = threading.Event()
    allow_running_checkpoint = threading.Event()
    child_interrupted = threading.Event()
    real_persist = ad._persist_record

    def delayed_running_checkpoint(record, **kwargs):
        if kwargs.get("delivery_status") == "running":
            running_checkpoint_entered.set()
            assert allow_running_checkpoint.wait(timeout=5)
        return real_persist(record, **kwargs)

    monkeypatch.setattr(ad, "_persist_record", delayed_running_checkpoint)

    def runner():
        assert child_interrupted.wait(timeout=5)
        return {"status": "interrupted", "summary": "stopped"}

    dispatch_result = {}

    def dispatch():
        dispatch_result["value"] = ad.dispatch_async_delegation(
            goal="checkpoint race",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=runner,
            interrupt_fn=child_interrupted.set,
            max_async_children=1,
        )

    thread = threading.Thread(target=dispatch)
    thread.start()
    assert running_checkpoint_entered.wait(timeout=5)
    assert ad.interrupt_all(reason="race") == 1
    allow_running_checkpoint.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    result = dispatch_result["value"]
    assert result["status"] == "dispatched"
    event = _drain_for(result["delegation_id"])
    assert event is not None
    assert event["status"] == "interrupted"


def test_stale_scheduled_checkpoint_cannot_overwrite_terminal_state():
    delegation_id = "deleg_monotonic_checkpoint"
    scheduled = _review_record(delegation_id, status="scheduled", is_batch=True)
    terminal = dict(scheduled)
    terminal.update({
        "status": "interrupted",
        "delivery_status": "pending",
        "completed_at": time.time(),
    })

    assert ad._persist_record(terminal, delivery_status="pending")
    assert not ad._persist_record(scheduled, delivery_status="scheduled")

    stored = json.loads(ad._persistence_path().read_text())["records"][delegation_id]
    assert stored["status"] == "interrupted"
    assert stored["record"]["status"] == "interrupted"


def test_recovery_does_not_publish_when_checkpoint_write_fails(tmp_path, monkeypatch):
    tracker = tmp_path / "recovery-write-fails.json"
    now = time.time()
    record = {
        "delegation_id": "deleg_recovery_write_fails",
        "goal": "recovery durability",
        "session_key": "",
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 4242,
        "owner_started_at": 111,
    }
    tracker.write_text(json.dumps({"version": 1, "records": {
        record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": "running",
            "delivery_status": "running",
        }
    }}))
    monkeypatch.setattr(ad, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(
        ad,
        "_write_persisted_unlocked",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        ad.recover_async_delegations(tracker)

    assert _drain_for(record["delegation_id"], timeout=0.2) is None


def test_dead_batch_recovery_preserves_batch_event_shape(tmp_path, monkeypatch):
    tracker = tmp_path / "dead-batch.json"
    now = time.time()
    record = {
        "delegation_id": "deleg_dead_batch",
        "goal": "dead batch",
        "goals": ["a", "b"],
        "session_key": "",
        "status": "running",
        "delivery_status": "running",
        "dispatched_at": now,
        "last_heartbeat_at": now,
        "owner_pid": 4242,
        "owner_started_at": 111,
        "is_batch": True,
    }
    tracker.write_text(json.dumps({"version": 1, "records": {
        record["delegation_id"]: {
            "delegation_id": record["delegation_id"],
            "record": record,
            "status": "running",
            "delivery_status": "running",
        }
    }}))
    monkeypatch.setattr(ad, "_pid_exists", lambda pid: False)

    result = ad.recover_async_delegations(tracker)
    event = _drain_for(record["delegation_id"])

    assert result["lost"] == 1
    assert event is not None
    assert event["status"] == "lost"
    assert event["is_batch"] is True
    assert event["goals"] == ["a", "b"]
    assert event["results"] == []

