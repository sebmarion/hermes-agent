"""Tests for notify_on_complete background process feature.

Covers:
  - ProcessSession.notify_on_complete field
  - ProcessRegistry.completion_queue population on _move_to_finished()
  - Checkpoint persistence of notify_on_complete
  - Terminal tool schema includes notify_on_complete
  - Terminal tool handler passes notify_on_complete through
"""

import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time
import pytest
from unittest.mock import MagicMock, patch

from tools.process_registry import (
    ProcessRegistry,
    ProcessSession,
)


def _notification_runtime_worker(
    outbox_path: str,
    session_id: str,
    connection,
) -> None:
    """Drive one independent notification runtime from a parent barrier."""
    from tools import process_registry as process_registry_module

    process_registry_module.NOTIFICATIONS_PATH = Path(outbox_path)
    worker_registry = ProcessRegistry()
    worker_registry._write_checkpoint = lambda: True
    with worker_registry._completion_outbox_lock:
        worker_registry._ensure_completion_outbox_loaded_locked()
    connection.send("ready")
    command = connection.recv()
    if command == "finish":
        session = _make_session(
            sid=session_id,
            notify_on_complete=True,
            exited=True,
            exit_code=0,
            output=session_id,
        )
        worker_registry._running[session.id] = session
        connection.send(worker_registry._move_to_finished(session))
    elif isinstance(command, dict) and command.get("action") == "ack":
        connection.send(
            worker_registry.mark_completion_consumed(command["event"])
        )
    connection.close()


def _notification_crash_before_replace(
    outbox_path: str,
    session_id: str,
) -> None:
    from tools import durable_state
    from tools import process_registry as process_registry_module

    process_registry_module.NOTIFICATIONS_PATH = Path(outbox_path)
    worker_registry = ProcessRegistry()
    worker_registry._write_checkpoint = lambda: True
    session = _make_session(
        sid=session_id,
        notify_on_complete=True,
        exited=True,
        exit_code=0,
        output="crash",
    )
    worker_registry._running[session.id] = session
    durable_state.os.replace = lambda *_args, **_kwargs: os._exit(91)
    worker_registry._move_to_finished(session)
    os._exit(92)


@pytest.fixture()
def registry(monkeypatch, tmp_path):
    """Create a fresh ProcessRegistry."""
    from tools import process_registry as process_registry_module

    monkeypatch.setattr(
        process_registry_module,
        "NOTIFICATIONS_PATH",
        tmp_path / "process_notifications.json",
    )
    return ProcessRegistry()


def _make_session(
    sid="proc_test_notify",
    command="echo hello",
    task_id="t1",
    exited=False,
    exit_code=None,
    output="",
    notify_on_complete=False,
) -> ProcessSession:
    s = ProcessSession(
        id=sid,
        command=command,
        task_id=task_id,
        started_at=time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer=output,
        notify_on_complete=notify_on_complete,
    )
    return s


# =========================================================================
# ProcessSession field
# =========================================================================

class TestProcessSessionField:
    def test_default_false(self):
        s = ProcessSession(id="proc_1", command="echo hi")
        assert s.notify_on_complete is False

    def test_set_true(self):
        s = ProcessSession(id="proc_1", command="echo hi", notify_on_complete=True)
        assert s.notify_on_complete is True


# =========================================================================
# Completion queue
# =========================================================================

class TestCompletionQueue:
    def test_queue_exists(self, registry):
        assert hasattr(registry, "completion_queue")
        assert registry.completion_queue.empty()

    def test_move_to_finished_no_notify(self, registry):
        """Processes without notify_on_complete don't enqueue."""
        s = _make_session(notify_on_complete=False, output="done")
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)
        assert registry.completion_queue.empty()

    def test_move_to_finished_with_notify(self, registry):
        """Processes with notify_on_complete push to queue."""
        s = _make_session(
            notify_on_complete=True,
            output="build succeeded",
            exit_code=0,
        )
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        assert not registry.completion_queue.empty()
        completion = registry.completion_queue.get_nowait()
        assert completion["session_id"] == s.id
        assert completion["command"] == "echo hello"
        assert completion["exit_code"] == 0
        assert completion["completion_reason"] == "exited"
        assert completion["termination_source"] == ""
        assert "build succeeded" in completion["output"]

    def test_move_to_finished_nonzero_exit(self, registry):
        """Nonzero exit codes are captured correctly."""
        s = _make_session(
            notify_on_complete=True,
            output="FAILED",
            exit_code=1,
        )
        s.exited = True
        s.exit_code = 1
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        completion = registry.completion_queue.get_nowait()
        assert completion["exit_code"] == 1
        assert "FAILED" in completion["output"]

    def test_move_to_finished_idempotent_no_duplicate(self, registry):
        """Calling _move_to_finished twice must NOT enqueue two notifications.

        Regression test: kill_process() and the reader thread can both call
        _move_to_finished() for the same session, producing duplicate
        [SYSTEM: Background process ...] messages.
        """
        s = _make_session(notify_on_complete=True, output="done", exit_code=-15)
        s.exited = True
        s.exit_code = -15
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)  # first call — should enqueue
            s.exit_code = 143  # reader thread updates exit code
            registry._move_to_finished(s)  # second call — should be no-op

        assert registry.completion_queue.qsize() == 1
        completion = registry.completion_queue.get_nowait()
        assert completion["exit_code"] == -15  # from the first (kill) call

    def test_kill_process_sets_completion_reason_and_source(self, registry):
        s = _make_session(notify_on_complete=True, output="stopping")
        s.process = MagicMock()
        s.process.pid = 4242
        registry._running[s.id] = s

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def children(self, recursive=False):
                return []

            def terminate(self):
                pass

        import psutil as _psutil

        with patch.object(_psutil, "Process", side_effect=lambda pid: FakeProcess(pid)), \
             patch.object(registry, "_write_checkpoint"):
            result = registry.kill_process(s.id)

        assert result["status"] == "killed"
        assert result["completion_reason"] == "killed"
        assert result["termination_source"] == "process.kill"
        completion = registry.completion_queue.get_nowait()
        assert completion["completion_reason"] == "killed"
        assert completion["termination_source"] == "process.kill"

    def test_output_truncated_to_2000(self, registry):
        """Long output is truncated to last 2000 chars."""
        long_output = "x" * 5000
        s = _make_session(
            notify_on_complete=True,
            output=long_output,
        )
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        completion = registry.completion_queue.get_nowait()
        assert len(completion["output"]) == 2000

    def test_multiple_completions_queued(self, registry):
        """Multiple notify processes all push to the same queue."""
        for i in range(3):
            s = _make_session(
                sid=f"proc_{i}",
                notify_on_complete=True,
                output=f"output_{i}",
            )
            s.exited = True
            s.exit_code = 0
            registry._running[s.id] = s
            with patch.object(registry, "_write_checkpoint"):
                registry._move_to_finished(s)

        completions = []
        while not registry.completion_queue.empty():
            completions.append(registry.completion_queue.get_nowait())
        assert len(completions) == 3
        ids = {c["session_id"] for c in completions}
        assert ids == {"proc_0", "proc_1", "proc_2"}

    def test_move_to_finished_stays_release_visible_until_outbox_is_durable(
        self, registry
    ):
        session = _make_session(
            sid="proc_release_barrier",
            notify_on_complete=True,
            output="complete",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        writer_entered = threading.Event()
        allow_writer = threading.Event()

        def blocked_writer():
            writer_entered.set()
            assert allow_writer.wait(2)

        with patch.object(
            registry,
            "_write_completion_outbox_locked",
            side_effect=blocked_writer,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            worker = threading.Thread(
                target=registry._move_to_finished,
                args=(session,),
            )
            worker.start()
            assert writer_entered.wait(2)
            with registry._lock:
                assert session.id in registry._running
                assert session.id in registry._finalizing
                assert session.id not in registry._finished
            allow_writer.set()
            worker.join(timeout=2)

        assert not worker.is_alive()
        assert session.id in registry._finished
        assert session.id not in registry._running
        assert session.id not in registry._finalizing

    def test_outbox_is_durable_before_queue_publication(self, registry, tmp_path):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_durable_first",
            notify_on_complete=True,
            output="complete",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session

        class InspectingQueue:
            def __init__(self):
                self.events = []

            def put(self, event):
                durable = json.loads(outbox_path.read_text(encoding="utf-8"))
                assert durable["events"][event["event_id"]]["delivered"] is False
                self.events.append(event)

        queue = InspectingQueue()
        registry.completion_queue = queue
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True

        assert len(queue.events) == 1
        assert queue.events[0]["event_id"] == (
            "process:proc_durable_first:completion"
        )

    def test_outbox_persist_failure_keeps_process_release_visible(self, registry):
        session = _make_session(
            sid="proc_persist_failure",
            notify_on_complete=True,
            output="complete",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session

        with patch.object(
            registry,
            "_write_completion_outbox_locked",
            side_effect=OSError("disk full"),
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is False

        assert session.id in registry._running
        assert session.id not in registry._finished
        assert session.id not in registry._finalizing
        assert registry.completion_queue.empty()
        snapshot = registry.completion_activity_snapshot()
        assert snapshot["running_processes"] == 1

    def test_reconcile_retries_durable_finalization_after_transient_failure(
        self, registry
    ):
        session = _make_session(
            sid="proc_transient_finalize_failure",
            notify_on_complete=True,
            output="complete",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session

        with patch.object(
            registry,
            "_write_checkpoint",
            side_effect=[False, True],
        ):
            assert registry._move_to_finished(session) is False
            assert session.id in registry._running

            registry._reconcile_local_exit(session)

        assert session.id not in registry._running
        assert session.id in registry._finished
        assert registry.completion_queue.qsize() == 1

    def test_concurrent_move_to_finished_creates_one_durable_event(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_concurrent_finish",
            notify_on_complete=True,
            output="complete",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        barrier = threading.Barrier(3)

        def finish():
            barrier.wait()
            registry._move_to_finished(session)

        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            workers = [threading.Thread(target=finish) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=2)

        durable = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert list(durable["events"]) == [
            "process:proc_concurrent_finish:completion"
        ]
        assert registry.completion_queue.qsize() == 1


# =========================================================================
# Checkpoint persistence
# =========================================================================

class TestCheckpointNotify:
    def test_checkpoint_includes_notify(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session(notify_on_complete=True)
            s.pid = os.getpid()
            registry._running[s.id] = s
            assert registry._write_checkpoint() is True

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["notify_on_complete"] is True

    def test_checkpoint_without_notify(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session(notify_on_complete=False)
            s.pid = os.getpid()
            registry._running[s.id] = s
            assert registry._write_checkpoint() is True

            data = json.loads((tmp_path / "procs.json").read_text())
            assert data[0]["notify_on_complete"] is False

    def test_recover_preserves_notify(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "notify_on_complete": True,
            "host_start_time": ProcessRegistry._safe_host_start_time(os.getpid()),
            "process_start_token": ProcessRegistry._safe_host_start_token(os.getpid()),
        }]))
        checkpoint.chmod(0o600)
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            s = registry.get("proc_live")
            assert s.notify_on_complete is True

    def test_recover_requeues_notify_watchers(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "session_key": "sk1",
            "watcher_platform": "telegram",
            "watcher_chat_id": "123",
            "watcher_user_id": "u123",
            "watcher_user_name": "alice",
            "watcher_thread_id": "42",
            "watcher_interval": 5,
            "notify_on_complete": True,
            "host_start_time": ProcessRegistry._safe_host_start_time(os.getpid()),
            "process_start_token": ProcessRegistry._safe_host_start_token(os.getpid()),
        }]))
        checkpoint.chmod(0o600)
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            assert len(registry.pending_watchers) == 1
            assert registry.pending_watchers[0]["notify_on_complete"] is True
            assert registry.pending_watchers[0]["user_id"] == "u123"
            assert registry.pending_watchers[0]["user_name"] == "alice"

    def test_recover_defaults_false(self, registry, tmp_path):
        """Old checkpoint entries without the field default to False."""
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "host_start_time": ProcessRegistry._safe_host_start_time(os.getpid()),
            "process_start_token": ProcessRegistry._safe_host_start_token(os.getpid()),
        }]))
        checkpoint.chmod(0o600)
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            s = registry.get("proc_live")
            assert s.notify_on_complete is False


# =========================================================================
# Terminal tool schema
# =========================================================================

class TestTerminalSchema:
    def test_schema_has_notify_on_complete(self):
        from tools.terminal_tool import TERMINAL_SCHEMA
        props = TERMINAL_SCHEMA["parameters"]["properties"]
        assert "notify_on_complete" in props
        assert props["notify_on_complete"]["type"] == "boolean"
        assert props["notify_on_complete"]["default"] is False

    def test_handler_passes_notify(self):
        """_handle_terminal passes notify_on_complete to terminal_tool."""
        from tools.terminal_tool import _handle_terminal
        with patch("tools.terminal_tool.terminal_tool", return_value='{"ok":true}') as mock_tt:
            _handle_terminal(
                {"command": "echo hi", "background": True, "notify_on_complete": True},
                task_id="t1",
            )
            _, kwargs = mock_tt.call_args
            assert kwargs["notify_on_complete"] is True


# =========================================================================
# Code execution blocked params
# =========================================================================

class TestCodeExecutionBlocked:
    def test_notify_on_complete_blocked_in_sandbox(self):
        from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS
        assert "notify_on_complete" in _TERMINAL_BLOCKED_PARAMS


# =========================================================================
# Completion consumed suppression
# =========================================================================

class TestCompletionConsumed:
    """Test that wait/log consume completion notifications while poll stays read-only."""

    def test_wait_marks_completion_consumed(self, registry):
        """wait() returning exited status marks session as consumed."""
        s = _make_session(sid="proc_wait", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        # Notification is in the queue
        assert not registry.completion_queue.empty()
        assert not registry.is_completion_consumed("proc_wait")

        # Agent calls wait() — gets the result directly
        result = registry.wait("proc_wait", timeout=1)
        assert result["status"] == "exited"

        # Now the completion is marked as consumed
        assert registry.is_completion_consumed("proc_wait")

    def test_poll_does_not_mark_completion_consumed(self, registry):
        """poll() is a read-only status check and must not suppress notify_on_complete."""
        s = _make_session(sid="proc_poll", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._finished[s.id] = s

        result = registry.poll("proc_poll")
        assert result["status"] == "exited"
        assert not registry.is_completion_consumed("proc_poll")

    def test_log_marks_completion_consumed(self, registry):
        """read_log() on exited session marks as consumed."""
        s = _make_session(sid="proc_log", notify_on_complete=True, output="line1\nline2")
        s.exited = True
        s.exit_code = 0
        registry._finished[s.id] = s

        result = registry.read_log("proc_log")
        assert result["status"] == "exited"
        assert registry.is_completion_consumed("proc_log")

    def test_running_process_not_consumed(self, registry):
        """poll() on a still-running process does not mark as consumed."""
        s = _make_session(sid="proc_running", notify_on_complete=True, output="partial")
        registry._running[s.id] = s

        result = registry.poll("proc_running")
        assert result["status"] == "running"
        assert not registry.is_completion_consumed("proc_running")

    def test_poll_marks_poll_observed_for_cli_drain(self, registry):
        """poll() on an exited process records _poll_observed so the CLI drain
        dedups (the agent already saw the exit inline) without marking the
        session _completion_consumed (which would suppress the gateway watcher)."""
        s = _make_session(sid="proc_pobs", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        # Completion is queued, nothing consumed/observed yet.
        assert not registry.completion_queue.empty()
        assert "proc_pobs" not in registry._poll_observed
        assert not registry.is_completion_consumed("proc_pobs")

        # Agent polls inline — read-only, so NOT _completion_consumed, but the
        # exit was observed so the CLI drain must skip the queued completion.
        assert registry.poll("proc_pobs")["status"] == "exited"
        assert "proc_pobs" in registry._poll_observed
        assert not registry.is_completion_consumed("proc_pobs")

        # CLI drain skips it → no duplicate [SYSTEM: ...] injection (#8228).
        drained = registry.drain_notifications()
        assert drained == []

    def test_poll_observed_does_not_suppress_gateway_watcher(self, registry):
        """The gateway/tui watcher gate (is_completion_consumed) must stay False
        after a read-only poll, so the autonomous delivery turn still fires
        even though the CLI drain was deduped (#10156)."""
        s = _make_session(sid="proc_gw", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._finished[s.id] = s

        registry.poll("proc_gw")
        # CLI-side dedup signal present...
        assert "proc_gw" in registry._poll_observed
        # ...but the gateway watcher gate is untouched, so it still delivers.
        assert not registry.is_completion_consumed("proc_gw")

    def test_running_poll_does_not_mark_poll_observed(self, registry):
        """poll() on a still-running process must not record _poll_observed."""
        s = _make_session(sid="proc_run2", notify_on_complete=True, output="partial")
        registry._running[s.id] = s

        registry.poll("proc_run2")
        assert "proc_run2" not in registry._poll_observed

    def test_wait_and_log_still_skip_cli_drain(self, registry):
        """wait()/read_log() consume the output, so the CLI drain skips their
        completions via _completion_consumed (the original #8228 contract)."""
        for sid, action in (("proc_w", "wait"), ("proc_l", "log")):
            s = _make_session(sid=sid, notify_on_complete=True, output="done")
            s.exited = True
            s.exit_code = 0
            registry._running[s.id] = s
            with patch.object(registry, "_write_checkpoint"):
                registry._move_to_finished(s)
            if action == "wait":
                registry.wait(sid, timeout=1)
            else:
                registry.read_log(sid)
            assert registry.is_completion_consumed(sid)
        assert registry.drain_notifications() == []

    def test_mark_completion_consumed_is_public_durable_and_idempotent(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_public_ack",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            event = registry.completion_queue.get_nowait()
            assert registry.mark_completion_consumed(event) is True
            assert registry.mark_completion_consumed(event) is True

            replacement = ProcessRegistry()
            assert replacement.recover_completion_notifications() == 0

        durable = json.loads(outbox_path.read_text(encoding="utf-8"))
        record = durable["events"][event["event_id"]]
        assert record["delivered"] is True
        assert registry.is_completion_consumed(session.id) is True

    @pytest.mark.parametrize("consumer", ["wait", "read_log"])
    def test_wait_and_read_log_ack_durable_completion(
        self, registry, tmp_path, consumer
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / f"{consumer}-notifications.json"
        session = _make_session(
            sid=f"proc_{consumer}_durable",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            result = getattr(registry, consumer)(session.id)

        assert result["status"] == "exited"
        assert registry.completion_activity_snapshot()[
            "durable_undelivered_completions"
        ] == 0


class TestCompletionRecovery:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file locks")
    def test_two_runtime_add_add_preserves_both_events(self, tmp_path):
        outbox = tmp_path / "process_notifications.json"
        context = multiprocessing.get_context("spawn")
        parent_a, child_a = context.Pipe()
        parent_b, child_b = context.Pipe()
        workers = [
            context.Process(
                target=_notification_runtime_worker,
                args=(os.fspath(outbox), "proc_runtime_a", child_a),
            ),
            context.Process(
                target=_notification_runtime_worker,
                args=(os.fspath(outbox), "proc_runtime_b", child_b),
            ),
        ]
        for worker in workers:
            worker.start()
        assert parent_a.recv() == "ready"
        assert parent_b.recv() == "ready"
        parent_a.send("finish")
        parent_b.send("finish")
        assert parent_a.recv() is True
        assert parent_b.recv() is True
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0

        durable = json.loads(outbox.read_text(encoding="utf-8"))
        assert set(durable["events"]) == {
            "process:proc_runtime_a:completion",
            "process:proc_runtime_b:completion",
        }
        assert (
            outbox.with_name(f".{outbox.name}.lock").stat().st_mode & 0o777
        ) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file locks")
    def test_two_runtime_add_and_ack_are_monotonic(self, registry, tmp_path):
        from tools import process_registry as process_registry_module

        outbox = tmp_path / "process_notifications.json"
        original = _make_session(
            sid="proc_original",
            notify_on_complete=True,
            exited=True,
            exit_code=0,
            output="original",
        )
        registry._running[original.id] = original
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(original) is True
            event = registry.completion_queue.get_nowait()

        context = multiprocessing.get_context("spawn")
        parent_add, child_add = context.Pipe()
        parent_ack, child_ack = context.Pipe()
        add_worker = context.Process(
            target=_notification_runtime_worker,
            args=(os.fspath(outbox), "proc_added", child_add),
        )
        ack_worker = context.Process(
            target=_notification_runtime_worker,
            args=(os.fspath(outbox), "unused", child_ack),
        )
        add_worker.start()
        ack_worker.start()
        assert parent_add.recv() == "ready"
        assert parent_ack.recv() == "ready"
        parent_add.send("finish")
        parent_ack.send({"action": "ack", "event": event})
        assert parent_add.recv() is True
        assert parent_ack.recv() is True
        add_worker.join(timeout=5)
        ack_worker.join(timeout=5)
        assert add_worker.exitcode == 0
        assert ack_worker.exitcode == 0

        durable = json.loads(outbox.read_text(encoding="utf-8"))["events"]
        assert set(durable) == {
            event["event_id"],
            "process:proc_added:completion",
        }
        assert durable[event["event_id"]]["delivered"] is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX crash injection")
    def test_crash_before_notification_replace_preserves_prior_events(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox = tmp_path / "process_notifications.json"
        baseline = _make_session(
            sid="proc_baseline",
            notify_on_complete=True,
            exited=True,
            exit_code=0,
            output="baseline",
        )
        registry._running[baseline.id] = baseline
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(baseline) is True
        before = outbox.read_bytes()

        context = multiprocessing.get_context("spawn")
        crashing = context.Process(
            target=_notification_crash_before_replace,
            args=(os.fspath(outbox), "proc_crashing"),
        )
        crashing.start()
        crashing.join(timeout=10)
        assert crashing.exitcode == 91
        assert outbox.read_bytes() == before

    def test_recovery_does_not_duplicate_live_event_already_queued(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_live_already_queued",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            assert registry.completion_queue.qsize() == 1

            assert registry.recover_completion_notifications() == 0
            assert registry.completion_queue.qsize() == 1

    def test_failed_durable_ack_requeues_exactly_once(self, registry):
        event = {
            "type": "completion",
            "event_id": "process:proc_ack_failure:completion",
            "session_id": "proc_ack_failure",
        }
        with patch.object(registry, "mark_completion_consumed", return_value=False):
            assert registry.finish_notification_delivery(event, committed=True) is False

        assert registry.completion_queue.qsize() == 1
        assert registry.completion_queue.get_nowait() == event

    def test_failed_async_ack_requeues_instead_of_reporting_success(self, registry):
        event = {
            "type": "async_delegation",
            "delegation_id": "deleg_ack_failure",
        }
        with patch(
            "tools.async_delegation.mark_async_delegation_delivered",
            return_value=False,
        ):
            assert registry.finish_notification_delivery(event, committed=True) is False

        assert registry.completion_queue.qsize() == 1
        assert registry.completion_queue.get_nowait() == event

    def test_legacy_async_drain_requeues_when_ack_is_not_durable(self, registry):
        event = {
            "type": "async_delegation",
            "delegation_id": "deleg_drain_ack_failure",
            "goal": "retry delivery",
            "status": "completed",
            "summary": "done",
        }
        registry.completion_queue.put(event)
        with patch(
            "tools.async_delegation.mark_async_delegation_delivered",
            return_value=False,
        ):
            drained = registry.drain_notifications()

        assert len(drained) == 1
        assert registry.completion_queue.qsize() == 1
        assert registry.completion_queue.get_nowait() == event

    def test_committed_automatic_delivery_acks_before_restart_replay(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_automatic_delivery",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            event = registry.completion_queue.get_nowait()

            assert registry.finish_notification_delivery(event, committed=True) is True

            replacement = ProcessRegistry()
            assert replacement.recover_completion_notifications() == 0

    def test_uncommitted_automatic_delivery_requeues_and_replays_after_restart(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_automatic_retry",
            notify_on_complete=True,
            output="retry",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            event = registry.completion_queue.get_nowait()

            assert registry.finish_notification_delivery(event, committed=False) is False
            assert registry.completion_queue.get_nowait() == event

            replacement = ProcessRegistry()
            assert replacement.recover_completion_notifications() == 1

    def test_recovery_replays_unacked_completion_once_per_registry(
        self, registry, tmp_path
    ):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_replay",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            original = registry.completion_queue.get_nowait()

            replacement = ProcessRegistry()
            assert replacement.recover_completion_notifications() == 1
            assert replacement.recover_completion_notifications() == 0
            replayed = replacement.completion_queue.get_nowait()

        assert replayed == original
        assert replayed["event_id"] == "process:proc_replay:completion"

    def test_recovery_never_replays_delivered_completion(self, registry, tmp_path):
        from tools import process_registry as process_registry_module

        outbox_path = tmp_path / "process_notifications.json"
        session = _make_session(
            sid="proc_delivered",
            notify_on_complete=True,
            output="done",
            exit_code=0,
        )
        session.exited = True
        registry._running[session.id] = session
        with patch.object(
            process_registry_module,
            "NOTIFICATIONS_PATH",
            outbox_path,
        ), patch.object(registry, "_write_checkpoint", return_value=True):
            assert registry._move_to_finished(session) is True
            event = registry.completion_queue.get_nowait()
            assert registry.mark_completion_consumed(event) is True

            replacement = ProcessRegistry()
            assert replacement.recover_completion_notifications() == 0
            assert replacement.completion_queue.empty()


# ---------------------------------------------------------------------------
# Silent-background-process hint
#
# background=True without notify_on_complete=True OR watch_patterns runs
# the process silently — the agent has no way to learn it finished short
# of calling process(action="poll") explicitly. The tool result must
# include a "hint" field that nudges the agent toward
# notify_on_complete=True for bounded tasks. May 2026 PR #31231 incident:
# bg CI poller exited green, agent never noticed, user had to surface it.
# ---------------------------------------------------------------------------


def _silent_bg_base_config(tmp_path):
    return {
        "env_type": "local",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "cwd": str(tmp_path),
        "timeout": 30,
    }


def _silent_bg_harness(monkeypatch, tmp_path):
    """Common test fixture: patch enough of terminal_tool to spawn a fake
    background process and capture the JSON result the agent sees."""
    import tools.terminal_tool as terminal_tool_module
    from tools import process_registry as process_registry_module
    from types import SimpleNamespace

    config = _silent_bg_base_config(tmp_path)
    dummy_env = SimpleNamespace(env={})

    def fake_spawn_local(**kwargs):
        return SimpleNamespace(
            id="proc_silent_test",
            pid=4242,
            notify_on_complete=False,
            watcher_platform="",
            watcher_chat_id="",
            watcher_user_id="",
            watcher_user_name="",
            watcher_thread_id="",
            watcher_message_id="",
            watcher_interval=0,
        )

    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool_module, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True})
    monkeypatch.setattr(process_registry_module.process_registry, "spawn_local", fake_spawn_local)
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", dummy_env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)
    return terminal_tool_module


def test_background_without_notify_emits_silent_process_hint(monkeypatch, tmp_path):
    """The footgun case (May 2026 PR #31231): bg=True alone runs silently
    and the agent has no signal it finished. Tool must nudge."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="while true; do gh pr checks 999; sleep 30; done",
                background=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert result["session_id"] == "proc_silent_test"
    hint = result.get("hint", "")
    assert hint, "Silent background process must include a hint field"
    assert "notify_on_complete" in hint, (
        "Hint must name the corrective flag so the agent can self-correct"
    )
    assert "silent" in hint.lower() or "no way to learn" in hint.lower(), (
        "Hint must explain the failure mode, not just suggest the fix"
    )


def test_background_with_notify_does_not_emit_hint(monkeypatch, tmp_path):
    """The correct shape — bg+notify together — must not nag."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="pytest tests/",
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Correct usage must not emit a hint, got: {result.get('hint')!r}"
    )
    assert result.get("notify_on_complete") is True


def test_background_with_watch_patterns_does_not_emit_hint(monkeypatch, tmp_path):
    """watch_patterns is the other legitimate non-silent shape — also no hint."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="uvicorn app:server --port 8080",
                background=True,
                watch_patterns=["Application startup complete"],
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"watch_patterns shape must not emit a silent-process hint, got: {result.get('hint')!r}"
    )


def test_foreground_command_does_not_emit_hint(monkeypatch, tmp_path):
    """Hint only applies to background processes — foreground returns its
    result synchronously and the agent always sees the outcome."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)

    # Foreground path doesn't go through spawn_local. Patch the local-env
    # exec method to short-circuit to a clean exit so the test doesn't
    # actually shell out.
    from types import SimpleNamespace
    dummy_env = SimpleNamespace(
        env={},
        execute=lambda *a, **kw: {"output": "done", "exit_code": 0, "error": None},
    )
    monkeypatch.setitem(tt._active_environments, "default", dummy_env)

    try:
        result = json.loads(
            tt.terminal_tool(
                command="echo hello",
                background=False,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Foreground commands must not emit the background-silence hint, got: {result.get('hint')!r}"
    )


# ---------------------------------------------------------------------------
# Homebrewed-CI-watcher hint
#
# Background processes whose command looks like a hand-rolled CI poller
# (`gh pr view` / `gh pr checks` combined with jq/awk on stdout) get an
# additional hint pointing at the canonical green-ci-policy snippet. The
# homebrew shape has burned us repeatedly (May 2026 PRs #31329, #31448,
# #31695, #31709, #31745, #32264, #33131) with stdout buffering, jq null
# keys, conclusion-vs-status confusion, and TTY-only banner grepping —
# none of which the canonical snippets suffer from. Fire on every detection;
# false positives are cheap (~one read).
# ---------------------------------------------------------------------------


def test_homebrew_ci_poller_via_statusCheckRollup_emits_hint(monkeypatch, tmp_path):
    """The canonical anti-pattern: jq pipeline parsing statusCheckRollup
    JSON. Tool must point the agent at the green-ci-policy skill snippet."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command=(
                    "PR=12345; while true; do "
                    "status=$(gh pr view $PR --json statusCheckRollup "
                    "--jq '[.statusCheckRollup[] | .conclusion] "
                    "| group_by(.) | map({k:.[0],v:length}) | from_entries'); "
                    "echo \"$status\"; sleep 30; done"
                ),
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    hint = result.get("hint", "")
    assert hint, "Homebrew CI poller must emit a hint pointing at green-ci-policy"
    assert "green-ci-policy" in hint, (
        "Hint must name the canonical skill file so the agent can find the verbatim snippets"
    )
    # Naming exit-code-driven OR column-2 in the hint is what makes it actionable.
    assert "exit" in hint.lower() or "column-2" in hint.lower() or "tab" in hint.lower(), (
        "Hint must point at the canonical alternatives (exit-code or column-2)"
    )


def test_homebrew_ci_poller_via_gh_pr_checks_piped_to_jq_emits_hint(monkeypatch, tmp_path):
    """`gh pr checks` doesn't emit JSON, so piping it to jq is a confused-
    intent anti-pattern that produces silent failures (jq fails, loop
    keeps spinning with empty data)."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command=(
                    "PR=99; while true; do "
                    "gh pr checks $PR | jq -R 'split(\"\\t\")[1]'; "
                    "sleep 30; done"
                ),
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    hint = result.get("hint", "")
    assert hint, "Homebrew `gh pr checks | jq` poller must emit a hint"
    assert "green-ci-policy" in hint


def test_canonical_column2_awk_poller_does_not_emit_homebrew_hint(monkeypatch, tmp_path):
    """The blessed column-2 awk-on-tabs poller from green-ci-policy is the
    PREFERRED pattern for sharded matrices. Must not be flagged as
    homebrew — the gating signal is statusCheckRollup or `gh pr checks
    | jq`, NOT awk on tabs."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command=(
                    "PR=1; while :; do "
                    "out=$(gh pr checks $PR 2>&1); "
                    "pending=$(echo \"$out\" | awk -F\"\\t\" \"\\$2==\\\"pending\\\"\" | wc -l); "
                    "failed=$(echo \"$out\" | awk -F\"\\t\" \"\\$2==\\\"fail\\\"\" | wc -l); "
                    "if [ \"$pending\" -eq 0 ]; then "
                    "[ \"$failed\" -gt 0 ] && exit 1 || exit 0; "
                    "fi; sleep 30; "
                    "done"
                ),
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Canonical column-2 awk poller must not be flagged as homebrew, got: {result.get('hint')!r}"
    )


def test_canonical_gh_pr_checks_exit_code_loop_does_not_emit_hint(monkeypatch, tmp_path):
    """The blessed exit-code-driven snippet from green-ci-policy is exactly
    what we want — no jq, no awk-on-stdout, gates the loop on exit code.
    Must not be flagged as a homebrew anti-pattern."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command=(
                    "PR=1; while :; do "
                    "gh pr checks $PR >/dev/null 2>&1; rc=$?; "
                    "case $rc in 0) exit 0;; 8) sleep 30;; *) exit 1;; esac; "
                    "done"
                ),
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    # No silent-process hint (we have notify_on_complete) AND no
    # homebrew-poller hint (no jq / awk pipeline parsing stdout).
    assert "hint" not in result, (
        f"Canonical exit-code-driven poller must not be flagged as homebrew, got: {result.get('hint')!r}"
    )


def test_non_ci_background_command_does_not_emit_homebrew_hint(monkeypatch, tmp_path):
    """A long-running task that happens to use awk for unrelated reasons
    must not be mistaken for a CI poller — the gating signal is the
    combination of `gh pr ...` AND a stdout parser."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="cat /var/log/syslog | awk '/error/ {print}' > /tmp/errs.log",
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Non-CI command using awk must not be flagged as homebrew CI poller, got: {result.get('hint')!r}"
    )
