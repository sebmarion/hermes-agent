import queue

import cli as cli_module
from cli import (
    _enqueue_automatic_notification,
    _notification_turn_committed,
    _start_process_loop_with_durable_recovery,
)


def test_enqueue_automatic_notification_preserves_raw_event_for_post_turn_ack():
    pending = queue.Queue()
    event = {
        "type": "completion",
        "event_id": "process:proc_cli_ack:completion",
        "session_id": "proc_cli_ack",
    }

    _enqueue_automatic_notification(pending, event, "[SYSTEM: completed]")

    prompt, images, meta = pending.get_nowait()
    assert prompt == "[SYSTEM: completed]"
    assert images == []
    assert meta == {"kind": "automatic_notification", "event": event}


def test_notification_turn_commit_requires_exact_new_user_message():
    prior = [{"role": "user", "content": "older"}]
    prompt = "[SYSTEM: completed]"

    assert not _notification_turn_committed(prior, 1, prompt)
    assert not _notification_turn_committed(
        prior + [{"role": "assistant", "content": prompt}],
        1,
        prompt,
    )
    assert not _notification_turn_committed(
        prior + [{"role": "user", "content": prompt}],
        1,
        prompt,
    )
    assert not _notification_turn_committed(
        prior
        + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "handled"},
        ],
        1,
        prompt,
        turn_succeeded=False,
    )
    assert _notification_turn_committed(
        prior
        + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "handled"},
        ],
        1,
        prompt,
    )


def test_cli_replays_both_durable_outboxes_before_consumer_thread(monkeypatch):
    from tools import async_delegation
    from tools.process_registry import process_registry

    order = []

    class _CapturingThread:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            order.append("thread-start")

    monkeypatch.setattr(
        process_registry,
        "recover_completion_notifications",
        lambda: order.append("recover-process") or 0,
    )
    monkeypatch.setattr(
        async_delegation,
        "recover_async_delegations",
        lambda: order.append("recover-async") or {"queued": 0, "lost": 0},
    )
    monkeypatch.setattr(cli_module.threading, "Thread", _CapturingThread)

    _start_process_loop_with_durable_recovery(lambda: None)

    assert order == ["recover-process", "recover-async", "thread-start"]
