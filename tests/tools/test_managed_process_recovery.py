import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

from tools import process_registry as process_registry_module
from tools.process_registry import (
    ManagedProcessRecoveryAmbiguous,
    ManagedProcessRecoveryOutcome,
    ProcessRegistry,
)


def _write_private(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _checkpoint_entry(session_id="proc_live", pid=4242, token="token:4242"):
    return {
        "session_id": session_id,
        "command": "sleep 99",
        "pid": pid,
        "pid_scope": "host",
        "process_start_token": token,
        "started_at": 1.0,
        "notify_on_complete": True,
    }


def _outbox(session_id="proc_done"):
    event_id = f"process:{session_id}:completion"
    return {
        "version": 1,
        "events": {
            event_id: {
                "event_id": event_id,
                "type": "completion",
                "session_id": session_id,
                "created_at": 1.0,
                "delivered": False,
            }
        },
    }


def _real_crash_recovery_worker(
    checkpoint_path: str,
    notifications_path: str,
    boundary: str,
) -> None:
    from pathlib import Path
    import os

    from tools import process_registry as module
    from tools.process_registry import ProcessRegistry

    module.CHECKPOINT_PATH = Path(checkpoint_path)
    module.NOTIFICATIONS_PATH = Path(notifications_path)
    registry = ProcessRegistry()
    registry._safe_host_start_token = lambda pid: f"token:{pid}"
    registry._is_host_pid_alive = lambda pid: pid == 4242
    registry.recover_managed_startup_exact(
        crash_hook=lambda observed: os._exit(73)
        if observed == boundary
        else None
    )


@pytest.fixture
def authorities(tmp_path, monkeypatch):
    checkpoint = tmp_path / "processes.json"
    notifications = tmp_path / "process_notifications.json"
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr(
        process_registry_module, "NOTIFICATIONS_PATH", notifications
    )
    return checkpoint, notifications


@pytest.fixture
def exact_registry(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(
        registry,
        "_safe_host_start_token",
        lambda pid: f"token:{pid}",
    )
    monkeypatch.setattr(registry, "_is_host_pid_alive", lambda pid: pid == 4242)
    return registry


def test_absent_is_exact_and_not_malformed(exact_registry, authorities):
    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.outcome is ManagedProcessRecoveryOutcome.PROVED_ABSENT
    assert receipt.recovered_process_ids == ()
    assert receipt.completion_event_ids == ()
    assert receipt.checkpoint_before_identity is None
    assert receipt.checkpoint_before_sha256 is None


def test_exact_receipt_names_recovered_process_and_queued_event(
    exact_registry, authorities
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())

    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.outcome is ManagedProcessRecoveryOutcome.PROVED_COMPLETE
    assert receipt.recovered_process_ids == ("proc_live",)
    assert receipt.completion_event_ids == (
        "process:proc_done:completion",
    )
    assert receipt.queued_completion_event_ids == receipt.completion_event_ids
    assert receipt.checkpoint_before_sha256
    assert receipt.checkpoint_after_sha256
    assert receipt.registry_epoch.startswith("registry_")
    assert receipt.process_start_token == f"token:{os.getpid()}"
    assert exact_registry.completion_queue.get_nowait()["event_id"] == (
        "process:proc_done:completion"
    )


def test_same_registry_retry_dedupes_and_restart_replays_once(
    exact_registry, authorities
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())
    first = exact_registry.recover_managed_startup_exact()
    second = exact_registry.recover_managed_startup_exact()
    assert first.queued_completion_event_ids == (
        "process:proc_done:completion",
    )
    assert second.queued_completion_event_ids == ()
    assert second.deduped_completion_event_ids == (
        "process:proc_done:completion",
    )
    assert exact_registry.completion_queue.qsize() == 1

    restarted = ProcessRegistry()
    restarted._safe_host_start_token = lambda pid: f"token:{pid}"
    restarted._is_host_pid_alive = lambda pid: pid == 4242
    receipt = restarted.recover_managed_startup_exact()
    assert receipt.registry_epoch != first.registry_epoch
    assert receipt.queued_completion_event_ids == (
        "process:proc_done:completion",
    )
    assert restarted.completion_queue.qsize() == 1


def test_malformed_checkpoint_is_ambiguous_and_preserved(
    exact_registry, authorities
):
    checkpoint, _notifications = authorities
    checkpoint.write_bytes(b"{bad")
    checkpoint.chmod(0o600)
    before = checkpoint.read_bytes()
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()
    assert checkpoint.read_bytes() == before


def test_pid_reuse_is_terminally_classified_not_recovered(
    exact_registry, authorities
):
    checkpoint, _notifications = authorities
    entry = _checkpoint_entry(token="token:old")
    _write_private(checkpoint, [entry])
    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.recovered_process_ids == ()
    assert receipt.record_classifications == (
        ("proc_live", "pid_identity_mismatch"),
    )
    assert exact_registry.get("proc_live") is None
    assert json.loads(checkpoint.read_text()) == []
    durable = json.loads(_notifications.read_text())
    event = next(iter(durable["events"].values()))
    assert event["completion_reason"] == "lost"
    assert event["process_start_token"] == "token:old"
    assert "token:old" not in event["event_id"]
    assert exact_registry.completion_queue.get_nowait()["event_id"] == event[
        "event_id"
    ]


def test_recovered_detached_pid_reuse_kill_never_signals(
    exact_registry, authorities
):
    checkpoint, _notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    exact_registry.recover_managed_startup_exact()
    session = exact_registry._running["proc_live"]
    exact_registry._safe_host_start_token = lambda _pid: "token:reused"
    terminate = Mock()
    exact_registry._terminate_host_pid = terminate

    result = exact_registry.kill_process(session.id)

    assert result["status"] == "already_exited"
    terminate.assert_not_called()


def test_managed_completion_identity_binds_exact_process_generation():
    registry = ProcessRegistry()
    first = process_registry_module.ProcessSession(
        id="proc_same",
        command="one",
        pid=44,
        process_start_token="generation-one",
        notify_on_complete=True,
    )
    second = process_registry_module.ProcessSession(
        id="proc_same",
        command="two",
        pid=44,
        process_start_token="generation-two",
        notify_on_complete=True,
    )
    assert registry._build_completion_record(first)["event_id"] != (
        registry._build_completion_record(second)["event_id"]
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "after_checkpoint_commit",
        "after_registry_publish",
        "after_notification_queue",
    ],
)
def test_crash_boundary_retry_converges_exactly_once(
    exact_registry, authorities, boundary
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())

    def crash(observed):
        if observed == boundary:
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError, match="synthetic"):
        exact_registry.recover_managed_startup_exact(crash_hook=crash)
    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.recovered_process_ids in ((), ("proc_live",))
    assert set(receipt.completion_event_ids) == {
        "process:proc_done:completion"
    }
    assert exact_registry.completion_queue.qsize() == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "after_checkpoint_commit",
        "after_registry_publish",
        "after_notification_queue",
    ],
)
def test_real_process_exit_releases_lock_and_fresh_registry_converges(
    exact_registry, authorities, boundary
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())
    process = multiprocessing.get_context("spawn").Process(
        target=_real_crash_recovery_worker,
        args=(str(checkpoint), str(notifications), boundary),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73

    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.outcome is ManagedProcessRecoveryOutcome.PROVED_COMPLETE
    assert exact_registry.completion_queue.qsize() == 1


def test_real_exit_after_terminal_intent_preserves_row_until_restart(
    exact_registry, authorities
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry(token="token:old")])
    process = multiprocessing.get_context("spawn").Process(
        target=_real_crash_recovery_worker,
        args=(
            str(checkpoint),
            str(notifications),
            "after_terminal_outbox_commit",
        ),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 73
    assert json.loads(checkpoint.read_text())[0]["session_id"] == "proc_live"

    receipt = exact_registry.recover_managed_startup_exact()
    assert receipt.record_classifications == (
        ("proc_live", "pid_identity_mismatch"),
    )
    assert json.loads(checkpoint.read_text()) == []
    assert exact_registry.completion_queue.qsize() == 1


def test_parent_directory_rebind_fails_closed(authorities, monkeypatch):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())
    registry = ProcessRegistry()
    registry._safe_host_start_token = lambda pid: f"token:{pid}"
    registry._is_host_pid_alive = lambda pid: pid == 4242
    parent = checkpoint.parent
    moved = parent.with_name(f"{parent.name}-moved")

    def rebind(stage):
        if stage == "after_checkpoint_commit":
            parent.rename(moved)
            parent.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="parent identity changed"):
        registry.recover_managed_startup_exact(crash_hook=rebind)
    assert (moved / checkpoint.name).exists()


def test_lifecycle_lock_replacement_fails_closed(authorities):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    _write_private(notifications, _outbox())
    registry = ProcessRegistry()
    registry._safe_host_start_token = lambda pid: f"token:{pid}"
    registry._is_host_pid_alive = lambda pid: pid == 4242
    admission = process_registry_module._process_admission_anchor(checkpoint)
    lock_path = admission.with_name(f".{admission.name}.lock")
    displaced = lock_path.with_suffix(".displaced")

    def replace_lock(stage):
        if stage == "after_notification_queue":
            lock_path.rename(displaced)
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)

    with pytest.raises(ValueError, match="lock changed while held"):
        registry.recover_managed_startup_exact(crash_hook=replace_lock)


def test_missing_checkpoint_leaf_with_durable_outbox_is_not_absent(
    exact_registry, authorities
):
    _checkpoint, notifications = authorities
    _write_private(notifications, _outbox())

    receipt = exact_registry.recover_managed_startup_exact()

    assert receipt.outcome is ManagedProcessRecoveryOutcome.PROVED_COMPLETE
    assert receipt.checkpoint_before_identity is None
    assert receipt.completion_event_ids == ("process:proc_done:completion",)


def test_foreign_owner_active_blocks_release_without_adoption(
    exact_registry, authorities, monkeypatch
):
    checkpoint, _notifications = authorities
    entry = _checkpoint_entry()
    entry.update(
        {
            "checkpoint_owner_id": "runtime_foreign",
            "checkpoint_owner_pid": 4242,
            "checkpoint_owner_start_token": "token:4242",
        }
    )
    _write_private(checkpoint, [entry])
    monkeypatch.setattr(
        ProcessRegistry,
        "_safe_host_start_token",
        staticmethod(lambda pid: f"token:{pid}"),
    )
    monkeypatch.setattr(
        ProcessRegistry,
        "_is_host_pid_alive",
        staticmethod(lambda pid: pid == 4242),
    )

    receipt = exact_registry.recover_managed_startup_exact()
    snapshot = exact_registry.completion_activity_snapshot()

    assert receipt.record_classifications == (
        ("proc_live", "foreign_owner_active"),
    )
    assert exact_registry.get("proc_live") is None
    assert snapshot["foreign_owner_active_processes"] == 1
    assert snapshot["running_processes"] == 1


def test_windows_legacy_lifecycle_avoids_strict_held_directory(
    exact_registry, authorities, monkeypatch
):
    entered = []

    class _LegacyLock:
        def __enter__(self):
            entered.append("lock")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(process_registry_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        process_registry_module,
        "platform_neutral_lifecycle_lock",
        lambda _path: _LegacyLock(),
    )
    monkeypatch.setattr(
        process_registry_module,
        "hold_private_authority_directory",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("strict POSIX authority used on Windows")
        ),
    )
    monkeypatch.setattr(
        exact_registry,
        "_spawn_local_admitted",
        lambda *_args, **_kwargs: process_registry_module.ProcessSession(
            "proc_windows", "true"
        ),
    )

    session = exact_registry.spawn_local("true")

    assert session.id == "proc_windows"
    assert entered == ["lock"]


def test_closed_managed_outbox_schema_rejects_unknown_and_bad_delivered_at(
    exact_registry, authorities
):
    _checkpoint, notifications = authorities
    malformed = _outbox()
    row = next(iter(malformed["events"].values()))
    row["unknown"] = True
    _write_private(notifications, malformed)
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()
    row.pop("unknown")
    row["delivered"] = True
    row["delivered_at"] = None
    _write_private(notifications, malformed)
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "x" * 513),
        ("pid", 2**31),
        ("watcher_interval", 86_401),
    ],
)
def test_unbounded_checkpoint_fields_fail_before_authority_mutation(
    exact_registry, authorities, field, value
):
    checkpoint, notifications = authorities
    entry = _checkpoint_entry()
    entry[field] = value
    _write_private(checkpoint, [entry])
    before = checkpoint.read_bytes()

    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()

    assert checkpoint.read_bytes() == before
    assert not notifications.exists()
    assert exact_registry._running == {}


def test_terminal_outbox_capacity_failure_retains_checkpoint(
    exact_registry, authorities, monkeypatch
):
    checkpoint, notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry(token="token:old")])
    monkeypatch.setattr(
        process_registry_module, "MAX_COMPLETION_OUTBOX_RECORDS", 0
    )

    with pytest.raises(
        ManagedProcessRecoveryAmbiguous, match="capacity is exhausted"
    ):
        exact_registry.recover_managed_startup_exact()

    assert json.loads(checkpoint.read_text())[0]["session_id"] == "proc_live"
    assert not notifications.exists()


def test_exact_completion_ack_rejects_missing_durable_event(authorities):
    _checkpoint, notifications = authorities
    _write_private(notifications, {"version": 1, "events": {}})
    registry = ProcessRegistry()
    event = {
        "event_id": "process:proc_missing:completion",
        "type": "completion",
        "session_id": "proc_missing",
        "created_at": 1.0,
    }

    assert registry.mark_completion_consumed(event) is False


def test_spawn_and_recovery_share_one_lifecycle_fence(
    exact_registry, authorities, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    recovered = threading.Event()

    def admitted(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return process_registry_module.ProcessSession("proc_fenced", "true")

    monkeypatch.setattr(exact_registry, "_spawn_local_admitted", admitted)
    spawn_thread = threading.Thread(
        target=lambda: exact_registry.spawn_local("true")
    )
    recovery_thread = threading.Thread(
        target=lambda: (
            exact_registry.recover_managed_startup_exact(),
            recovered.set(),
        )
    )
    spawn_thread.start()
    assert entered.wait(timeout=2)
    recovery_thread.start()
    time.sleep(0.05)
    assert not recovered.is_set()
    release.set()
    spawn_thread.join(timeout=5)
    recovery_thread.join(timeout=5)
    assert recovered.is_set()


def test_finalizer_and_recovery_share_one_lifecycle_fence(
    exact_registry, authorities, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    recovered = threading.Event()
    session = process_registry_module.ProcessSession(
        "proc_finalizing",
        "true",
        pid=4242,
        process_start_token="token:4242",
        exited=True,
    )
    exact_registry._running[session.id] = session
    original_write = exact_registry._write_checkpoint

    def blocked_write():
        entered.set()
        assert release.wait(timeout=5)
        return original_write()

    monkeypatch.setattr(exact_registry, "_write_checkpoint", blocked_write)
    finalizer = threading.Thread(
        target=lambda: exact_registry._move_to_finished(session)
    )
    recovery = threading.Thread(
        target=lambda: (
            exact_registry.recover_managed_startup_exact(),
            recovered.set(),
        )
    )
    finalizer.start()
    assert entered.wait(timeout=2)
    recovery.start()
    time.sleep(0.05)
    assert not recovered.is_set()
    release.set()
    finalizer.join(timeout=5)
    recovery.join(timeout=5)
    assert recovered.is_set()


def test_checkpoint_tamper_during_post_snapshot_is_ambiguous(
    exact_registry, authorities, monkeypatch
):
    checkpoint, _notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    original = process_registry_module._read_private_json_receipt
    calls = 0

    def tamper(path, **kwargs):
        nonlocal calls
        result = original(path, **kwargs)
        if Path(path) == checkpoint:
            calls += 1
            if calls == 2:
                _write_private(checkpoint, [])
        return result

    monkeypatch.setattr(
        process_registry_module, "_read_private_json_receipt", tamper
    )
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()


def test_record_and_byte_bounds_fail_closed(
    exact_registry, authorities, monkeypatch
):
    checkpoint, _notifications = authorities
    _write_private(checkpoint, [_checkpoint_entry()])
    monkeypatch.setattr(process_registry_module, "MAX_MANAGED_RECOVERY_RECORDS", 0)
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()

    monkeypatch.setattr(process_registry_module, "MAX_MANAGED_RECOVERY_RECORDS", 4096)
    monkeypatch.setattr(process_registry_module, "MAX_CHECKPOINT_BYTES", 2)
    with pytest.raises(ManagedProcessRecoveryAmbiguous):
        exact_registry.recover_managed_startup_exact()
