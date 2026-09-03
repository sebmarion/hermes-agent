"""Lifecycle-scoped gateway delivery regressions for terminal completions.

The gateway contract here is deliberately narrower than exactly-once: one live
GatewayRunner suppresses concurrent/replayed copies after successful adapter
injection, failed injection remains retryable, and durable async-delegation
state (when available) is acknowledged through its authoritative SQLite API.
"""

import asyncio
import json
import queue
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Any current/future durable compatibility path must stay in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _runner(adapter, *, origins=None):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries=origins or {},
    )
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._background_tasks = set()
    return runner


def _async_event(delegation_id="deleg_duplicate", *, session_key="agent:main:telegram:dm:12345:678"):
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": session_key,
        "goal": "Investigate flaky test",
        "status": "completed",
        "summary": "Found it",
        "api_calls": 1,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
        # PR #62479 stamps these on gateway-owned events. They must not
        # change the producer identity used for queue replay.
        "origin_profile": "default",
        "origin_hermes_home": "/tmp/hermes-default",
    }


def _completion_event(*, started_at, session_id="proc_reused"):
    return {
        "type": "completion",
        "session_id": session_id,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "started_at": started_at,
        "command": "echo done",
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "done\n",
    }


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


def test_duplicate_async_queue_replay_injects_once(monkeypatch, isolated_registry):
    """Byte-identical queue replays produce one turn in one gateway lifecycle."""
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(dict(_async_event()))
    isolated.put(dict(_async_event()))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()


def test_unroutable_async_event_is_not_requeued_forever(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    event = _async_event("deleg_desktop_or_cli")
    event["session_key"] = "20260711_unparseable_ui_session"
    isolated.put(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_not_awaited()
    assert isolated.empty()


def test_concurrent_claims_share_the_same_narrow_delivery_seam():
    """Concurrent consumers in one runner cannot both enter the adapter."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_injection(_event):
        entered.set()
        await release.wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_blocked_injection))
    runner = _runner(adapter)
    event = _async_event()
    text = "completion"

    async def _exercise():
        first = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await entered.wait()
        second = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    assert sorted(asyncio.run(_exercise()), key=str) == [None, True]
    adapter.handle_message.assert_awaited_once()


def test_failed_async_injection_is_retried_and_only_success_is_acked(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_async_event())

    adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=[RuntimeError("temporary"), None])
    )
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)

    from tools import async_delegation

    acknowledgements = []
    monkeypatch.setattr(
        async_delegation,
        "complete_completion_delivery",
        lambda delegation_id, _claim_id: acknowledgements.append(delegation_id) or True,
        raising=False,
    )

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert adapter.handle_message.await_count == 2
    assert acknowledgements == ["deleg_duplicate"]


def _persist_pending_completion(event):
    from tools import async_delegation

    async_delegation._persist_dispatch({
        "delegation_id": event["delegation_id"],
        "session_key": event["session_key"],
        "origin_ui_session_id": "",
        "parent_session_id": event.get("parent_session_id"),
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, {
        "status": "completed",
        "summary": event["summary"],
    })


def test_terminal_outbox_commit_atomically_settles_claimed_completion():
    """Settlement and durable terminal publication are one transaction."""
    from tools import async_delegation

    event = _async_event("deleg-terminal-outbox", session_key="session-terminal-outbox")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-outbox"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    terminal_event = {
        "type": "message.complete",
        "session_id": "session-terminal-outbox",
        "payload": {"text": "Found it", "status": "success"},
    }

    committed = async_delegation.commit_terminal_output(
        event,
        claim_id,
        terminal_event,
    )

    assert committed["delivery_id"] == "async-delegation:deleg-terminal-outbox"
    assert async_delegation.get_durable_delegation(event["delegation_id"])[
        "delivery_state"
    ] == "delivered"
    assert async_delegation.list_terminal_outputs("session-terminal-outbox") == [
        {
            "delivery_id": "async-delegation:deleg-terminal-outbox",
            "delegation_id": "deleg-terminal-outbox",
            "session_id": "session-terminal-outbox",
            "event": terminal_event,
        }
    ]
    committed_again = async_delegation.commit_terminal_output(event, claim_id, terminal_event)
    assert committed_again == {**committed, "already_committed": True}
    assert len(async_delegation.list_terminal_outputs("session-terminal-outbox")) == 1


def _persist_terminal_assistant(session_id, content, delivery_id):
    from tools import async_delegation

    with async_delegation._transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                display_metadata TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            """INSERT INTO messages(session_id, role, content, display_metadata, active)
               VALUES (?, 'assistant', ?, ?, 1)""",
            (session_id, content, json.dumps({"delivery_id": delivery_id})),
        )


def test_terminal_outbox_ack_rejects_inactive_transcript_identity():
    from tools import async_delegation

    event = _async_event("deleg-inactive-transcript", session_key="inactive-session")
    _persist_pending_completion(event)
    claim_id = "replay-claim-inactive"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    committed = async_delegation.commit_terminal_output(
        event,
        claim_id,
        {
            "type": "message.complete",
            "stored_session_id": "inactive-session",
            "payload": {"text": "rewound answer"},
        },
    )
    with async_delegation._transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, display_metadata TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            "UPDATE messages SET active=0 WHERE session_id=?",
            ("inactive-session",),
        )
        conn.execute(
            """INSERT INTO messages(session_id, role, content, display_metadata, active)
               VALUES (?, 'assistant', ?, ?, 0)""",
            (
                "inactive-session",
                "rewound answer",
                json.dumps({"delivery_id": committed["delivery_id"]}),
            ),
        )

    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "inactive-session", claim_id
    ) is False
    assert async_delegation.list_terminal_outputs("inactive-session")


def test_terminal_transcript_identity_lookup_is_bounded_to_one_row():
    from tools import async_delegation

    class _NoFetchAllCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            return self._cursor.fetchone()

        def fetchall(self):
            raise AssertionError("terminal identity lookup scanned all assistant rows")

    class _ConnProxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args):
            return _NoFetchAllCursor(self._conn.execute(*args))

    with async_delegation._transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, display_metadata TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            """INSERT INTO messages(session_id, role, content, display_metadata, active)
               VALUES ('bounded-session', 'assistant', 'answer', ?, 1)""",
            (json.dumps({"delivery_id": "async-delegation:bounded"}),),
        )
        assert async_delegation._has_persisted_terminal_assistant(
            _ConnProxy(conn), "bounded-session", "async-delegation:bounded"
        ) is True


def test_terminal_outbox_ack_is_idempotent_and_stops_replay():
    from tools import async_delegation

    event = _async_event("deleg-terminal-ack", session_key="session-terminal-ack")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-ack"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    terminal_event = {
        "type": "message.complete",
        "session_id": "session-terminal-ack",
        "payload": {"text": "Delivered", "status": "success"},
    }
    committed = async_delegation.commit_terminal_output(event, claim_id, terminal_event)
    _persist_terminal_assistant(
        "session-terminal-ack",
        "Delivered",
        committed["delivery_id"],
    )

    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "session-terminal-ack", claim_id
    )
    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "session-terminal-ack", claim_id
    )
    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "session-terminal-ack", "unrelated-transport"
    ) is False
    assert async_delegation.list_terminal_outputs("session-terminal-ack") == []


def test_terminal_outbox_replay_claim_is_exclusive_and_ack_fenced():
    from tools import async_delegation

    event = _async_event("deleg-terminal-replay-claim", session_key="shared-session")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-replay-claim"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    committed = async_delegation.commit_terminal_output(
        event,
        claim_id,
        {
            "type": "message.complete",
            "stored_session_id": "shared-session",
            "session_id": "runtime-before",
            "payload": {"text": "One durable answer"},
        },
    )
    _persist_terminal_assistant(
        "shared-session",
        "One durable answer",
        committed["delivery_id"],
    )

    assert async_delegation.ack_terminal_output(
        "async-delegation:deleg-terminal-replay-claim", "shared-session", "unclaimed-attacker"
    ) is False
    assert async_delegation.claim_terminal_outputs("shared-session", "client-a") == []
    assert async_delegation.mark_terminal_output_live_published(
        committed["delivery_id"], claim_id
    )
    [first] = async_delegation.claim_terminal_outputs("shared-session", "client-a")
    assert first["delivery_id"] == committed["delivery_id"]
    assert async_delegation.claim_terminal_outputs("shared-session", "client-b") == []
    assert not async_delegation.ack_terminal_output(
        committed["delivery_id"], "shared-session", "client-b"
    )
    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "shared-session", "client-a"
    )


def test_terminal_outbox_live_ack_requires_publisher_or_replay_claim():
    from tools import async_delegation

    event = _async_event("deleg-terminal-live-ack-fence", session_key="live-ack-session")
    _persist_pending_completion(event)
    publisher_claim = "tui-poller:live-ack"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], publisher_claim)
    committed = async_delegation.commit_terminal_output(
        event,
        publisher_claim,
        {
            "type": "message.complete",
            "stored_session_id": "live-ack-session",
            "session_id": "runtime-live-ack",
            "payload": {"text": "Live answer"},
        },
    )
    _persist_terminal_assistant(
        "live-ack-session",
        "Live answer",
        committed["delivery_id"],
    )
    assert async_delegation.mark_terminal_output_live_published(
        committed["delivery_id"], publisher_claim
    )

    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "live-ack-session", "unrelated-transport"
    ) is False
    assert async_delegation.ack_terminal_output(
        committed["delivery_id"], "live-ack-session", publisher_claim
    ) is True


def test_terminal_outbox_concurrent_replay_claims_have_one_winner():
    from tools import async_delegation

    event = _async_event("deleg-terminal-concurrent-claim", session_key="shared-concurrent-session")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-concurrent-claim"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    committed = async_delegation.commit_terminal_output(
        event,
        claim_id,
        {
            "type": "message.complete",
            "stored_session_id": "shared-concurrent-session",
            "session_id": "runtime-before",
            "payload": {"text": "One concurrent answer"},
        },
    )

    assert async_delegation.mark_terminal_output_live_published(
        committed["delivery_id"], claim_id
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda client: async_delegation.claim_terminal_outputs(
                    "shared-concurrent-session", client
                ),
                ("client-a", "client-b"),
            )
        )

    assert sorted(len(result) for result in results) == [0, 1]
    winner = next(result for result in results if result)
    assert winner[0]["delivery_id"] == committed["delivery_id"]

def test_terminal_outbox_ack_rejects_wrong_stable_session():
    from tools import async_delegation

    event = _async_event("deleg-terminal-wrong-session", session_key="session-terminal-wrong-session")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-wrong-session"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    committed = async_delegation.commit_terminal_output(
        event,
        claim_id,
        {
            "type": "message.complete",
            "session_id": "runtime-session",
            "stored_session_id": "session-terminal-wrong-session",
            "payload": {"text": "Protected answer"},
        },
    )

    assert not async_delegation.ack_terminal_output(committed["delivery_id"], "other-session")
    assert async_delegation.list_terminal_outputs("session-terminal-wrong-session")


def test_terminal_outbox_rejects_wrong_claim_without_inserting_or_settling():
    import pytest
    from tools import async_delegation

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-atomic-reject",
        "session_key": "stable-atomic",
        "parent_session_id": "stable-atomic",
        "dispatched_at": 1.0,
        "summary": "child result",
        "event": {"type": "message.complete", "session_id": "runtime"},
    }
    _persist_pending_completion(event)
    assert async_delegation.claim_completion_delivery("deleg-atomic-reject", "claim-atomic") is True

    terminal_event = {
        "type": "message.complete",
        "session_id": "runtime",
        "stored_session_id": "stable-atomic",
        "payload": {"text": "Should not commit"},
    }
    with pytest.raises(Exception):
        async_delegation.commit_terminal_output(event, "wrong-claim", terminal_event)

    assert async_delegation.list_terminal_outputs("stable-atomic") == []
    assert async_delegation.event_delivery_claim_status(event, "claim-atomic") == "owned"


def test_terminal_outbox_routes_by_stable_session_key_after_runtime_restart():
    from tools import async_delegation

    event = _async_event("deleg-terminal-restart", session_key="stable-session-key")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-restart"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    terminal_event = {
        "type": "message.complete",
        "session_id": "old-runtime-sid",
        "stored_session_id": "stable-session-key",
        "payload": {"text": "Recovered after restart", "status": "success"},
    }

    async_delegation.commit_terminal_output(event, claim_id, terminal_event)

    assert async_delegation.list_terminal_outputs("old-runtime-sid") == []
    [replayed] = async_delegation.list_terminal_outputs("stable-session-key")
    assert replayed["session_id"] == "stable-session-key"
    assert replayed["event"] == terminal_event


def test_terminal_outbox_rejects_destination_not_owned_by_claimed_event():
    """A claimed completion cannot redirect its durable output to another session."""
    from tools import async_delegation

    event = _async_event("deleg-terminal-route-fence")
    event["session_key"] = "owned-stable-session"
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:terminal-route-fence"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)

    with pytest.raises(ValueError, match="does not belong"):
        async_delegation.commit_terminal_output(
            event,
            claim_id,
            {
                "type": "message.complete",
                "stored_session_id": "unrelated-stable-session",
                "session_id": "runtime",
                "payload": {"text": "must not be redirected"},
            },
        )

    assert async_delegation.list_terminal_outputs("unrelated-stable-session") == []
    assert async_delegation.get_durable_delegation(event["delegation_id"])[
        "delivery_state"
    ] == "pending"


def test_terminal_outbox_uses_persisted_event_identity_not_mutable_claim_payload():
    from tools import async_delegation

    event = _async_event("deleg-persisted-identity", session_key="persisted-session")
    _persist_pending_completion(event)
    claim_id = "claim-persisted-identity"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    event["session_key"] = "attacker-session"

    with pytest.raises(ValueError, match="destination"):
        async_delegation.commit_terminal_output(
            event,
            claim_id,
            {
                "type": "message.complete",
                "stored_session_id": "attacker-session",
                "payload": {"text": "Misrouted"},
            },
        )
    assert async_delegation.get_durable_delegation(event["delegation_id"])[
        "delivery_state"
    ] == "pending"

def test_dispatch_persistence_does_not_replace_existing_terminal_record(monkeypatch):
    """Reusing an id cannot reset a delivered row behind its durable outbox."""
    from tools import async_delegation

    event = _async_event("deleg-no-dispatch-replace")
    event["session_key"] = "stable-no-replace"
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:no-dispatch-replace"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    terminal_event = {
        "type": "message.complete",
        "stored_session_id": "stable-no-replace",
        "session_id": "runtime",
        "payload": {"text": "already durable"},
    }
    async_delegation.commit_terminal_output(event, claim_id, terminal_event)

    with pytest.raises(Exception):
        async_delegation._persist_dispatch({
            "delegation_id": event["delegation_id"],
            "session_key": "attacker-or-replacement",
            "origin_ui_session_id": "",
            "parent_session_id": None,
            "dispatched_at": 9999.0,
        })

    durable = async_delegation.get_durable_delegation(event["delegation_id"])
    assert durable["delivery_state"] == "delivered"
    assert async_delegation.list_terminal_outputs("stable-no-replace")[0]["event"] == terminal_event


def test_terminal_outbox_replay_rebinds_runtime_session_without_mutating_record():
    from tools import async_delegation

    row = {
        "delivery_id": "async-delegation:deleg-rebind",
        "session_id": "persisted-session",
        "event": {
            "type": "message.complete",
            "session_id": "runtime-before",
            "stored_session_id": "persisted-session",
            "payload": {"text": "durable answer"},
        },
    }

    replay = async_delegation.replay_terminal_output(row, "runtime-after")

    assert replay["session_id"] == "runtime-after"
    assert replay["payload"]["delivery_id"] == "async-delegation:deleg-rebind"
    assert row["event"]["session_id"] == "runtime-before"


def test_stale_claim_is_not_stolen_while_owner_process_is_alive(tmp_path):
    """A missed heartbeat must not permit duplicate work in a live owner process."""
    import os
    import sqlite3
    import time

    from gateway.status import get_process_start_time
    from tools import async_delegation

    event = _async_event("deleg-live-owner-lease")
    _persist_pending_completion(event)
    owner_started_at = get_process_start_time(os.getpid())
    assert owner_started_at is not None
    owner_claim = f"tui-notify:{os.getpid()}:{owner_started_at}:owner"
    assert async_delegation.claim_completion_delivery(
        event["delegation_id"], owner_claim
    )
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_claimed_at=? WHERE delegation_id=?",
            (time.time() - 301, event["delegation_id"]),
        )

    assert not async_delegation.claim_completion_delivery(
        event["delegation_id"], f"tui-post-turn:{os.getpid()}:competitor"
    )


def test_event_delivery_claim_status_reports_unclaimed_after_release():
    from tools import async_delegation

    event = _async_event("deleg-claim-status")
    _persist_pending_completion(event)
    claim_id = "tui-poller:123:claim-status"
    assert async_delegation.claim_completion_delivery(event["delegation_id"], claim_id)
    assert async_delegation.release_event_delivery(event, claim_id)

    assert async_delegation.event_delivery_claim_status(event, claim_id) == "unclaimed"


def test_restore_clears_fresh_claim_owned_by_dead_process():
    import queue
    import time

    from tools import async_delegation

    event = _async_event("deleg-dead-owner-restore")
    event["dispatched_at"] = time.time()
    event["completed_at"] = event["dispatched_at"]
    _persist_pending_completion(event)
    dead_claim = "tui-poller:999999:dead-owner"
    assert async_delegation.claim_completion_delivery(
        event["delegation_id"], dead_claim
    )
    restored = queue.Queue()

    assert async_delegation.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == event["delegation_id"]
    assert async_delegation.claim_event_delivery(event, "restart-poller") is not None


def test_explicit_kill_returns_output_before_consuming_notification(monkeypatch):
    import tools.process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_consumed",
        command="sleep 999",
        task_id="task",
        started_at=1.0,
        output_buffer="important terminal output\n",
        notify_on_complete=True,
    )
    session.process = MagicMock()
    session.process.pid = 4242
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    monkeypatch.setattr(pr_module, "process_registry", registry)

    result = registry.kill_process(session.id)
    assert result["status"] == "killed"
    assert result["output"] == "important terminal output\n"
    assert registry.is_completion_consumed(session.id)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    adapter.handle_message.assert_not_awaited()


def test_process_tool_redacts_explicit_kill_output(monkeypatch):
    from tools import process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_redacted",
        command="printenv",
        task_id="task",
        started_at=1.0,
        output_buffer="PRIVATE_TOKEN=opaque-value\n",
        exited=True,
        exit_code=0,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)

    def _redact(result):
        assert result["output"] == "PRIVATE_TOKEN=opaque-value\n"
        result["output"] = "PRIVATE_TOKEN=<redacted>\n"
        return result

    monkeypatch.setattr(pr_module, "_redact_process_result", _redact)

    result = json.loads(pr_module._handle_process({
        "action": "kill",
        "session_id": session.id,
    }))
    assert result["output"] == "PRIVATE_TOKEN=<redacted>\n"


def test_autonomous_completion_redacts_real_command_and_output_secrets(monkeypatch):
    import agent.redact as redact_module
    import tools.process_registry as pr_module

    secret = "abc123randomopaquetokenvalue999"
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_autonomous_redaction",
        command=f"printenv MY_SERVICE_TOKEN={secret}",
        task_id="task",
        started_at=1234.5,
        output_buffer=f"MY_SERVICE_TOKEN={secret}\nHOME=/home/user\n",
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", True)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    delivered = adapter.handle_message.await_args.args[0]
    assert secret not in delivered.text
    assert "HOME=/home/user" in delivered.text


def test_concurrent_process_watchers_coalesce_one_session_completion_turn(monkeypatch):
    """Concurrent terminal watchers for one session must re-enter the agent once."""
    import tools.process_registry as pr_module

    registry = ProcessRegistry()
    watchers = []
    for index in range(3):
        session = ProcessSession(
            id=f"proc_batch_{index}",
            command=f"printf batch-{index}",
            task_id=f"task-{index}",
            started_at=1000.0 + index,
            output_buffer=f"batch-{index}\n",
            exited=True,
            exit_code=0,
            notify_on_complete=True,
        )
        registry._finished[session.id] = session
        watchers.append({
            "session_id": session.id,
            "check_interval": 0,
            "session_key": "agent:main:telegram:dm:123",
            "platform": "telegram",
            "chat_type": "dm",
            "chat_id": "123",
            "notify_on_complete": True,
        })
    monkeypatch.setattr(pr_module, "process_registry", registry)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _exercise():
        await asyncio.gather(*(
            runner._run_process_watcher(watcher)
            for watcher in watchers
        ))

    asyncio.run(_exercise())

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert "3 background processes completed" in delivered.text
    for index in range(3):
        assert f"proc_batch_{index}" in delivered.text


def test_completion_arriving_during_batch_delivery_schedules_next_flush():
    """A new event cannot be stranded behind an in-flight batch for its route."""
    first_delivery_entered = asyncio.Event()
    release_first_delivery = asyncio.Event()
    delivery_count = 0

    async def _deliver(_event):
        nonlocal delivery_count
        delivery_count += 1
        if delivery_count == 1:
            first_delivery_entered.set()
            await release_first_delivery.wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_deliver))
    runner = _runner(adapter)

    async def _exercise():
        first = asyncio.create_task(runner._enqueue_process_completion_notification(
            "first completion",
            _completion_event(started_at=1.0, session_id="proc_first"),
        ))
        await first_delivery_entered.wait()
        second = asyncio.create_task(runner._enqueue_process_completion_notification(
            "second completion",
            _completion_event(started_at=2.0, session_id="proc_second"),
        ))
        release_first_delivery.set()
        assert await first is True
        assert await asyncio.wait_for(second, timeout=1.0) is True

    asyncio.run(_exercise())

    assert adapter.handle_message.await_count == 2


def test_completion_batches_do_not_cross_conversation_routes():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    first = _completion_event(started_at=1.0, session_id="proc_route_a")
    second = _completion_event(started_at=2.0, session_id="proc_route_b")
    second["session_key"] = "agent:main:telegram:dm:456"
    second["chat_id"] = "456"

    async def _exercise():
        return await asyncio.gather(
            runner._enqueue_process_completion_notification("first", first),
            runner._enqueue_process_completion_notification("second", second),
        )

    assert asyncio.run(_exercise()) == [True, True]
    assert adapter.handle_message.await_count == 2


def test_failed_coalesced_delivery_retries_all_entries():
    attempts = 0

    async def _deliver(_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary adapter failure")

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_deliver))
    runner = _runner(adapter)
    events = [
        _completion_event(started_at=float(index), session_id=f"proc_retry_{index}")
        for index in range(2)
    ]

    async def _enqueue_all():
        return await asyncio.gather(*(
            runner._enqueue_process_completion_notification(f"event-{index}", event)
            for index, event in enumerate(events)
        ))

    async def _exercise():
        assert await _enqueue_all() == [False, False]
        assert await _enqueue_all() == [True, True]

    asyncio.run(_exercise())
    assert adapter.handle_message.await_count == 2


def test_coalesced_success_records_every_completion_identity():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    events = [
        _completion_event(started_at=float(index), session_id=f"proc_ledger_{index}")
        for index in range(3)
    ]

    async def _exercise():
        return await asyncio.gather(*(
            runner._enqueue_process_completion_notification(f"event-{index}", event)
            for index, event in enumerate(events)
        ))

    assert asyncio.run(_exercise()) == [True, True, True]
    for event in events:
        identity = runner._completion_delivery_identity(event)
        assert identity in runner._completion_deliveries_delivered


def test_coalesced_format_bounds_details_and_reports_omitted_count():
    async def _format():
        loop = asyncio.get_running_loop()
        entries = [
            (
                f"event-{index}",
                _completion_event(
                    started_at=float(index), session_id=f"proc_bound_{index}"
                ),
                loop.create_future(),
            )
            for index in range(12)
        ]
        return GatewayRunner._format_coalesced_process_completions(entries)

    text = asyncio.run(_format())

    for index in range(10):
        assert f"proc_bound_{index}" in text
    assert "proc_bound_10" not in text
    assert "proc_bound_11" not in text
    assert "and 2 more completion(s)" in text


def test_coalesced_format_force_redacts_output_when_redaction_disabled(monkeypatch):
    """A user setting cannot disable the gateway's outbound secret floor."""
    import agent.redact as redact_module

    secret = "abc123randomopaquetokenvalue999"
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", False)

    async def _format():
        loop = asyncio.get_running_loop()
        first = _completion_event(started_at=1.0, session_id="proc_secret")
        first["output"] = (
            f"MY_SERVICE_TOKEN={secret}\n"
            "HOME=/home/user\n"
        )
        second = _completion_event(started_at=2.0, session_id="proc_control")
        return GatewayRunner._format_coalesced_process_completions([
            ("first", first, loop.create_future()),
            ("second", second, loop.create_future()),
        ])

    text = asyncio.run(_format())

    assert secret not in text
    assert "HOME=/home/user" in text


def test_coalesced_format_redacts_before_truncating_output(monkeypatch):
    """Truncation cannot remove the prefix needed to recognize a secret."""
    import agent.redact as redact_module

    marker = "SHOULD_NOT_SURVIVE"
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", False)

    async def _format():
        loop = asyncio.get_running_loop()
        first = _completion_event(started_at=1.0, session_id="proc_long_secret")
        first["output"] = f"MY_SERVICE_TOKEN={'x' * 900}{marker}\n"
        second = _completion_event(started_at=2.0, session_id="proc_control")
        return GatewayRunner._format_coalesced_process_completions([
            ("first", first, loop.create_future()),
            ("second", second, loop.create_future()),
        ])

    text = asyncio.run(_format())

    assert marker not in text


def test_duplicate_primary_does_not_discard_fresh_batch_sibling():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    duplicate = _completion_event(started_at=1.0, session_id="proc_duplicate")
    fresh = _completion_event(started_at=2.0, session_id="proc_fresh")
    duplicate_identity = runner._completion_delivery_identity(duplicate)
    runner._completion_deliveries_delivered[duplicate_identity] = None

    async def _exercise():
        return await asyncio.gather(
            runner._enqueue_process_completion_notification("duplicate", duplicate),
            runner._enqueue_process_completion_notification("fresh", fresh),
        )

    assert asyncio.run(_exercise()) == [True, True]
    adapter.handle_message.assert_awaited_once()
    fresh_identity = runner._completion_delivery_identity(fresh)
    assert fresh_identity in runner._completion_deliveries_delivered


def test_batch_format_failure_resolves_waiters_for_retry(monkeypatch):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    monkeypatch.setattr(
        runner,
        "_format_coalesced_process_completions",
        MagicMock(side_effect=ValueError("bad batch")),
    )
    events = [
        _completion_event(started_at=float(index), session_id=f"proc_format_{index}")
        for index in range(2)
    ]

    async def _exercise():
        pending = asyncio.gather(*(
            runner._enqueue_process_completion_notification(f"event-{index}", event)
            for index, event in enumerate(events)
        ))
        return await asyncio.wait_for(pending, timeout=1.0)

    assert asyncio.run(_exercise()) == [False, False]
    adapter.handle_message.assert_not_awaited()


def test_shutdown_cancels_batch_during_window_and_settles_waiter_for_retry():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    sleep_entered = asyncio.Event()
    release_sleep = asyncio.Event()
    real_sleep = asyncio.sleep
    event = _completion_event(started_at=1.0, session_id="proc_cancel_window")

    async def _controlled_sleep(delay):
        if delay == runner._completion_notification_batch_window:
            sleep_entered.set()
            await release_sleep.wait()
            return
        await real_sleep(delay)

    async def _exercise():
        pending = asyncio.create_task(
            runner._enqueue_process_completion_notification("completion", event)
        )
        await sleep_entered.wait()
        flush_task = next(iter(runner._completion_notification_batch_tasks.values()))
        assert flush_task in runner._background_tasks

        await runner._cancel_process_completion_batch_tasks()

        assert await asyncio.wait_for(pending, timeout=1.0) is False
        assert flush_task.cancelled()
        assert flush_task not in runner._background_tasks
        assert runner._completion_notification_batches == {}
        assert runner._completion_notification_batch_tasks == {}

    with patch("gateway.run.asyncio.sleep", new=_controlled_sleep):
        asyncio.run(_exercise())
    adapter.handle_message.assert_not_awaited()


def test_shutdown_cancels_blocked_batch_delivery_and_keeps_it_retryable():
    delivery_entered = asyncio.Event()

    async def _blocked_delivery(_event):
        delivery_entered.set()
        await asyncio.Event().wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_blocked_delivery))
    runner = _runner(adapter)
    runner._completion_notification_batch_window = 0
    event = _completion_event(started_at=1.0, session_id="proc_cancel_delivery")

    async def _exercise():
        pending = asyncio.create_task(
            runner._enqueue_process_completion_notification("completion", event)
        )
        await delivery_entered.wait()
        flush_task = next(iter(runner._completion_notification_batch_flush_tasks))

        await runner._cancel_process_completion_batch_tasks()

        assert await asyncio.wait_for(pending, timeout=1.0) is False
        assert flush_task.cancelled()
        assert runner._completion_delivery_identity(event) not in runner._completion_deliveries_inflight
        assert runner._completion_delivery_identity(event) not in runner._completion_deliveries_delivered
        assert runner._completion_notification_batches == {}
        assert runner._completion_notification_batch_tasks == {}

    asyncio.run(_exercise())
    adapter.handle_message.assert_awaited_once()


def test_completion_enqueue_stays_retryable_after_shutdown_starts():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _exercise():
        await runner._cancel_process_completion_batch_tasks()
        return await runner._enqueue_process_completion_notification(
            "completion",
            _completion_event(started_at=1.0, session_id="proc_after_shutdown"),
        )

    assert asyncio.run(_exercise()) is False
    assert runner._completion_notification_batches == {}
    assert runner._completion_notification_batch_tasks == {}
    adapter.handle_message.assert_not_awaited()


def test_successful_batch_releases_all_lifecycle_task_references():
    adapter = SimpleNamespace(handle_message=AsyncMock(return_value=None))
    runner = _runner(adapter)
    runner._completion_notification_batch_window = 0

    async def _exercise():
        result = await runner._enqueue_process_completion_notification(
            "completion",
            _completion_event(started_at=1.0, session_id="proc_success_cleanup"),
        )
        await asyncio.sleep(0)
        return result

    assert asyncio.run(_exercise()) is True
    assert runner._completion_notification_batch_tasks == {}
    assert runner._completion_notification_batch_flush_tasks == set()
    assert runner._background_tasks == set()


def test_shutdown_cancels_overlapping_flushes_for_same_route():
    delivery_entered = asyncio.Event()

    async def _blocked_delivery(_event):
        delivery_entered.set()
        await asyncio.Event().wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_blocked_delivery))
    runner = _runner(adapter)
    runner._completion_notification_batch_window = 0
    first_event = _completion_event(started_at=1.0, session_id="proc_old_flush")
    second_event = _completion_event(started_at=2.0, session_id="proc_new_flush")

    async def _exercise():
        first = asyncio.create_task(
            runner._enqueue_process_completion_notification("first", first_event)
        )
        await delivery_entered.wait()

        # The first task has detached from the route index while blocked in
        # adapter delivery.  A new completion for the same route must create a
        # second flush, and shutdown must still own and cancel both tasks.
        assert runner._completion_notification_batch_tasks == {}
        runner._completion_notification_batch_window = 3600
        second = asyncio.create_task(
            runner._enqueue_process_completion_notification("second", second_event)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        flush_tasks = set(runner._completion_notification_batch_flush_tasks)
        assert len(flush_tasks) == 2

        await runner._cancel_process_completion_batch_tasks()

        assert await asyncio.gather(first, second) == [False, False]
        assert all(task.cancelled() for task in flush_tasks)
        assert runner._completion_notification_batches == {}
        assert runner._completion_notification_batch_tasks == {}
        assert runner._completion_notification_batch_flush_tasks == set()
        assert runner._background_tasks == set()

    asyncio.run(_exercise())
    adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Async-delegation same-tick coalescing (#70300)
# ---------------------------------------------------------------------------


def _distinct_async_event(delegation_id, session_key="agent:main:telegram:dm:12345:678"):
    event = _async_event(delegation_id)
    event["session_key"] = session_key
    event["summary"] = f"Result for {delegation_id}"
    return event


def test_same_tick_async_batch_coalesces_into_one_turn_and_acks_all_rows(
    monkeypatch, isolated_registry,
):
    """Three same-session async completions in one drain -> one synthetic turn.

    All three durable delegation rows must be honestly acknowledged only
    after the single consolidated injection was accepted by the adapter.
    """
    from tools import async_delegation

    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    events = [_distinct_async_event(f"deleg_batch_{i}") for i in range(3)]
    for event in events:
        _persist_pending_completion(event)
        isolated.put(dict(event))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert "3 background subagent delegations" in delivered.text
    for i in range(3):
        assert f"Result for deleg_batch_{i}" in delivered.text
    for event in events:
        row = async_delegation.get_durable_delegation(event["delegation_id"])
        assert row is not None
        assert row["delivery_state"] == "delivered"
    assert isolated.empty()


def test_same_tick_async_events_for_different_sessions_do_not_coalesce(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_distinct_async_event("deleg_route_a"))
    isolated.put(_distinct_async_event(
        "deleg_route_b", session_key="agent:main:telegram:dm:99999:678",
    ))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert adapter.handle_message.await_count == 2
    texts = [call.args[0].text for call in adapter.handle_message.await_args_list]
    assert not any("background subagent delegations" in text for text in texts)
    assert any("deleg_route_a" in text for text in texts)
    assert any("deleg_route_b" in text for text in texts)


def test_single_async_event_latency_and_text_are_unchanged(
    monkeypatch, isolated_registry,
):
    """A lone completion keeps the plain per-event formatter output."""
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_distinct_async_event("deleg_single"))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert "background subagent delegations" not in delivered.text
    assert "deleg_single" in delivered.text


def test_failed_coalesced_async_batch_releases_claims_and_retries(
    monkeypatch, isolated_registry,
):
    """A rejected consolidated injection leaves every durable row pending."""
    from tools import async_delegation

    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    events = [_distinct_async_event(f"deleg_retry_{i}") for i in range(2)]
    for event in events:
        _persist_pending_completion(event)
        isolated.put(dict(event))

    adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=[RuntimeError("temporary"), None])
    )
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    # First tick fails as one batch, second tick delivers the same batch.
    assert adapter.handle_message.await_count == 2
    for event in events:
        row = async_delegation.get_durable_delegation(event["delegation_id"])
        assert row is not None
        assert row["delivery_state"] == "delivered"
    assert isolated.empty()


def test_sibling_claimed_by_other_consumer_is_not_double_delivered(
    monkeypatch, isolated_registry,
):
    """A sibling owned elsewhere is excluded from the consolidated turn."""
    from tools import async_delegation

    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    events = [_distinct_async_event(f"deleg_owned_{i}") for i in range(2)]
    for event in events:
        _persist_pending_completion(event)
        isolated.put(dict(event))
    # Simulate another live consumer holding the second row's claim.
    assert async_delegation.claim_completion_delivery(
        events[1]["delegation_id"], "other-consumer:claim",
    )

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert "Result for deleg_owned_0" in delivered.text
    assert "Result for deleg_owned_1" not in delivered.text
    row = async_delegation.get_durable_delegation(events[1]["delegation_id"])
    assert row["delivery_state"] == "pending"
