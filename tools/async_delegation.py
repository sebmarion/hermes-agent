#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
# Age/size retention defaults for terminal async delegation records. Running
# records are never pruned by cleanup: if the process still owns the handle,
# status surfaces should keep showing it. These caps currently protect the
# in-memory status registry; if/when async records are persisted, the same
# policy becomes the disk cleanup contract.
_DEFAULT_COMPLETED_RETENTION_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_FAILED_RETENTION_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_LOST_RETENTION_SECONDS = 14 * 24 * 60 * 60
_DEFAULT_MAX_STORE_BYTES = 250 * 1024 * 1024
# Throttle opportunistic cleanup so status reads stay cheap.
_CLEANUP_INTERVAL_SECONDS = 15 * 60
_last_cleanup_at = 0.0
_persist_lock = threading.Lock()
_recovery_attempted = False
_replayed_persisted_ids: set[tuple[str, str]] = set()
_PERSISTENCE_VERSION = 1
# Lightweight liveness ping for status consumers (/agents, TUI/Desktop
# delegation.status). Completion delivery still rides the shared process queue;
# this heartbeat only proves that the async-delegation supervisor in this
# process still owns the handle, so a UI can distinguish "still running" from
# "no record / likely lost with process restart" without waiting for the final
# re-entry event.
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_HEARTBEAT_STALE_SECONDS = _HEARTBEAT_INTERVAL_SECONDS * 3


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of delegations still running or finishing durable delivery."""
    with _records_lock:
        return sum(
            1
            for record in _records.values()
            if record.get("status") in {"scheduled", "running"}
            or record.get("delivery_status") == "finalizing"
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _owner_pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def _persistence_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from hermes_cli.config import get_hermes_home

        home = get_hermes_home()
    except Exception:
        home = Path(os.path.expanduser("~/.hermes"))
    return Path(home) / "async_delegations.json"


def _read_persisted_unlocked(path: str | Path | None = None) -> Dict[str, Any]:
    path = Path(_persistence_path() if path is None else _persistence_path(path))
    if not path.exists():
        return {"version": _PERSISTENCE_VERSION, "records": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": _PERSISTENCE_VERSION, "records": {}}
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        data["version"] = data.get("version") or _PERSISTENCE_VERSION
        return data
    except Exception as exc:
        logger.warning("Failed to read async delegation checkpoint %s: %s", path, exc)
        return {"version": _PERSISTENCE_VERSION, "records": {}}


def _write_persisted_unlocked(data: Dict[str, Any], path: str | Path | None = None) -> None:
    path = Path(_persistence_path() if path is None else _persistence_path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _persistable_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: v
        for k, v in record.items()
        if k not in {"interrupt_fn", "heartbeat_stop"}
    }


def _load_delegation_config() -> Dict[str, Any]:
    """Load delegation config defensively without making async status fragile."""
    try:
        from hermes_cli.config import load_config

        full = load_config() or {}
        cfg = full.get("delegation") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _positive_number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _retention_policy_from_config() -> Dict[str, float]:
    cfg = _load_delegation_config()
    completed_days = _positive_number(
        cfg.get("async_retention_days"),
        _DEFAULT_COMPLETED_RETENTION_SECONDS / 86400,
    )
    failed_days = _positive_number(
        cfg.get("async_failed_retention_days"),
        _DEFAULT_FAILED_RETENTION_SECONDS / 86400,
    )
    lost_days = _positive_number(
        cfg.get("async_lost_retention_days"),
        _DEFAULT_LOST_RETENTION_SECONDS / 86400,
    )
    max_mb = _positive_number(
        cfg.get("async_max_store_mb"),
        _DEFAULT_MAX_STORE_BYTES / (1024 * 1024),
    )
    return {
        "completed_seconds": completed_days * 86400,
        "failed_seconds": failed_days * 86400,
        "lost_seconds": lost_days * 86400,
        "max_bytes": max_mb * 1024 * 1024,
    }


def _record_terminal_age(record: Dict[str, Any], now: float) -> float:
    completed_at = record.get("completed_at") or record.get("last_heartbeat_at") or record.get("dispatched_at") or now
    try:
        return max(0.0, now - float(completed_at))
    except (TypeError, ValueError):
        return 0.0


def _terminal_retention_seconds(status: str, policy: Dict[str, float]) -> float:
    if status in {"error", "failed"}:
        return policy["failed_seconds"]
    if status in {"stale", "lost"}:
        return policy["lost_seconds"]
    return policy["completed_seconds"]


def _record_size_bytes(record: Dict[str, Any]) -> int:
    """Rough JSON size for retention budgeting; excludes live closures/events."""
    serialisable = {
        k: v
        for k, v in record.items()
        if k not in {"interrupt_fn", "heartbeat_stop"}
    }
    try:
        return len(json.dumps(serialisable, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _persisted_entry_size(entry: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _cleanup_persisted_data_locked(data: Dict[str, Any], *, now: float) -> int:
    policy = _retention_policy_from_config()
    records = data.setdefault("records", {})
    if not isinstance(records, dict):
        data["records"] = {}
        return 0
    removed = 0
    for rid, entry in list(records.items()):
        if not isinstance(entry, dict):
            records.pop(rid, None)
            removed += 1
            continue
        record = entry.get("record") if isinstance(entry.get("record"), dict) else entry
        status = str(entry.get("status") or record.get("status") or "")
        delivery_status = str(entry.get("delivery_status") or "")
        if status == "running":
            continue
        age = _record_terminal_age(record, now)
        ttl = _terminal_retention_seconds(status, policy)
        if delivery_status != "delivered" and status not in {"completed", "interrupted"}:
            ttl = policy["failed_seconds"]
        if age > ttl:
            records.pop(rid, None)
            removed += 1

    total = sum(_persisted_entry_size(e) for e in records.values() if isinstance(e, dict))
    max_bytes = policy.get("max_bytes", _DEFAULT_MAX_STORE_BYTES)
    if total <= max_bytes:
        return removed
    terminal = []
    for rid, entry in records.items():
        if not isinstance(entry, dict):
            continue
        record = entry.get("record") if isinstance(entry.get("record"), dict) else entry
        if record.get("status") == "running":
            continue
        terminal.append((rid, entry, record))
    terminal.sort(key=lambda item: item[2].get("completed_at") or item[2].get("dispatched_at") or 0)
    for rid, entry, _record in terminal:
        if total <= max_bytes:
            break
        total -= _persisted_entry_size(entry)
        records.pop(rid, None)
        removed += 1
    return removed


def _persist_record(
    record: Dict[str, Any],
    *,
    result: Optional[Dict[str, Any]] = None,
    event: Optional[Dict[str, Any]] = None,
    delivery_status: Optional[str] = None,
) -> None:
    """Best-effort durable checkpoint for one async delegation record."""
    delegation_id = str(record.get("delegation_id") or "")
    if not delegation_id:
        return
    now = time.time()
    entry = {
        "delegation_id": delegation_id,
        "record": _persistable_record(record),
        "status": record.get("status"),
        "updated_at": now,
    }
    if result is not None:
        entry["result"] = result
    if event is not None:
        entry["event"] = event
    if delivery_status is not None:
        entry["delivery_status"] = delivery_status
        if delivery_status == "queued":
            entry["queued_at"] = now
        elif delivery_status == "delivered":
            entry["delivered_at"] = now
    try:
        with _persist_lock:
            tracker_path = record.get("origin_tracker_path") or None
            data = _read_persisted_unlocked(tracker_path)
            existing = data.setdefault("records", {}).get(delegation_id, {})
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(entry)
            if result is None and "result" in existing:
                merged["result"] = existing["result"]
            if event is None and "event" in existing:
                merged["event"] = existing["event"]
            if delivery_status is None and "delivery_status" in existing:
                merged["delivery_status"] = existing["delivery_status"]
            data["records"][delegation_id] = merged
            _cleanup_persisted_data_locked(data, now=now)
            _write_persisted_unlocked(data, tracker_path)
    except Exception as exc:
        logger.warning("Failed to persist async delegation %s: %s", delegation_id, exc)


def _mark_persisted_delivery(
    delegation_id: str,
    status: str,
    *,
    tracker_path: str | Path | None = None,
    raise_on_error: bool = False,
) -> bool:
    if not delegation_id:
        return False
    now = time.time()
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            entry = data.setdefault("records", {}).get(delegation_id)
            if not isinstance(entry, dict):
                return False
            entry["delivery_status"] = status
            entry["updated_at"] = now
            if status == "queued":
                entry["queued_at"] = now
            elif status == "delivered":
                entry["delivered_at"] = now
            _cleanup_persisted_data_locked(data, now=now)
            _write_persisted_unlocked(data, tracker_path)
            verify = _read_persisted_unlocked(tracker_path)
            stored = (verify.get("records") or {}).get(delegation_id) or {}
            if stored.get("delivery_status") != status:
                raise OSError("async delegation ACK verification failed")
            return True
    except Exception as exc:
        if raise_on_error:
            raise OSError(
                f"Failed to update async delegation delivery {delegation_id}: {exc}"
            ) from exc
        logger.warning("Failed to update async delegation delivery %s: %s", delegation_id, exc)
    return False


def mark_async_delegation_delivered(evt: Dict[str, Any]) -> bool:
    """ACK that an async-delegation notification was consumed by a driver."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        raise ValueError("async delegation ACK requires an async_delegation event")
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        raise ValueError("async delegation ACK requires delegation_id")
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["delivery_status"] = "delivered"
            record["delivered_at"] = time.time()
    tracker_path = str(evt.get("origin_tracker_path") or "") or None
    if not _mark_persisted_delivery(
        delegation_id,
        "delivered",
        tracker_path=tracker_path,
        raise_on_error=True,
    ):
        raise KeyError(f"async delegation tracker record not found: {delegation_id}")
    return True


def _event_for_lost_record(record: Dict[str, Any]) -> Dict[str, Any]:
    delegation_id = str(record.get("delegation_id") or "")
    result = {
        "status": "error",
        "summary": (
            "Async delegation was still running when the Hermes process stopped; "
            "the child result cannot be recovered."
        ),
        "error": "async delegation lost during process restart",
        "api_calls": 0,
        "duration_seconds": 0,
        "model": record.get("model") or "",
        "exit_reason": "lost",
    }
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": record.get("session_key") or "",
        "origin_ui_session_id": record.get("origin_ui_session_id") or "",
        "origin_profile": record.get("origin_profile") or "",
        "origin_tracker_path": record.get("origin_tracker_path") or "",
        "parent_session_id": record.get("parent_session_id"),
        "bestplan_plan_id": record.get("bestplan_plan_id") or "",
        "goal": record.get("goal") or "background delegation",
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role") or "leaf",
        "model": record.get("model") or "",
        "result": result,
        "status": "error",
        "summary": result["summary"],
        "error": result["error"],
    }


def recover_async_delegations(
    tracker_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Replay undelivered completions and mark previous-process runners lost."""
    global _recovery_attempted
    queued = 0
    lost = 0
    now = time.time()
    try:
        from tools.process_registry import process_registry
    except Exception as exc:
        return {"queued": 0, "lost": 0, "error": str(exc)}
    with _persist_lock:
        data = _read_persisted_unlocked(tracker_path)
        records = data.setdefault("records", {})
        if not isinstance(records, dict):
            return {"queued": 0, "lost": 0, "error": "invalid records"}
        for rid, entry in list(records.items()):
            if not isinstance(entry, dict):
                records.pop(rid, None)
                continue
            record = entry.get("record") if isinstance(entry.get("record"), dict) else {}
            if not isinstance(record, dict):
                continue
            status = str(entry.get("status") or record.get("status") or "")
            delivery_status = str(entry.get("delivery_status") or "")
            event = entry.get("event") if isinstance(entry.get("event"), dict) else None
            if status == "scheduled" and not _owner_pid_alive(record.get("owner_pid")):
                # The durable worker gate opens only after this phase is stored.
                # A fresh process cannot own that queued Future, and the runner
                # never crossed its running gate, so retry from intent.
                record = dict(record)
                record["status"] = "intent"
                record["delivery_status"] = "intent"
                entry["record"] = record
                entry["status"] = "intent"
                entry["delivery_status"] = "intent"
                entry["updated_at"] = now
                status = "intent"
                delivery_status = "intent"
            if status == "running":
                record = dict(record)
                record["status"] = "lost"
                record["completed_at"] = now
                record["last_heartbeat_at"] = record.get("last_heartbeat_at") or record.get("dispatched_at") or now
                event = _event_for_lost_record(record)
                entry["record"] = record
                entry["status"] = "lost"
                entry["event"] = event
                entry["result"] = event["result"]
                entry["delivery_status"] = "pending"
                entry["updated_at"] = now
                status = "lost"
                delivery_status = "pending"
                lost += 1
            replay_identity = (str(_persistence_path(tracker_path)), str(rid))
            if status != "running" and delivery_status != "delivered" and event and replay_identity not in _replayed_persisted_ids:
                process_registry.completion_queue.put(event)
                _replayed_persisted_ids.add(replay_identity)
                entry["delivery_status"] = "queued"
                entry["queued_at"] = now
                entry["updated_at"] = now
                queued += 1
                with _records_lock:
                    if rid not in _records:
                        restored = dict(record)
                        restored["delivery_status"] = "queued"
                        _records[rid] = restored
        _cleanup_persisted_data_locked(data, now=now)
        _write_persisted_unlocked(data, tracker_path)
    _recovery_attempted = True
    return {"queued": queued, "lost": lost}


def _recover_once() -> None:
    global _recovery_attempted
    if _recovery_attempted:
        return
    try:
        recover_async_delegations()
    except Exception as exc:
        logger.debug("async delegation recovery failed: %s", exc)
        _recovery_attempted = True


def _cleanup_locked(now: Optional[float] = None, policy: Optional[Dict[str, float]] = None) -> int:
    """Prune terminal async records by age and rough size. Lock must be held."""
    now = time.time() if now is None else now
    policy = policy or _retention_policy_from_config()
    removed = 0

    # First pass: age-based pruning by terminal status. Running records are
    # never removed here; a live process should keep surfacing them even if
    # heartbeat looks stale.
    for rid, record in list(_records.items()):
        status = str(record.get("status") or "")
        if status == "running":
            continue
        age = _record_terminal_age(record, now)
        if age > _terminal_retention_seconds(status, policy):
            _records.pop(rid, None)
            removed += 1

    # Second pass: rough size cap. Drop oldest terminal records first. If the
    # cap is lower than the running set itself, leave running untouched and stop.
    max_bytes = policy.get("max_bytes", _DEFAULT_MAX_STORE_BYTES)
    total = sum(_record_size_bytes(r) for r in _records.values())
    if total <= max_bytes:
        return removed

    terminal = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    terminal.sort(
        key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0
    )
    for rid, record in terminal:
        if total <= max_bytes:
            break
        total -= _record_size_bytes(record)
        _records.pop(rid, None)
        removed += 1
    return removed


def cleanup_async_delegations() -> Dict[str, Any]:
    """Run async delegation retention cleanup now and return a compact report."""
    now = time.time()
    policy = _retention_policy_from_config()
    with _records_lock:
        before = len(_records)
        removed = _cleanup_locked(now=now, policy=policy)
        after = len(_records)
        approx_bytes = sum(_record_size_bytes(r) for r in _records.values())
    with _persist_lock:
        data = _read_persisted_unlocked()
        persisted_before = len(data.get("records", {}) or {})
        persisted_removed = _cleanup_persisted_data_locked(data, now=now)
        persisted_after = len(data.get("records", {}) or {})
        _write_persisted_unlocked(data)
    return {
        "removed": removed,
        "before": before,
        "after": after,
        "approx_bytes": approx_bytes,
        "max_bytes": int(policy["max_bytes"]),
        "persisted_removed": persisted_removed,
        "persisted_before": persisted_before,
        "persisted_after": persisted_after,
    }


def _maybe_cleanup() -> None:
    """Cheap opportunistic cleanup, throttled to avoid status-read overhead."""
    global _last_cleanup_at
    now = time.time()
    if now - _last_cleanup_at < _CLEANUP_INTERVAL_SECONDS:
        return
    with _records_lock:
        if now - _last_cleanup_at < _CLEANUP_INTERVAL_SECONDS:
            return
        _cleanup_locked(now=now)
        _last_cleanup_at = now


def _mark_heartbeat(delegation_id: str) -> None:
    """Refresh the liveness timestamp for a running async delegation."""
    now = time.time()
    with _records_lock:
        record = _records.get(delegation_id)
        if not record or record.get("status") != "running":
            return
        record["last_heartbeat_at"] = now
        record["heartbeat_count"] = int(record.get("heartbeat_count") or 0) + 1


def _start_heartbeat_thread(delegation_id: str) -> threading.Event:
    """Start a daemon heartbeat updater for one async delegation record."""
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            _mark_heartbeat(delegation_id)

    thread = threading.Thread(
        target=_loop,
        name=f"async-delegate-heartbeat-{delegation_id}",
        daemon=True,
    )
    thread.start()
    return stop


def _serialise_record(record: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Return a JSON-safe status snapshot with derived liveness fields."""
    out = {
        k: v
        for k, v in record.items()
        if k not in {"interrupt_fn", "heartbeat_stop"}
    }
    dispatched_at = float(record.get("dispatched_at") or now)
    completed_at = record.get("completed_at")
    last_heartbeat_at = float(record.get("last_heartbeat_at") or dispatched_at)
    out["age_seconds"] = round(max(0.0, now - dispatched_at), 2)
    if completed_at:
        out["completed_age_seconds"] = round(max(0.0, now - float(completed_at)), 2)
    else:
        heartbeat_age = max(0.0, now - last_heartbeat_at)
        out["heartbeat_age_seconds"] = round(heartbeat_age, 2)
        out["heartbeat_stale"] = heartbeat_age > _HEARTBEAT_STALE_SECONDS
    return out


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    _recover_once()

    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "last_heartbeat_at": dispatched_at,
        "heartbeat_count": 0,
        "delivery_status": "running",
        "interrupt_fn": interrupt_fn,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record
    _persist_record(record, delivery_status="running")

    heartbeat_stop = _start_heartbeat_thread(delegation_id)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["heartbeat_stop"] = heartbeat_stop

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            failed_record = _records.pop(delegation_id, None)
        if failed_record:
            hb_stop = failed_record.get("heartbeat_stop")
            if hasattr(hb_stop, "set"):
                hb_stop.set()
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["last_heartbeat_at"] = record["completed_at"]
        record["delivery_status"] = "finalizing"
        hb_stop = record.get("heartbeat_stop")
        if hasattr(hb_stop, "set"):
            hb_stop.set()
        record["interrupt_fn"] = None  # drop the closure; child is done
        # Snapshot fields needed for the event while holding the lock.
        event_record = dict(record)
        _prune_completed_locked()
        _cleanup_locked(now=record["completed_at"])

    _push_completion_event(event_record, result, status)


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    try:
        delegation_id = str(record.get("delegation_id") or "")
        _persist_record(record, result=result, event=evt, delivery_status="pending")
        _mark_persisted_delivery(delegation_id, "queued")
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "queued"
                live["queued_at"] = time.time()
        # Publish only after the durable record says queued, so a fast
        # consumer cannot ACK a completion whose persistence still says pending.
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        delegation_id = str(record.get("delegation_id") or "")
        _mark_persisted_delivery(delegation_id, "pending")
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "pending"
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "persisted for replay: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
    origin_profile: str = "",
    origin_tracker_path: str = "",
    bestplan_plan_id: str = "",
    resolved_runtimes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    if not origin_tracker_path:
        _recover_once()
    delegation_id = str(delegation_id or _new_delegation_id())
    if origin_tracker_path:
        with _persist_lock:
            persisted = _read_persisted_unlocked(origin_tracker_path)
            existing = (persisted.get("records") or {}).get(delegation_id)
        if isinstance(existing, dict):
            phase = str(existing.get("status") or (existing.get("record") or {}).get("status") or "")
            if phase in {"scheduled", "running"}:
                owner_pid = (existing.get("record") or {}).get("owner_pid")
                if phase == "scheduled" and not _owner_pid_alive(owner_pid):
                    phase = "intent"
                else:
                    return {
                        "status": "dispatched",
                        "delegation_id": delegation_id,
                        "phase": phase,
                        "idempotent_replay": True,
                    }
            if phase not in {"", "intent"}:
                return {
                    "status": "terminal",
                    "phase": phase,
                    "delegation_id": delegation_id,
                    "idempotent_replay": True,
                }
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_profile": origin_profile,
        "origin_tracker_path": origin_tracker_path,
        "parent_session_id": parent_session_id,
        "bestplan_plan_id": bestplan_plan_id,
        "resolved_runtimes": list(resolved_runtimes or []),
        "status": "intent",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "last_heartbeat_at": dispatched_at,
        "heartbeat_count": 0,
        "delivery_status": "intent",
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "owner_pid": os.getpid(),
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") in {"scheduled", "running"}
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record
    _persist_record(record, delivery_status="intent")
    if origin_tracker_path:
        with _persist_lock:
            verify = _read_persisted_unlocked(origin_tracker_path)
            durable = (verify.get("records") or {}).get(delegation_id)
        if not isinstance(durable, dict):
            with _records_lock:
                _records.pop(delegation_id, None)
            return {
                "status": "rejected",
                "error": "strict async dispatch intent could not be persisted",
            }

    executor = _get_executor(max_async_children)
    start_gate = threading.Event()
    abort_gate = threading.Event()

    def _worker() -> None:
        start_gate.wait()
        if abort_gate.is_set():
            return
        with _records_lock:
            live = _records.get(delegation_id)
            if live is None or live.get("status") != "scheduled":
                return
            live["status"] = "running"
            live["delivery_status"] = "running"
            running_record = dict(live)
        _persist_record(running_record, delivery_status="running")
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        future = executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            failed_record = _records.get(delegation_id)
            if failed_record is not None:
                failed_record["status"] = "intent"
                failed_record["delivery_status"] = "intent"
                failed_record["last_error"] = str(exc)
                failed_snapshot = dict(failed_record)
            else:
                failed_snapshot = record
        _persist_record(failed_snapshot, delivery_status="intent")
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None:
            live["status"] = "scheduled"
            live["delivery_status"] = "scheduled"
            scheduled_record = dict(live)
        else:
            scheduled_record = None
    if scheduled_record is not None:
        _persist_record(scheduled_record, delivery_status="scheduled")
    durable_scheduled = True
    if origin_tracker_path:
        with _persist_lock:
            verified = _read_persisted_unlocked(origin_tracker_path)
            durable = (verified.get("records") or {}).get(delegation_id) or {}
        durable_scheduled = str(durable.get("status") or "") == "scheduled"
    if not durable_scheduled:
        abort_gate.set()
        start_gate.set()
        future.cancel()
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["status"] = "intent"
                live["delivery_status"] = "intent"
                repaired = dict(live)
            else:
                repaired = record
        _persist_record(repaired, delivery_status="intent")
        return {
            "status": "rejected",
            "error": "strict async scheduled phase could not be persisted",
        }

    heartbeat_stop = _start_heartbeat_thread(delegation_id)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["heartbeat_stop"] = heartbeat_stop
    start_gate.set()

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["last_heartbeat_at"] = record["completed_at"]
        record["delivery_status"] = "finalizing"
        hb_stop = record.get("heartbeat_stop")
        if hasattr(hb_stop, "set"):
            hb_stop.set()
        record["interrupt_fn"] = None
        event_record = dict(record)
        _prune_completed_locked()
        _cleanup_locked(now=record["completed_at"])

    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_profile": event_record.get("origin_profile", ""),
        "origin_tracker_path": event_record.get("origin_tracker_path", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "bestplan_plan_id": event_record.get("bestplan_plan_id", ""),
        "resolved_runtimes": event_record.get("resolved_runtimes") or [],
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    plan_id = str(event_record.get("bestplan_plan_id") or "")
    tracker_path = str(event_record.get("origin_tracker_path") or "")
    if plan_id and tracker_path:
        try:
            from agent.bestplan_state import BestplanStore

            plan_store = BestplanStore(db_path=Path(tracker_path).parent / "state.db")
            try:
                plan_store.mark_completed_unverified(plan_id, evt)
            finally:
                plan_store.close()
        except Exception:
            logger.warning(
                "BestPlan completion evidence persistence failed for %s",
                plan_id,
                exc_info=True,
            )
    try:
        _persist_record(event_record, result=combined, event=evt, delivery_status="pending")
        _mark_persisted_delivery(
            delegation_id, "queued", tracker_path=tracker_path or None,
        )
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "queued"
                live["queued_at"] = time.time()
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        try:
            _mark_persisted_delivery(
                delegation_id, "pending", tracker_path=tracker_path or None,
            )
        except Exception:
            logger.warning("failed to retain async delegation pending marker", exc_info=True)
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "pending"
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "persisted for replay: %s",
            delegation_id, exc,
        )


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    _recover_once()
    _maybe_cleanup()
    now = time.time()
    with _records_lock:
        return [_serialise_record(r, now) for r in _records.values()]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers, _last_cleanup_at, _recovery_attempted
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        for record in _records.values():
            hb_stop = record.get("heartbeat_stop")
            if hasattr(hb_stop, "set"):
                hb_stop.set()
        _records.clear()
        _last_cleanup_at = 0.0
    _recovery_attempted = False
    _replayed_persisted_ids.clear()
    try:
        _persistence_path().unlink(missing_ok=True)
    except TypeError:
        path = _persistence_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass
