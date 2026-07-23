"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


@pytest.mark.asyncio
async def test_dispatcher_and_auto_decompose_do_not_tick_during_drain(tmp_path):
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._external_drain_active = True
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Initial startup delay, then one admission-closed wait.
        if len(sleep_calls) >= 2:
            runner._running = False

    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": True,
        }
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), patch(
        "gateway.kanban_watchers._acquire_singleton_lock",
        return_value=(None, "unavailable"),
    ), patch.object(kb, "kanban_home", return_value=tmp_path), patch.object(
        kb, "reap_worker_zombies"
    ) as reap, patch.object(kb, "dispatch_once") as dispatch, patch(
        "asyncio.sleep", side_effect=fake_sleep
    ):
        await runner._kanban_dispatcher_watcher()

    reap.assert_not_called()
    dispatch.assert_not_called()


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)
