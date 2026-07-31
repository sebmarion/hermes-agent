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
import hashlib
import logging
import math
import os
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.durable_state import (
    FileIdentity,
    atomic_write_private_json,
    hold_private_authority_directory,
    interprocess_authority_lock,
    read_private_json,
)
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
_replay_ids_lock = threading.Lock()
_replayed_persisted_ids: set[str] = set()
_runtime_owner_lock = threading.Lock()
_runtime_owner_id = ""
_runtime_owner_pid = 0
_runtime_owner_start_token = ""
_PERSISTENCE_VERSION = 1
_MAX_PERSISTENCE_BYTES = 512 * 1024 * 1024
_UNSPECIFIED_IDENTITY = object()
_DELIVERY_STATUS_RANK = {
    "": 0,
    "running": 1,
    "finalizing": 2,
    "pending": 3,
    "queued": 4,
    "delivered": 5,
}
# Lightweight liveness ping for status consumers (/agents, TUI/Desktop
# delegation.status). Completion delivery still rides the shared process queue;
# this heartbeat only proves that the async-delegation supervisor in this
# process still owns the handle, so a UI can distinguish "still running" from
# "no record / likely lost with process restart" without waiting for the final
# re-entry event.
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_HEARTBEAT_STALE_SECONDS = _HEARTBEAT_INTERVAL_SECONDS * 3

_MANAGED_OUTBOX_VERSION = 1
_MAX_MANAGED_TRACKERS = 256
_MAX_MANAGED_RECORDS = 10_000
_MAX_MANAGED_OUTBOX_BYTES = 64 * 1024 * 1024
_MAX_MANAGED_AGGREGATE_BYTES = 128 * 1024 * 1024
_MANAGED_DELIVERED_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MANAGED_TOMBSTONE_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MANAGED_TERMINAL_STATUSES = {
    "completed",
    "error",
    "failed",
    "interrupted",
    "lost",
}


class ManagedAsyncDelegationRecoveryOutcome(str, Enum):
    ABSENT = "ABSENT"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ManagedAsyncDelegationProfile:
    profile_id: str
    tracker_path: Path


@dataclass(frozen=True)
class ManagedAsyncDelegationProfileManifest:
    generation: str
    profiles: tuple[ManagedAsyncDelegationProfile, ...]
    expected_profile_ids: tuple[str, ...]
    source_digest: str


def _managed_crash_hook(
    hook: Optional[Callable[[str], None]],
    stage: str,
) -> None:
    if hook is None:
        return
    try:
        hook(stage)
    except Exception as exc:
        setattr(exc, "_managed_crash_injection", True)
        raise


@dataclass(frozen=True)
class ManagedAsyncDelegationRecoveryReceipt:
    outcome: ManagedAsyncDelegationRecoveryOutcome
    tracker_paths: tuple[str, ...]
    tracker_hashes_before: tuple[tuple[str, Optional[str]], ...] = ()
    tracker_hashes_after: tuple[tuple[str, Optional[str]], ...] = ()
    tracker_identities_before: tuple[tuple[str, Optional[dict]], ...] = ()
    tracker_identities_after: tuple[tuple[str, Optional[dict]], ...] = ()
    outbox_path: str = ""
    outbox_hash_before: Optional[str] = None
    outbox_hash_after: Optional[str] = None
    delegation_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    queued_event_ids: tuple[str, ...] = ()
    deduped_event_ids: tuple[str, ...] = ()
    status_transitions: tuple[tuple[str, str, str], ...] = ()
    recovery_epoch: str = ""
    process_pid: int = 0
    process_start_token: str = ""
    runtime_generation: str = ""
    manifest_generation: str = ""
    manifest_source_digest: str = ""
    record_classifications: tuple[tuple[str, str], ...] = ()
    event_postconditions: tuple["ManagedAsyncEventPostcondition", ...] = ()
    verification_sha256: str = ""
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedAsyncEventPostcondition:
    event_id: str
    kind: str
    state: str
    row_sha256: str
    event_sha256: str
    immutable_sha256: str
    created_at: Optional[float]
    last_replay_epoch: str


@dataclass(frozen=True)
class ManagedAsyncDelegationVerificationReceipt:
    outcome: ManagedAsyncDelegationRecoveryOutcome
    tracker_hashes: tuple[tuple[str, Optional[str]], ...] = ()
    tracker_identities: tuple[tuple[str, Optional[dict]], ...] = ()
    outbox_hash: Optional[str] = None
    delegation_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    queue_event_ids: tuple[str, ...] = ()
    record_classifications: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()


def _managed_json_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _managed_identity(identity: Optional[FileIdentity]) -> Optional[dict]:
    return asdict(identity) if identity is not None else None


def _managed_paths(
    tracker_paths: Sequence[Path],
    outbox_path: Path,
) -> tuple[tuple[Path, ...], Path]:
    if isinstance(tracker_paths, (str, bytes, Path)):
        raise TypeError("managed tracker paths must be an explicit sequence")
    if len(tracker_paths) > _MAX_MANAGED_TRACKERS:
        raise ValueError("too many managed async delegation trackers")
    canonical: list[Path] = []
    for raw_path in tracker_paths:
        path = Path(raw_path)
        if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
            raise ValueError("managed tracker path must be absolute and canonical")
        canonical.append(path)
    if len(set(canonical)) != len(canonical):
        raise ValueError("managed tracker paths must be unique")
    canonical = sorted(canonical, key=os.fspath)
    outbox = Path(outbox_path)
    if (
        not outbox.is_absolute()
        or outbox.parent.resolve(strict=True) != outbox.parent
        or outbox in canonical
    ):
        raise ValueError("managed outbox path must be distinct, absolute, and canonical")
    return tuple(canonical), outbox


def _managed_read_tracker(
    path: Path,
) -> tuple[Dict[str, Any], Optional[FileIdentity]]:
    raw, identity = read_private_json(
        path,
        max_bytes=_MAX_PERSISTENCE_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return {"version": _PERSISTENCE_VERSION, "records": {}}, None
    data = _validate_persisted_data(raw)
    if len(data["records"]) > _MAX_MANAGED_RECORDS:
        raise ValueError("managed async delegation tracker has too many records")
    return data, identity


def _managed_read_outbox(
    path: Path,
) -> tuple[Dict[str, Any], Optional[FileIdentity]]:
    raw, identity = read_private_json(
        path,
        max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return {"version": _MANAGED_OUTBOX_VERSION, "events": {}}, None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _MANAGED_OUTBOX_VERSION
        or not isinstance(raw.get("events"), dict)
        or len(raw["events"]) > _MAX_MANAGED_RECORDS
    ):
        raise ValueError("managed async delegation outbox schema is invalid")
    events: Dict[str, Dict[str, Any]] = {}
    for event_id, row in raw["events"].items():
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(row, dict)
            or row.get("event_id") != event_id
            or row.get("state") not in {"intent", "enqueued", "delivered"}
            or not isinstance(row.get("event"), dict)
            or row["event"].get("managed_event_id") != event_id
            or not isinstance(row.get("tracker_hash"), str)
            or not row["tracker_hash"]
            or not isinstance(row.get("tracker_identity"), dict)
        ):
            raise ValueError("managed async delegation outbox event is invalid")
        events[event_id] = dict(row)
    return {"version": _MANAGED_OUTBOX_VERSION, "events": events}, identity


def _managed_queue_event_once(completion_queue: Any, event: Dict[str, Any]) -> bool:
    event_id = event["managed_event_id"]
    mutex = getattr(completion_queue, "mutex", None)
    storage = getattr(completion_queue, "queue", None)
    not_empty = getattr(completion_queue, "not_empty", None)
    if mutex is None or storage is None or not_empty is None:
        raise TypeError("managed completion queue must provide exact membership")
    with mutex:
        for existing in storage:
            if isinstance(existing, dict) and existing.get("managed_event_id") == event_id:
                if existing != event:
                    raise ValueError("managed queue event identity collision")
                return False
        maxsize = getattr(completion_queue, "maxsize", 0)
        if isinstance(maxsize, int) and maxsize > 0 and len(storage) >= maxsize:
            raise RuntimeError("managed completion queue is full")
        storage.append(dict(event))
        completion_queue.unfinished_tasks += 1
        not_empty.notify()
    return True


def _managed_receipt(
    *,
    outcome: ManagedAsyncDelegationRecoveryOutcome,
    trackers: Sequence[Path],
    outbox: Path,
    epoch: str,
    before: Sequence[tuple[Path, Dict[str, Any], Optional[FileIdentity]]] = (),
    after: Sequence[tuple[Path, Dict[str, Any], Optional[FileIdentity]]] = (),
    outbox_before: Optional[tuple[Dict[str, Any], Optional[FileIdentity]]] = None,
    outbox_after: Optional[tuple[Dict[str, Any], Optional[FileIdentity]]] = None,
    delegation_ids: Sequence[str] = (),
    event_ids: Sequence[str] = (),
    queued: Sequence[str] = (),
    deduped: Sequence[str] = (),
    transitions: Sequence[tuple[str, str, str]] = (),
    errors: Sequence[str] = (),
) -> ManagedAsyncDelegationRecoveryReceipt:
    return ManagedAsyncDelegationRecoveryReceipt(
        outcome=outcome,
        tracker_paths=tuple(os.fspath(path) for path in trackers),
        tracker_hashes_before=tuple(
            (os.fspath(path), _managed_json_hash(data)) for path, data, _ in before
        ),
        tracker_hashes_after=tuple(
            (os.fspath(path), _managed_json_hash(data)) for path, data, _ in after
        ),
        tracker_identities_before=tuple(
            (os.fspath(path), _managed_identity(identity))
            for path, _, identity in before
        ),
        tracker_identities_after=tuple(
            (os.fspath(path), _managed_identity(identity))
            for path, _, identity in after
        ),
        outbox_path=os.fspath(outbox),
        outbox_hash_before=(
            _managed_json_hash(outbox_before[0]) if outbox_before else None
        ),
        outbox_hash_after=(
            _managed_json_hash(outbox_after[0]) if outbox_after else None
        ),
        delegation_ids=tuple(delegation_ids),
        event_ids=tuple(event_ids),
        queued_event_ids=tuple(queued),
        deduped_event_ids=tuple(deduped),
        status_transitions=tuple(transitions),
        recovery_epoch=epoch,
        errors=tuple(errors),
    )


def _recover_managed_async_delegations_exact_v1(
    tracker_paths: Sequence[Path],
    *,
    outbox_path: Path,
    completion_queue: Any,
    recovery_epoch: str,
    crash_hook: Optional[Callable[[str], None]] = None,
) -> ManagedAsyncDelegationRecoveryReceipt:
    """Recover explicitly enumerated profile trackers through a durable outbox."""
    try:
        trackers, outbox = _managed_paths(tracker_paths, outbox_path)
        if not isinstance(recovery_epoch, str) or not recovery_epoch:
            raise ValueError("managed recovery epoch is required")
    except Exception as exc:
        raw_trackers = tuple(Path(path) for path in tracker_paths)
        return _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=raw_trackers,
            outbox=Path(outbox_path),
            epoch=str(recovery_epoch or ""),
            errors=(str(exc),),
        )

    before: list[tuple[Path, Dict[str, Any], Optional[FileIdentity]]] = []
    after: list[tuple[Path, Dict[str, Any], Optional[FileIdentity]]] = []
    outbox_before: Optional[tuple[Dict[str, Any], Optional[FileIdentity]]] = None
    outbox_after: Optional[tuple[Dict[str, Any], Optional[FileIdentity]]] = None
    delegation_ids: list[str] = []
    event_ids: list[str] = []
    queued: list[str] = []
    deduped: list[str] = []
    transitions: list[tuple[str, str, str]] = []

    try:
        with ExitStack() as stack:
            for authority in sorted((*trackers, outbox), key=os.fspath):
                stack.enter_context(interprocess_authority_lock(authority))
            for tracker in trackers:
                data, identity = _managed_read_tracker(tracker)
                before.append((tracker, data, identity))
            outbox_data, outbox_identity = _managed_read_outbox(outbox)
            outbox_before = (outbox_data, outbox_identity)

            candidates: list[
                tuple[Path, Dict[str, Any], Optional[FileIdentity], str, str, dict]
            ] = []
            for tracker, data, identity in before:
                for delegation_id, entry in sorted(data["records"].items()):
                    status = str(entry.get("status") or "")
                    delivery = str(entry.get("delivery_status") or "")
                    event = entry.get("event")
                    if status == "running":
                        record = entry.get("record")
                        if not isinstance(record, dict) or not all(
                            record.get(name) is not None
                            for name in (
                                "runtime_owner_id",
                                "runtime_owner_pid",
                                "runtime_owner_start_token",
                            )
                        ):
                            raise ValueError(
                                f"{tracker}: running delegation {delegation_id} "
                                "has no exact runtime owner"
                            )
                        if _persistence_owner_is_live(record):
                            continue
                        raise ValueError(
                            f"{tracker}: dead running delegation "
                            f"{delegation_id} requires an explicit loss decision"
                        )
                    if delivery == "delivered" or event is None:
                        continue
                    event_id = f"async-delegation:{delegation_id}:completion"
                    managed_event = dict(event)
                    managed_event["managed_event_id"] = event_id
                    managed_event["delegation_id"] = delegation_id
                    candidates.append(
                        (
                            tracker,
                            data,
                            identity,
                            delegation_id,
                            event_id,
                            managed_event,
                        )
                    )

            events = outbox_data["events"]
            outbox_changed = False
            for tracker, _data, _identity, delegation_id, event_id, event in candidates:
                existing = events.get(event_id)
                candidate = {
                    "event_id": event_id,
                    "delegation_id": delegation_id,
                    "tracker_path": os.fspath(tracker),
                    "tracker_hash": _managed_json_hash(_data),
                    "tracker_identity": _managed_identity(_identity),
                    "event": event,
                    "state": "intent",
                    "recovery_epoch": recovery_epoch,
                }
                if existing is None:
                    events[event_id] = candidate
                    outbox_changed = True
                elif (
                    existing.get("delegation_id") != delegation_id
                    or existing.get("tracker_path") != os.fspath(tracker)
                    or existing.get("event") != event
                ):
                    raise ValueError(f"managed outbox collision for {event_id}")
                delegation_ids.append(delegation_id)
                event_ids.append(event_id)

            if outbox_changed:
                outbox_identity = atomic_write_private_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            if candidates:
                _managed_crash_hook(crash_hook, "intent_committed")

            for _tracker, _data, _identity, _delegation_id, event_id, event in candidates:
                row = events[event_id]
                if row["state"] == "delivered":
                    deduped.append(event_id)
                    continue
                if (
                    row["state"] == "enqueued"
                    and row.get("recovery_epoch") == recovery_epoch
                ):
                    deduped.append(event_id)
                    continue
                if _managed_queue_event_once(completion_queue, event):
                    queued.append(event_id)
                else:
                    deduped.append(event_id)
                _managed_crash_hook(crash_hook, "event_enqueued")
                row["state"] = "enqueued"
                row["recovery_epoch"] = recovery_epoch
                outbox_changed = True

            if outbox_changed:
                outbox_identity = atomic_write_private_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            if candidates:
                _managed_crash_hook(crash_hook, "outbox_enqueued")

            for tracker, data, identity in before:
                changed = False
                for delegation_id, entry in data["records"].items():
                    event_id = f"async-delegation:{delegation_id}:completion"
                    row = events.get(event_id)
                    if not row or row["state"] not in {"enqueued", "delivered"}:
                        continue
                    old = str(entry.get("delivery_status") or "")
                    new = "delivered" if row["state"] == "delivered" else "queued"
                    if _DELIVERY_STATUS_RANK.get(new, -1) > _DELIVERY_STATUS_RANK.get(old, -1):
                        entry["delivery_status"] = new
                        if isinstance(entry.get("record"), dict):
                            entry["record"]["delivery_status"] = new
                        transitions.append((delegation_id, old, new))
                        changed = True
                if changed:
                    identity = atomic_write_private_json(
                        tracker,
                        data,
                        expected=identity,
                        max_bytes=_MAX_PERSISTENCE_BYTES,
                        sort_keys=True,
                    )
                reread, final_identity = _managed_read_tracker(tracker)
                after.append((tracker, reread, final_identity))
            final_outbox, final_outbox_identity = _managed_read_outbox(outbox)
            outbox_after = (final_outbox, final_outbox_identity)
    except Exception as exc:
        if getattr(exc, "_managed_crash_injection", False):
            raise
        return _managed_receipt(
            outcome=(
                ManagedAsyncDelegationRecoveryOutcome.PARTIAL
                if outbox_before is not None
                else ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
            ),
            trackers=trackers,
            outbox=outbox,
            epoch=recovery_epoch,
            before=before,
            after=after,
            outbox_before=outbox_before,
            outbox_after=outbox_after,
            delegation_ids=delegation_ids,
            event_ids=event_ids,
            queued=queued,
            deduped=deduped,
            transitions=transitions,
            errors=(str(exc),),
        )

    outcome = (
        ManagedAsyncDelegationRecoveryOutcome.COMPLETE
        if event_ids
        else ManagedAsyncDelegationRecoveryOutcome.ABSENT
    )
    return _managed_receipt(
        outcome=outcome,
        trackers=trackers,
        outbox=outbox,
        epoch=recovery_epoch,
        before=before,
        after=after,
        outbox_before=outbox_before,
        outbox_after=outbox_after,
        delegation_ids=delegation_ids,
        event_ids=event_ids,
        queued=queued,
        deduped=deduped,
        transitions=transitions,
    )


def _mark_managed_async_delegation_delivered_exact_v1(
    event: Dict[str, Any],
    *,
    tracker_paths: Sequence[Path],
    outbox_path: Path,
    crash_hook: Optional[Callable[[str], None]] = None,
) -> ManagedAsyncDelegationRecoveryReceipt:
    """Durably ACK one managed event in its outbox and named tracker."""
    try:
        trackers, outbox = _managed_paths(tracker_paths, outbox_path)
        event_id = event.get("managed_event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("managed delivery event identity is required")
    except Exception as exc:
        return _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=tuple(Path(path) for path in tracker_paths),
            outbox=Path(outbox_path),
            epoch="delivery",
            errors=(str(exc),),
        )

    before = []
    after = []
    transitions: list[tuple[str, str, str]] = []
    outbox_before = None
    outbox_after = None
    delegation_id = str(event.get("delegation_id") or "")
    try:
        with ExitStack() as stack:
            for authority in sorted((*trackers, outbox), key=os.fspath):
                stack.enter_context(interprocess_authority_lock(authority))
            for tracker in trackers:
                data, identity = _managed_read_tracker(tracker)
                before.append((tracker, data, identity))
            outbox_data, outbox_identity = _managed_read_outbox(outbox)
            outbox_before = (outbox_data, outbox_identity)
            row = outbox_data["events"].get(event_id)
            if (
                not isinstance(row, dict)
                or row.get("event") != event
                or row.get("delegation_id") != delegation_id
            ):
                raise ValueError("managed delivery does not match durable intent")
            if row["state"] != "delivered":
                row["state"] = "delivered"
                outbox_identity = atomic_write_private_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            _managed_crash_hook(crash_hook, "delivered_committed")
            found = False
            for tracker, data, identity in before:
                entry = data["records"].get(delegation_id)
                if not isinstance(entry, dict):
                    after.append((tracker, data, identity))
                    continue
                found = True
                old = str(entry.get("delivery_status") or "")
                if old != "delivered":
                    entry["delivery_status"] = "delivered"
                    if isinstance(entry.get("record"), dict):
                        entry["record"]["delivery_status"] = "delivered"
                    transitions.append((delegation_id, old, "delivered"))
                    identity = atomic_write_private_json(
                        tracker,
                        data,
                        expected=identity,
                        max_bytes=_MAX_PERSISTENCE_BYTES,
                        sort_keys=True,
                    )
                reread, final_identity = _managed_read_tracker(tracker)
                after.append((tracker, reread, final_identity))
            if not found:
                raise ValueError("managed delivery tracker record is absent")
            final_outbox, final_outbox_identity = _managed_read_outbox(outbox)
            outbox_after = (final_outbox, final_outbox_identity)
    except Exception as exc:
        if getattr(exc, "_managed_crash_injection", False):
            raise
        return _managed_receipt(
            outcome=(
                ManagedAsyncDelegationRecoveryOutcome.PARTIAL
                if outbox_before is not None
                else ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
            ),
            trackers=trackers,
            outbox=outbox,
            epoch="delivery",
            before=before,
            after=after,
            outbox_before=outbox_before,
            outbox_after=outbox_after,
            delegation_ids=(delegation_id,) if delegation_id else (),
            event_ids=(event_id,),
            transitions=transitions,
            errors=(str(exc),),
        )
    return _managed_receipt(
        outcome=ManagedAsyncDelegationRecoveryOutcome.COMPLETE,
        trackers=trackers,
        outbox=outbox,
        epoch="delivery",
        before=before,
        after=after,
        outbox_before=outbox_before,
        outbox_after=outbox_after,
        delegation_ids=(delegation_id,),
        event_ids=(event_id,),
        transitions=transitions,
    )


def _managed_v2_manifest(
    manifest: ManagedAsyncDelegationProfileManifest,
    outbox_path: Path,
) -> tuple[
    tuple[ManagedAsyncDelegationProfile, ...],
    Path,
]:
    if not isinstance(manifest, ManagedAsyncDelegationProfileManifest):
        raise ValueError("authoritative managed profile manifest is required")
    if (
        not isinstance(manifest.generation, str)
        or not manifest.generation
        or len(manifest.generation.encode("utf-8")) > 256
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in manifest.generation
        )
        or not manifest.profiles
        or not isinstance(manifest.source_digest, str)
        or len(manifest.source_digest) != 64
        or any(character not in "0123456789abcdef" for character in manifest.source_digest)
    ):
        raise ValueError(
            "managed profile manifest source digest/generation/set is invalid"
        )
    if (
        not manifest.expected_profile_ids
        or len(manifest.profiles) > _MAX_MANAGED_TRACKERS
    ):
        raise ValueError(
            "managed profile manifest is empty or exceeds tracker capacity"
        )
    profiles = tuple(
        sorted(manifest.profiles, key=lambda profile: profile.profile_id)
    )
    profile_ids = tuple(profile.profile_id for profile in profiles)
    if (
        tuple(sorted(manifest.expected_profile_ids)) != profile_ids
        or "default" not in profile_ids
        or len(set(profile_ids)) != len(profile_ids)
    ):
        raise ValueError("managed profile manifest is incomplete")
    paths = []
    for profile in profiles:
        if (
            not isinstance(profile.profile_id, str)
            or not profile.profile_id
            or len(profile.profile_id.encode("utf-8")) > 256
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in profile.profile_id
            )
        ):
            raise ValueError("managed profile identity is invalid")
        path = Path(profile.tracker_path)
        if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
            raise ValueError("managed tracker path must be absolute and canonical")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ValueError("managed profile tracker paths are not unique")
    outbox = Path(outbox_path)
    if (
        not outbox.is_absolute()
        or outbox.parent.resolve(strict=True) != outbox.parent
        or outbox in paths
    ):
        raise ValueError("managed outbox path is invalid")
    return profiles, outbox


def _managed_v2_runtime() -> tuple[int, str, str, str]:
    runtime_generation, process_pid, process_token = _runtime_persistence_owner()
    epoch = (
        f"{process_pid}:{hashlib.sha256(process_token.encode()).hexdigest()[:24]}"
        f":{runtime_generation}"
    )
    return process_pid, process_token, runtime_generation, epoch


def _managed_v2_runtime_current() -> Optional[tuple[int, str, str, str]]:
    """Read the existing runtime epoch without initializing one."""
    pid = os.getpid()
    token = _safe_process_start_token(pid)
    with _runtime_owner_lock:
        if (
            token is None
            or _runtime_owner_pid != pid
            or _runtime_owner_start_token != token
            or not _runtime_owner_id
        ):
            return None
        epoch = (
            f"{pid}:{hashlib.sha256(token.encode()).hexdigest()[:24]}"
            f":{_runtime_owner_id}"
        )
        return pid, token, _runtime_owner_id, epoch


def _managed_v2_event_id(
    profile_id: str,
    generation: str,
    delegation_id: str,
) -> str:
    binding = hashlib.sha256(
        f"{profile_id}\0{generation}\0{delegation_id}".encode("utf-8")
    ).hexdigest()
    return f"async-delegation:{binding}:completion"


def _managed_v2_validate_delegation_id(
    delegation_id: object,
    profile_id: str,
    generation: str,
) -> str:
    if not isinstance(delegation_id, str):
        raise ValueError("managed delegation id is invalid")
    prefix = f"deleg_{profile_id}_{generation}_"
    if not delegation_id.startswith(prefix):
        raise ValueError("managed delegation id does not bind profile generation")
    try:
        uuid.UUID(delegation_id[len(prefix):])
    except (ValueError, TypeError) as exc:
        raise ValueError("managed delegation id lacks a full UUID") from exc
    return delegation_id


def _managed_v2_read_outbox(
    held: Any,
    path: Path,
) -> tuple[Dict[str, Any], Optional[FileIdentity]]:
    raw, identity, _payload = held.read_json(
        path,
        max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return {"version": 2, "events": {}, "tombstones": {}}, None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "events", "tombstones"}
        or raw.get("version") != 2
        or not isinstance(raw.get("events"), dict)
        or not isinstance(raw.get("tombstones"), dict)
        or len(raw["events"]) + len(raw["tombstones"]) > _MAX_MANAGED_RECORDS
    ):
        raise ValueError("managed async delegation outbox schema is invalid")
    allowed_row = {
        "event_id", "delegation_id", "profile_id", "manifest_generation",
        "manifest_source_digest",
        "tracker_path", "tracker_hash", "tracker_identity", "event", "state",
        "created_at", "delivered_at", "last_replay_epoch",
        "tracker_current_hash", "tracker_current_identity",
    }
    events = {}
    for event_id, raw_row in raw["events"].items():
        if (
            not isinstance(event_id, str)
            or not isinstance(raw_row, dict)
            or set(raw_row) != allowed_row
        ):
            raise ValueError("managed outbox event schema is not closed")
        row = dict(raw_row)
        event = row.get("event")
        metadata = (
            event.get("managed_delivery") if isinstance(event, dict) else None
        )
        if (
            row.get("event_id") != event_id
            or row.get("state") not in {"intent", "enqueued", "delivered"}
            or not isinstance(row.get("event"), dict)
            or row["event"].get("managed_event_id") != event_id
            or not isinstance(row.get("tracker_identity"), dict)
            or not isinstance(row.get("tracker_hash"), str)
            or not row.get("tracker_hash")
            or not isinstance(row.get("manifest_source_digest"), str)
            or len(row.get("manifest_source_digest")) != 64
            or not isinstance(row.get("tracker_current_hash"), str)
            or not row.get("tracker_current_hash")
            or not isinstance(row.get("tracker_current_identity"), dict)
            or not isinstance(row.get("last_replay_epoch"), str)
            or not isinstance(row.get("created_at"), (int, float))
            or isinstance(row.get("created_at"), bool)
            or not math.isfinite(float(row.get("created_at")))
            or event.get("type") != "async_delegation"
            or event.get("delegation_id") != row.get("delegation_id")
            or event.get("profile_id") != row.get("profile_id")
            or event.get("profile_generation")
            != row.get("manifest_generation")
            or not isinstance(metadata, dict)
            or set(metadata)
            != {
                "protocol",
                "outbox_path",
                "event_id",
                "profile_id",
                "manifest_generation",
                "manifest_source_digest",
                "tracker_path",
                "tracker_hash",
                "tracker_identity",
                "runtime_generation",
            }
            or metadata.get("protocol") != 2
            or metadata.get("outbox_path") != os.fspath(path)
            or metadata.get("event_id") != event_id
            or metadata.get("profile_id") != row.get("profile_id")
            or metadata.get("manifest_generation")
            != row.get("manifest_generation")
            or metadata.get("manifest_source_digest")
            != row.get("manifest_source_digest")
            or metadata.get("tracker_path") != row.get("tracker_path")
            or metadata.get("tracker_hash") != row.get("tracker_hash")
            or metadata.get("tracker_identity")
            != row.get("tracker_identity")
            or not isinstance(metadata.get("runtime_generation"), str)
            or not metadata.get("runtime_generation")
        ):
            raise ValueError("managed outbox event is invalid")
        delivered_at = row.get("delivered_at")
        if row["state"] == "delivered":
            if (
                not isinstance(delivered_at, (int, float))
                or isinstance(delivered_at, bool)
                or not math.isfinite(float(delivered_at))
            ):
                raise ValueError("managed delivered event timestamp is invalid")
        elif delivered_at is not None:
            raise ValueError("managed undelivered event has delivered timestamp")
        events[event_id] = row
    tombstones = {}
    for event_id, tombstone in raw["tombstones"].items():
        if (
            not isinstance(event_id, str)
            or not isinstance(tombstone, dict)
            or set(tombstone) != {"event_id", "payload_hash", "delivered_at"}
            or tombstone.get("event_id") != event_id
            or not isinstance(tombstone.get("payload_hash"), str)
            or not isinstance(tombstone.get("delivered_at"), (int, float))
            or isinstance(tombstone.get("delivered_at"), bool)
            or not math.isfinite(float(tombstone.get("delivered_at")))
        ):
            raise ValueError("managed outbox tombstone is invalid")
        tombstones[event_id] = dict(tombstone)
    if set(events).intersection(tombstones):
        raise ValueError("managed outbox event/tombstone identity overlaps")
    return {"version": 2, "events": events, "tombstones": tombstones}, identity


def _managed_v2_tracker(
    held: Any,
    profile: ManagedAsyncDelegationProfile,
    generation: str,
    *,
    max_bytes: int = _MAX_PERSISTENCE_BYTES,
) -> tuple[Dict[str, Any], Optional[FileIdentity]]:
    raw, identity, _payload = held.read_json(
        profile.tracker_path,
        max_bytes=max_bytes,
        missing_ok=True,
    )
    if raw is None:
        return {"version": 1, "records": {}}, None
    data = _validate_persisted_data(raw)
    if len(data["records"]) > _MAX_MANAGED_RECORDS:
        raise ValueError("managed tracker record budget exceeded")
    allowed_entry = {
        "delegation_id", "profile_id", "profile_generation", "status",
        "delivery_status", "record", "event", "result", "updated_at",
        "queued_at", "delivered_at",
    }
    for delegation_id, entry in data["records"].items():
        if set(entry) - allowed_entry:
            raise ValueError("managed tracker entry schema is not closed")
        _managed_v2_validate_delegation_id(
            delegation_id, profile.profile_id, generation
        )
        if (
            entry.get("profile_id") != profile.profile_id
            or entry.get("profile_generation") != generation
        ):
            raise ValueError("managed tracker entry profile generation mismatch")
        record = entry.get("record")
        if not isinstance(record, dict):
            raise ValueError("managed tracker record payload is absent")
        if (
            record.get("profile_id") != profile.profile_id
            or record.get("profile_generation") != generation
            or record.get("delegation_id") != delegation_id
            or record.get("status") != entry.get("status")
            or record.get("delivery_status") != entry.get("delivery_status")
        ):
            raise ValueError("managed tracker record generation mismatch")
        status = entry.get("status")
        delivery = entry.get("delivery_status")
        if (
            status not in {"running", *_MANAGED_TERMINAL_STATUSES}
            or delivery not in _DELIVERY_STATUS_RANK
        ):
            raise ValueError("managed tracker status is invalid")
        event = entry.get("event")
        if status in _MANAGED_TERMINAL_STATUSES and (
            not isinstance(event, dict)
            or event.get("type") != "async_delegation"
            or event.get("delegation_id") != delegation_id
            or event.get("profile_id") != profile.profile_id
            or event.get("profile_generation") != generation
            or event.get("status") != status
        ):
            raise ValueError("managed terminal event binding is invalid")
        for timestamp_name in ("updated_at", "queued_at", "delivered_at"):
            timestamp = entry.get(timestamp_name)
            if timestamp is not None and (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(float(timestamp))
            ):
                raise ValueError("managed tracker timestamp is invalid")
    return data, identity


def _managed_v2_receipt(
    base: ManagedAsyncDelegationRecoveryReceipt,
    *,
    runtime: tuple[int, str, str, str],
    manifest_generation: str,
    manifest_source_digest: str,
    classifications: Sequence[tuple[str, str]],
) -> ManagedAsyncDelegationRecoveryReceipt:
    pid, token, runtime_generation, epoch = runtime
    return replace(
        base,
        recovery_epoch=epoch,
        process_pid=pid,
        process_start_token=token,
        runtime_generation=runtime_generation,
        manifest_generation=manifest_generation,
        manifest_source_digest=manifest_source_digest,
        record_classifications=tuple(sorted(classifications)),
    )


def _managed_event_postconditions(
    outbox: Dict[str, Any],
) -> tuple[ManagedAsyncEventPostcondition, ...]:
    result = []
    immutable_fields = (
        "event_id", "delegation_id", "profile_id", "manifest_generation",
        "manifest_source_digest", "tracker_path", "tracker_hash",
        "tracker_identity", "event", "created_at", "last_replay_epoch",
    )
    for event_id, row in sorted(outbox["events"].items()):
        immutable = {name: row[name] for name in immutable_fields}
        result.append(
            ManagedAsyncEventPostcondition(
                event_id=event_id,
                kind="event",
                state=row["state"],
                row_sha256=_managed_json_hash(row),
                event_sha256=_managed_json_hash(row["event"]),
                immutable_sha256=_managed_json_hash(immutable),
                created_at=float(row["created_at"]),
                last_replay_epoch=row["last_replay_epoch"],
            )
        )
    for event_id, row in sorted(outbox["tombstones"].items()):
        result.append(
            ManagedAsyncEventPostcondition(
                event_id=event_id,
                kind="tombstone",
                state="tombstoned",
                row_sha256=_managed_json_hash(row),
                event_sha256=row["payload_hash"],
                immutable_sha256=_managed_json_hash(row),
                created_at=None,
                last_replay_epoch="",
            )
        )
    return tuple(result)


def _managed_recovery_receipt_digest(
    receipt: ManagedAsyncDelegationRecoveryReceipt,
) -> str:
    value = asdict(receipt)
    value["verification_sha256"] = ""
    return _managed_json_hash(value)


@contextmanager
def _managed_v2_authorities(
    paths: Sequence[Path],
    *,
    create_locks: bool = True,
) -> Iterator[ExitStack]:
    # The authoritative profile-count bound limits work. Descriptor capacity is
    # owned by the running process and enforced by acquisition itself; a static
    # cap can reject valid manifests on a process with ample capacity. Keeping
    # construction inside the ExitStack also guarantees partial acquisition is
    # unwound if the OS reports EMFILE or any later authority fails to open.
    with ExitStack() as stack:
        held_by_parent = {}
        for path in paths:
            if path.parent not in held_by_parent:
                held_by_parent[path.parent] = stack.enter_context(
                    hold_private_authority_directory(path)
                )
        stack.held_by_parent = held_by_parent  # type: ignore[attr-defined]
        for path in sorted(paths, key=os.fspath):
            stack.enter_context(
                held_by_parent[path.parent].lock(path, create=create_locks)
            )
        yield stack


def recover_managed_async_delegations_exact(
    manifest: ManagedAsyncDelegationProfileManifest,
    *,
    outbox_path: Path,
    completion_queue: Any,
    crash_hook: Optional[Callable[[str], None]] = None,
) -> ManagedAsyncDelegationRecoveryReceipt:
    """Recover an authoritative profile set through one exact durable outbox."""
    empty_runtime = (0, "", "", "")
    try:
        profiles, outbox = _managed_v2_manifest(manifest, outbox_path)
        runtime = _managed_v2_runtime()
    except Exception as exc:
        base = _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=(),
            outbox=Path(outbox_path),
            epoch="",
            errors=(str(exc),),
        )
        return _managed_v2_receipt(
            base,
            runtime=empty_runtime,
            manifest_generation=getattr(manifest, "generation", ""),
            manifest_source_digest=getattr(manifest, "source_digest", ""),
            classifications=(),
        )
    tracker_paths = tuple(Path(profile.tracker_path) for profile in profiles)
    profile_by_id = {profile.profile_id: profile for profile in profiles}
    before = []
    after = []
    outbox_before = None
    outbox_after = None
    classifications = []
    transitions = []
    delegation_ids = []
    event_ids = []
    queued = []
    deduped = []
    aggregate_bytes = 0
    try:
        with _managed_v2_authorities((*tracker_paths, outbox)) as authorities:
            held_by_parent = authorities.held_by_parent  # type: ignore[attr-defined]
            outbox_data, outbox_identity = _managed_v2_read_outbox(
                held_by_parent[outbox.parent], outbox
            )
            outbox_before = (outbox_data, outbox_identity)
            aggregate_bytes += outbox_identity.size if outbox_identity else 0
            tracker_snapshots = {}
            for profile in profiles:
                tracker = Path(profile.tracker_path)
                remaining_bytes = (
                    _MAX_MANAGED_AGGREGATE_BYTES - aggregate_bytes
                )
                if remaining_bytes <= 0:
                    raise ValueError(
                        "managed aggregate authority budget exceeded"
                    )
                data, identity = _managed_v2_tracker(
                    held_by_parent[tracker.parent],
                    profile,
                    manifest.generation,
                    max_bytes=min(
                        _MAX_PERSISTENCE_BYTES, remaining_bytes
                    ),
                )
                tracker_snapshots[profile.profile_id] = (data, identity)
                before.append((tracker, data, identity))
                aggregate_bytes += identity.size if identity else 0
            total_records = len(outbox_data["events"]) + len(
                outbox_data["tombstones"]
            ) + sum(
                len(data["records"]) for data, _ in tracker_snapshots.values()
            )
            if (
                aggregate_bytes > _MAX_MANAGED_AGGREGATE_BYTES
                or total_records > _MAX_MANAGED_RECORDS
            ):
                raise ValueError("managed aggregate authority budget exceeded")

            now = time.time()
            events = outbox_data["events"]
            tombstones = outbox_data["tombstones"]
            outbox_changed = False
            for event_id, row in list(events.items()):
                if (
                    row["state"] == "delivered"
                    and now - float(row["delivered_at"])
                    > _MANAGED_DELIVERED_RETENTION_SECONDS
                ):
                    tombstones[event_id] = {
                        "event_id": event_id,
                        "payload_hash": _managed_json_hash(row["event"]),
                        "delivered_at": row["delivered_at"],
                    }
                    events.pop(event_id)
                    outbox_changed = True
            for event_id, tombstone in list(tombstones.items()):
                if (
                    now - float(tombstone["delivered_at"])
                    > _MANAGED_TOMBSTONE_RETENTION_SECONDS
                ):
                    tombstones.pop(event_id)
                    outbox_changed = True

            tracker_seen = set()
            for profile in profiles:
                tracker = Path(profile.tracker_path)
                data, identity = tracker_snapshots[profile.profile_id]
                for delegation_id, entry in sorted(data["records"].items()):
                    tracker_seen.add(delegation_id)
                    delegation_ids.append(delegation_id)
                    status = str(entry.get("status") or "")
                    delivery = str(entry.get("delivery_status") or "")
                    if status == "running":
                        record = entry["record"]
                        owner = (
                            record.get("runtime_owner_id"),
                            record.get("runtime_owner_pid"),
                            record.get("runtime_owner_start_token"),
                        )
                        if not all(owner) or not _persistence_owner_is_live(record):
                            raise ValueError(
                                f"running delegation {delegation_id} owner is ambiguous"
                            )
                        classifications.append((delegation_id, "in_progress"))
                        continue
                    event = entry.get("event")
                    if delivery == "delivered":
                        classifications.append((delegation_id, "delivered"))
                        continue
                    if not isinstance(event, dict):
                        raise ValueError(
                            f"terminal delegation {delegation_id} has no event"
                        )
                    event_id = _managed_v2_event_id(
                        profile.profile_id,
                        manifest.generation,
                        delegation_id,
                    )
                    event_ids.append(event_id)
                    durable = events.get(event_id)
                    durable_runtime_generation = runtime[2]
                    if isinstance(durable, dict):
                        durable_metadata = durable.get("event", {}).get(
                            "managed_delivery"
                        )
                        if isinstance(durable_metadata, dict):
                            durable_runtime_generation = durable_metadata.get(
                                "runtime_generation", runtime[2]
                            )
                    managed_event = dict(event)
                    delivery_metadata = {
                        "protocol": 2,
                        "outbox_path": os.fspath(outbox),
                        "event_id": event_id,
                        "profile_id": profile.profile_id,
                        "manifest_generation": manifest.generation,
                        "manifest_source_digest": manifest.source_digest,
                        "tracker_path": os.fspath(tracker),
                        "tracker_hash": _managed_json_hash(data),
                        "tracker_identity": _managed_identity(identity),
                        "runtime_generation": durable_runtime_generation,
                    }
                    managed_event["managed_event_id"] = event_id
                    managed_event["managed_delivery"] = delivery_metadata
                    candidate = {
                        "event_id": event_id,
                        "delegation_id": delegation_id,
                        "profile_id": profile.profile_id,
                        "manifest_generation": manifest.generation,
                        "manifest_source_digest": manifest.source_digest,
                        "tracker_path": os.fspath(tracker),
                        "tracker_hash": _managed_json_hash(data),
                        "tracker_identity": _managed_identity(identity),
                        "event": managed_event,
                        "state": "intent",
                        "created_at": now,
                        "delivered_at": None,
                        "last_replay_epoch": "",
                        "tracker_current_hash": _managed_json_hash(data),
                        "tracker_current_identity": _managed_identity(identity),
                    }
                    tombstone = tombstones.get(event_id)
                    if tombstone is not None:
                        if tombstone["payload_hash"] != _managed_json_hash(
                            managed_event
                        ):
                            raise ValueError("managed tombstone collision")
                        classifications.append((delegation_id, "tombstoned"))
                        continue
                    if durable is None:
                        events[event_id] = candidate
                        outbox_changed = True
                        classifications.append((delegation_id, "intent_created"))
                    else:
                        immutable = {
                            key: durable[key]
                            for key in (
                                "event_id", "delegation_id", "profile_id",
                                "manifest_generation", "tracker_path",
                                "manifest_source_digest",
                                "tracker_hash", "tracker_identity", "event",
                            )
                        }
                        expected = {
                            key: candidate[key] for key in immutable
                        }
                        if immutable != expected:
                            raise ValueError("managed event identity collision")
                        classifications.append((delegation_id, "outbox_bound"))

            final_record_count = len(events) + len(tombstones) + sum(
                len(data["records"]) for data, _ in tracker_snapshots.values()
            )
            if final_record_count > _MAX_MANAGED_RECORDS:
                raise ValueError("managed aggregate record budget exceeded")

            for event_id, row in sorted(events.items()):
                if row["manifest_generation"] != manifest.generation:
                    raise ValueError("outbox row generation is not in manifest")
                if row["manifest_source_digest"] != manifest.source_digest:
                    raise ValueError("outbox row source is not in manifest")
                profile = profile_by_id.get(row["profile_id"])
                if (
                    profile is None
                    or os.fspath(profile.tracker_path) != row["tracker_path"]
                ):
                    raise ValueError("outbox row profile is not authoritative")
                delegation_id = row["delegation_id"]
                _managed_v2_validate_delegation_id(
                    delegation_id,
                    row["profile_id"],
                    manifest.generation,
                )
                if delegation_id not in tracker_seen:
                    raise ValueError(
                        "managed outbox event has no ACK-capable tracker row"
                    )

            if outbox_changed:
                outbox_identity = held_by_parent[outbox.parent].atomic_write_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            if events:
                _managed_crash_hook(crash_hook, "intent_committed")

            for event_id, row in sorted(events.items()):
                if row["state"] == "delivered":
                    deduped.append(event_id)
                    continue
                if row["last_replay_epoch"] == runtime[3]:
                    deduped.append(event_id)
                    continue
                if _managed_queue_event_once(completion_queue, row["event"]):
                    queued.append(event_id)
                else:
                    deduped.append(event_id)
                _managed_crash_hook(crash_hook, "event_enqueued")
                row["state"] = "enqueued"
                row["last_replay_epoch"] = runtime[3]
                outbox_changed = True
            if outbox_changed:
                outbox_identity = held_by_parent[outbox.parent].atomic_write_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            if events:
                _managed_crash_hook(crash_hook, "outbox_enqueued")

            for profile in profiles:
                tracker = Path(profile.tracker_path)
                data, identity = tracker_snapshots[profile.profile_id]
                changed = False
                for delegation_id, entry in data["records"].items():
                    event_id = _managed_v2_event_id(
                        profile.profile_id,
                        manifest.generation,
                        delegation_id,
                    )
                    row = events.get(event_id)
                    new = None
                    if event_id in tombstones:
                        new = "delivered"
                    elif row is not None:
                        new = (
                            "delivered"
                            if row["state"] == "delivered"
                            else "queued"
                        )
                    if (
                        new is not None
                        and _DELIVERY_STATUS_RANK.get(new, -1)
                        > _DELIVERY_STATUS_RANK.get(
                            str(entry.get("delivery_status") or ""), -1
                        )
                    ):
                        old = str(entry.get("delivery_status") or "")
                        entry["delivery_status"] = new
                        entry["record"]["delivery_status"] = new
                        transitions.append((delegation_id, old, new))
                        changed = True
                if changed:
                    identity = held_by_parent[tracker.parent].atomic_write_json(
                        tracker,
                        data,
                        expected=identity,
                        max_bytes=_MAX_PERSISTENCE_BYTES,
                        sort_keys=True,
                    )
                    for row in events.values():
                        if row["tracker_path"] == os.fspath(tracker):
                            row["tracker_current_hash"] = _managed_json_hash(data)
                            row["tracker_current_identity"] = _managed_identity(
                                identity
                            )
                    outbox_identity = held_by_parent[
                        outbox.parent
                    ].atomic_write_json(
                        outbox,
                        outbox_data,
                        expected=outbox_identity,
                        max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                        sort_keys=True,
                    )
                reread, final_identity = _managed_v2_tracker(
                    held_by_parent[tracker.parent],
                    profile,
                    manifest.generation,
                )
                after.append((tracker, reread, final_identity))
            final_outbox, final_outbox_identity = _managed_v2_read_outbox(
                held_by_parent[outbox.parent], outbox
            )
            outbox_after = (final_outbox, final_outbox_identity)
    except Exception as exc:
        if getattr(exc, "_managed_crash_injection", False):
            raise
        base = _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=tracker_paths,
            outbox=outbox,
            epoch=runtime[3],
            before=before,
            after=after,
            outbox_before=outbox_before,
            outbox_after=outbox_after,
            delegation_ids=delegation_ids,
            event_ids=event_ids,
            queued=queued,
            deduped=deduped,
            transitions=transitions,
            errors=(str(exc),),
        )
        return _managed_v2_receipt(
            base,
            runtime=runtime,
            manifest_generation=manifest.generation,
            manifest_source_digest=manifest.source_digest,
            classifications=classifications,
        )
    active = bool(classifications or events or tombstones)
    base = _managed_receipt(
        outcome=(
            ManagedAsyncDelegationRecoveryOutcome.COMPLETE
            if active
            else ManagedAsyncDelegationRecoveryOutcome.ABSENT
        ),
        trackers=tracker_paths,
        outbox=outbox,
        epoch=runtime[3],
        before=before,
        after=after,
        outbox_before=outbox_before,
        outbox_after=outbox_after,
        delegation_ids=tuple(sorted(set(delegation_ids))),
        event_ids=tuple(sorted(set(event_ids))),
        queued=queued,
        deduped=deduped,
        transitions=transitions,
    )
    result = _managed_v2_receipt(
        base,
        runtime=runtime,
        manifest_generation=manifest.generation,
        manifest_source_digest=manifest.source_digest,
        classifications=classifications,
    )
    result = replace(
        result,
        event_postconditions=_managed_event_postconditions(
            outbox_after[0] if outbox_after is not None else {
                "events": {},
                "tombstones": {},
            }
        ),
    )
    return replace(
        result,
        verification_sha256=_managed_recovery_receipt_digest(result),
    )


def verify_managed_async_delegations_exact(
    receipt: ManagedAsyncDelegationRecoveryReceipt,
    manifest: ManagedAsyncDelegationProfileManifest,
    *,
    completion_queue: Any,
) -> ManagedAsyncDelegationVerificationReceipt:
    """Read-only proof of tracker, outbox, queue, ACK, manifest, and epoch state."""
    tracker_hashes: list[tuple[str, Optional[str]]] = []
    tracker_identities: list[tuple[str, Optional[dict]]] = []
    outbox_hash = None
    delegation_ids: list[str] = []
    event_ids: list[str] = []
    queue_event_ids: list[str] = []
    classifications: list[tuple[str, str]] = []
    errors: list[str] = []
    outcome = ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    try:
        if type(receipt) is not ManagedAsyncDelegationRecoveryReceipt:
            raise ValueError("managed async verification receipt type is invalid")
        if (
            not receipt.verification_sha256
            or receipt.verification_sha256
            != _managed_recovery_receipt_digest(receipt)
        ):
            raise ValueError("managed async verification receipt digest mismatch")
        profiles, outbox = _managed_v2_manifest(
            manifest, Path(receipt.outbox_path)
        )
        tracker_paths = tuple(Path(profile.tracker_path) for profile in profiles)
        if (
            tuple(map(os.fspath, tracker_paths)) != receipt.tracker_paths
            or receipt.manifest_generation != manifest.generation
            or receipt.manifest_source_digest != manifest.source_digest
        ):
            raise ValueError("managed async verification manifest mismatch")
        _managed_v2_runtime_current()
        expected_hashes = dict(receipt.tracker_hashes_after)
        expected_identities = dict(receipt.tracker_identities_after)
        if (
            set(expected_hashes) != set(receipt.tracker_paths)
            or set(expected_identities) != set(receipt.tracker_paths)
        ):
            raise ValueError("managed async verification receipt tracker mismatch")

        with _managed_v2_authorities(
            (*tracker_paths, outbox), create_locks=False
        ) as authorities:
            held_by_parent = authorities.held_by_parent  # type: ignore[attr-defined]
            outbox_data, outbox_identity = _managed_v2_read_outbox(
                held_by_parent[outbox.parent], outbox
            )
            outbox_hash = _managed_json_hash(outbox_data)
            outbox_changed = outbox_hash != receipt.outbox_hash_after
            tracker_changed = False
            postconditions = {
                item.event_id: item for item in receipt.event_postconditions
            }
            if (
                len(postconditions) != len(receipt.event_postconditions)
                or set(postconditions)
                != set(outbox_data["events"]) | set(outbox_data["tombstones"])
            ):
                raise ValueError(
                    "managed async verification receipt event set mismatch"
                )
            tracker_data: dict[str, tuple[Dict[str, Any], Optional[FileIdentity]]] = {}
            aggregate_bytes = outbox_identity.size if outbox_identity else 0
            for profile in profiles:
                path = Path(profile.tracker_path)
                remaining = _MAX_MANAGED_AGGREGATE_BYTES - aggregate_bytes
                if remaining <= 0:
                    raise ValueError("managed async verification authority budget exceeded")
                data, identity = _managed_v2_tracker(
                    held_by_parent[path.parent],
                    profile,
                    manifest.generation,
                    max_bytes=min(_MAX_PERSISTENCE_BYTES, remaining),
                )
                digest = _managed_json_hash(data)
                identity_value = _managed_identity(identity)
                tracker_hashes.append((os.fspath(path), digest))
                tracker_identities.append((os.fspath(path), identity_value))
                if (
                    digest != expected_hashes[os.fspath(path)]
                    or identity_value != expected_identities[os.fspath(path)]
                ):
                    tracker_changed = True
                tracker_data[profile.profile_id] = (data, identity)
                aggregate_bytes += identity.size if identity else 0

            records_by_id: dict[str, Dict[str, Any]] = {}
            for profile in profiles:
                data, _identity = tracker_data[profile.profile_id]
                for delegation_id, entry in data["records"].items():
                    if delegation_id in records_by_id:
                        raise ValueError(
                            "managed async verification delegation identity collision"
                        )
                    records_by_id[delegation_id] = entry
                    delegation_ids.append(delegation_id)
            if tuple(sorted(delegation_ids)) != tuple(sorted(receipt.delegation_ids)):
                raise ValueError("managed async verification delegation mismatch")

            with completion_queue.mutex:
                queue_snapshot = tuple(completion_queue.queue)
            queued_by_id: dict[str, Dict[str, Any]] = {}
            for event in queue_snapshot:
                if not isinstance(event, dict):
                    continue
                event_id = event.get("managed_event_id")
                if not isinstance(event_id, str):
                    continue
                if event_id in queued_by_id:
                    raise ValueError(
                        "managed async verification queue identity collision"
                    )
                queued_by_id[event_id] = event

            missing_queue = False
            for event_id, row in sorted(outbox_data["events"].items()):
                event_ids.append(event_id)
                postcondition = postconditions[event_id]
                immutable = {
                    name: row[name]
                    for name in (
                        "event_id", "delegation_id", "profile_id",
                        "manifest_generation", "manifest_source_digest",
                        "tracker_path", "tracker_hash", "tracker_identity",
                        "event", "created_at", "last_replay_epoch",
                    )
                }
                unchanged = (
                    postcondition.kind == "event"
                    and _managed_json_hash(row) == postcondition.row_sha256
                )
                exact_ack = (
                    postcondition.kind == "event"
                    and postcondition.state in {"intent", "enqueued"}
                    and row["state"] == "delivered"
                    and _managed_json_hash(row["event"])
                    == postcondition.event_sha256
                    and _managed_json_hash(immutable)
                    == postcondition.immutable_sha256
                    and row["created_at"] == postcondition.created_at
                    and row["last_replay_epoch"]
                    == postcondition.last_replay_epoch
                )
                if not unchanged and not exact_ack:
                    raise ValueError(
                        "managed async verification event successor is invalid"
                    )
                delegation_id = row["delegation_id"]
                entry = records_by_id.get(delegation_id)
                if entry is None:
                    raise ValueError(
                        "managed async verification outbox lacks tracker"
                    )
                profile_data, profile_identity = tracker_data[row["profile_id"]]
                if (
                    row["tracker_current_hash"]
                    != _managed_json_hash(profile_data)
                    or row["tracker_current_identity"]
                    != _managed_identity(profile_identity)
                ):
                    raise ValueError(
                        "managed async verification tracker binding mismatch"
                    )
                if row["state"] == "delivered":
                    if entry.get("delivery_status") != "delivered":
                        raise ValueError(
                            "managed async verification ACK state mismatch"
                        )
                    classifications.append((delegation_id, "delivered"))
                    continue
                queued = queued_by_id.get(event_id)
                if queued is None:
                    missing_queue = True
                    classifications.append((delegation_id, "queue_missing"))
                elif queued != row["event"]:
                    raise ValueError(
                        "managed async verification queued payload mismatch"
                    )
                else:
                    queue_event_ids.append(event_id)
                    classifications.append((delegation_id, "queued_exact"))
                if entry.get("delivery_status") != "queued":
                    raise ValueError(
                        "managed async verification tracker queue state mismatch"
                    )
            for event_id, tombstone in sorted(outbox_data["tombstones"].items()):
                postcondition = postconditions[event_id]
                if postcondition.kind == "tombstone":
                    valid_tombstone = (
                        _managed_json_hash(tombstone)
                        == postcondition.row_sha256
                    )
                else:
                    valid_tombstone = (
                        postcondition.kind == "event"
                        and tombstone["payload_hash"]
                        == postcondition.event_sha256
                    )
                if not valid_tombstone:
                    raise ValueError(
                        "managed async verification tombstone successor is invalid"
                    )
                matching = [
                    delegation_id
                    for delegation_id in records_by_id
                    if _managed_v2_event_id(
                        records_by_id[delegation_id]["profile_id"],
                        manifest.generation,
                        delegation_id,
                    )
                    == event_id
                ]
                if len(matching) != 1:
                    raise ValueError(
                        "managed async verification tombstone tracker mismatch"
                    )
                delegation_id = matching[0]
                if records_by_id[delegation_id].get("delivery_status") != "delivered":
                    raise ValueError(
                        "managed async verification tombstone ACK mismatch"
                    )
                classifications.append((delegation_id, "tombstoned"))

            receipt_events = set(receipt.event_ids)
            current_events = set(event_ids) | set(outbox_data["tombstones"])
            if not receipt_events.issubset(current_events):
                raise ValueError("managed async verification receipt event mismatch")
            if outbox_changed or tracker_changed:
                advanced = any(
                    (
                        postconditions[event_id].kind == "event"
                        and (
                            event_id in outbox_data["tombstones"]
                            or outbox_data["events"][event_id]["state"]
                            == "delivered"
                        )
                    )
                    for event_id in postconditions
                )
                if not advanced:
                    raise ValueError(
                        "managed async verification authority changed"
                    )
            if missing_queue:
                outcome = ManagedAsyncDelegationRecoveryOutcome.PARTIAL
            elif (
                receipt.outcome is ManagedAsyncDelegationRecoveryOutcome.ABSENT
                and not delegation_ids
                and not current_events
            ):
                outcome = ManagedAsyncDelegationRecoveryOutcome.ABSENT
            else:
                outcome = ManagedAsyncDelegationRecoveryOutcome.COMPLETE
    except Exception as exc:
        errors.append(str(exc))
        outcome = ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS
    return ManagedAsyncDelegationVerificationReceipt(
        outcome=outcome,
        tracker_hashes=tuple(sorted(tracker_hashes)),
        tracker_identities=tuple(sorted(tracker_identities)),
        outbox_hash=outbox_hash,
        delegation_ids=tuple(sorted(delegation_ids)),
        event_ids=tuple(sorted(event_ids)),
        queue_event_ids=tuple(sorted(queue_event_ids)),
        record_classifications=tuple(sorted(classifications)),
        errors=tuple(errors),
    )


def mark_managed_async_delegation_delivered_exact(
    event: Dict[str, Any],
    *,
    crash_hook: Optional[Callable[[str], None]] = None,
) -> ManagedAsyncDelegationRecoveryReceipt:
    """ACK exactly the outbox-bound tracker authority named by a managed event."""
    metadata = event.get("managed_delivery") if isinstance(event, dict) else None
    try:
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {
                "protocol", "outbox_path", "event_id", "profile_id",
                "manifest_generation", "tracker_path", "tracker_hash",
                "manifest_source_digest",
                "tracker_identity", "runtime_generation",
            }
            or metadata.get("protocol") != 2
            or event.get("managed_event_id") != metadata.get("event_id")
        ):
            raise ValueError("managed delivery metadata is invalid")
        outbox = Path(metadata["outbox_path"])
        tracker = Path(metadata["tracker_path"])
        profile = ManagedAsyncDelegationProfile(
            metadata["profile_id"], tracker
        )
        generation = metadata["manifest_generation"]
        source_digest = metadata["manifest_source_digest"]
        runtime = _managed_v2_runtime()
        if (
            not isinstance(profile.profile_id, str)
            or not profile.profile_id
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in profile.profile_id
            )
            or not isinstance(source_digest, str)
            or len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)
            or not isinstance(generation, str)
            or not generation
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in generation
            )
            or not tracker.is_absolute()
            or tracker.parent.resolve(strict=True) != tracker.parent
            or not outbox.is_absolute()
            or outbox.parent.resolve(strict=True) != outbox.parent
            or tracker == outbox
        ):
            raise ValueError("managed delivery authority is invalid")
    except Exception as exc:
        base = _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=(),
            outbox=Path(metadata.get("outbox_path", "/"))
            if isinstance(metadata, dict)
            else Path("/"),
            epoch="",
            errors=(str(exc),),
        )
        return base
    before = []
    after = []
    outbox_before = None
    outbox_after = None
    transitions = []
    delegation_id = str(event.get("delegation_id") or "")
    event_id = metadata["event_id"]
    try:
        with _managed_v2_authorities((tracker, outbox)) as authorities:
            held = authorities.held_by_parent  # type: ignore[attr-defined]
            outbox_data, outbox_identity = _managed_v2_read_outbox(
                held[outbox.parent], outbox
            )
            outbox_before = (outbox_data, outbox_identity)
            row = outbox_data["events"].get(event_id)
            if (
                not isinstance(row, dict)
                or row["event"] != event
                or row["delegation_id"] != delegation_id
                or row["tracker_path"] != os.fspath(tracker)
                or row["profile_id"] != profile.profile_id
                or row["manifest_generation"] != generation
                or row["manifest_source_digest"] != source_digest
                or row["tracker_hash"] != metadata["tracker_hash"]
                or row["tracker_identity"] != metadata["tracker_identity"]
            ):
                raise ValueError("managed ACK does not match durable binding")
            data, identity = _managed_v2_tracker(held[tracker.parent], profile, generation)
            before.append((tracker, data, identity))
            if (
                _managed_json_hash(data) != row["tracker_current_hash"]
                or _managed_identity(identity) != row["tracker_current_identity"]
            ):
                raise ValueError("managed ACK tracker authority changed")
            entry = data["records"].get(delegation_id)
            if not isinstance(entry, dict):
                raise ValueError("managed ACK tracker record is absent")
            if row["state"] != "delivered":
                row["state"] = "delivered"
                row["delivered_at"] = time.time()
                outbox_identity = held[outbox.parent].atomic_write_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            _managed_crash_hook(crash_hook, "delivered_committed")
            old = str(entry.get("delivery_status") or "")
            if old != "delivered":
                entry["delivery_status"] = "delivered"
                entry["record"]["delivery_status"] = "delivered"
                transitions.append((delegation_id, old, "delivered"))
                identity = held[tracker.parent].atomic_write_json(
                    tracker,
                    data,
                    expected=identity,
                    max_bytes=_MAX_PERSISTENCE_BYTES,
                    sort_keys=True,
                )
                row["tracker_current_hash"] = _managed_json_hash(data)
                row["tracker_current_identity"] = _managed_identity(identity)
                outbox_identity = held[outbox.parent].atomic_write_json(
                    outbox,
                    outbox_data,
                    expected=outbox_identity,
                    max_bytes=_MAX_MANAGED_OUTBOX_BYTES,
                    sort_keys=True,
                )
            reread, final_identity = _managed_v2_tracker(
                held[tracker.parent], profile, generation
            )
            after.append((tracker, reread, final_identity))
            final_outbox, final_outbox_identity = _managed_v2_read_outbox(
                held[outbox.parent], outbox
            )
            outbox_after = (final_outbox, final_outbox_identity)
    except Exception as exc:
        if getattr(exc, "_managed_crash_injection", False):
            raise
        base = _managed_receipt(
            outcome=ManagedAsyncDelegationRecoveryOutcome.AMBIGUOUS,
            trackers=(tracker,),
            outbox=outbox,
            epoch=runtime[3],
            before=before,
            after=after,
            outbox_before=outbox_before,
            outbox_after=outbox_after,
            delegation_ids=(delegation_id,),
            event_ids=(event_id,),
            transitions=transitions,
            errors=(str(exc),),
        )
        return _managed_v2_receipt(
            base,
            runtime=runtime,
            manifest_generation=generation,
            manifest_source_digest=source_digest,
            classifications=((delegation_id, "ack_ambiguous"),),
        )
    base = _managed_receipt(
        outcome=ManagedAsyncDelegationRecoveryOutcome.COMPLETE,
        trackers=(tracker,),
        outbox=outbox,
        epoch=runtime[3],
        before=before,
        after=after,
        outbox_before=outbox_before,
        outbox_after=outbox_after,
        delegation_ids=(delegation_id,),
        event_ids=(event_id,),
        transitions=transitions,
    )
    return _managed_v2_receipt(
        base,
        runtime=runtime,
        manifest_generation=generation,
        manifest_source_digest=source_digest,
        classifications=((delegation_id, "delivered"),),
    )


def _publish_completion_once(process_registry: Any, delegation_id: str, evt: dict) -> bool:
    """Publish one durable event at most once in this process."""
    with _replay_ids_lock:
        if delegation_id in _replayed_persisted_ids:
            return False
        _replayed_persisted_ids.add(delegation_id)
        try:
            process_registry.completion_queue.put(evt)
        except Exception:
            _replayed_persisted_ids.discard(delegation_id)
            raise
    return True


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
            if record.get("status") == "running"
            or record.get("delivery_status") == "finalizing"
        )


def _new_delegation_id() -> str:
    return f"deleg_default_legacy_{uuid.uuid4()}"


def _safe_process_start_token(pid: int) -> Optional[str]:
    try:
        from gateway.status import get_process_start_token

        token = get_process_start_token(pid)
        return token if isinstance(token, str) and token else None
    except Exception:
        return None


def _runtime_persistence_owner() -> tuple[str, int, str]:
    """Return a fork-safe exact identity for this async supervisor runtime."""
    global _runtime_owner_id, _runtime_owner_pid
    global _runtime_owner_start_token
    pid = os.getpid()
    token = _safe_process_start_token(pid)
    if token is None:
        raise RuntimeError("async delegation runtime identity is unavailable")
    with _runtime_owner_lock:
        if (
            _runtime_owner_pid != pid
            or _runtime_owner_start_token != token
            or not _runtime_owner_id
        ):
            _runtime_owner_id = f"runtime_{uuid.uuid4().hex}"
            _runtime_owner_pid = pid
            _runtime_owner_start_token = token
        return (
            _runtime_owner_id,
            _runtime_owner_pid,
            _runtime_owner_start_token,
        )


def _persistence_owner_is_live(record: Dict[str, Any]) -> bool:
    owner_pid = record.get("runtime_owner_pid")
    owner_token = record.get("runtime_owner_start_token")
    if (
        isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 1
        or not isinstance(owner_token, str)
        or not owner_token
    ):
        return False
    try:
        from gateway.status import _pid_exists

        return (
            _pid_exists(owner_pid)
            and _safe_process_start_token(owner_pid) == owner_token
        )
    except Exception:
        return False


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


def _persistence_path() -> Path:
    try:
        from hermes_cli.config import get_hermes_home

        home = get_hermes_home()
    except Exception:
        home = Path(os.path.expanduser("~/.hermes"))
    # Resolve the trusted parent only. Resolving the full leaf would follow a
    # pre-existing symlink before the O_NOFOLLOW read can reject it.
    return Path(home).resolve() / "async_delegations.json"


def _validate_persisted_data(raw: object) -> Dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _PERSISTENCE_VERSION
        or not isinstance(raw.get("records"), dict)
    ):
        raise ValueError("async delegation authority schema is invalid")
    records: Dict[str, Dict[str, Any]] = {}
    for delegation_id, raw_entry in raw["records"].items():
        if (
            not isinstance(delegation_id, str)
            or not delegation_id
            or not isinstance(raw_entry, dict)
        ):
            raise ValueError("async delegation authority record is invalid")
        entry = dict(raw_entry)
        entry_id = entry.get("delegation_id")
        if entry_id is not None and entry_id != delegation_id:
            raise ValueError(
                "async delegation authority record identity is invalid"
            )
        record = entry.get("record")
        if record is not None:
            if not isinstance(record, dict):
                raise ValueError(
                    "async delegation authority payload is invalid"
                )
            record_id = record.get("delegation_id")
            if record_id is not None and record_id != delegation_id:
                raise ValueError(
                    "async delegation authority payload identity is invalid"
                )
            owner_values = (
                record.get("runtime_owner_id"),
                record.get("runtime_owner_pid"),
                record.get("runtime_owner_start_token"),
            )
            owner_present = [value is not None for value in owner_values]
            if any(owner_present) and not all(owner_present):
                raise ValueError(
                    "async delegation runtime owner identity is incomplete"
                )
            if all(owner_present):
                owner_id, owner_pid, owner_token = owner_values
                if (
                    not isinstance(owner_id, str)
                    or not owner_id
                    or isinstance(owner_pid, bool)
                    or not isinstance(owner_pid, int)
                    or owner_pid <= 1
                    or not isinstance(owner_token, str)
                    or not owner_token
                ):
                    raise ValueError(
                        "async delegation runtime owner identity is invalid"
                    )
        event = entry.get("event")
        if event is not None:
            if (
                not isinstance(event, dict)
                or event.get("delegation_id") != delegation_id
            ):
                raise ValueError(
                    "async delegation authority event identity is invalid"
                )
        result = entry.get("result")
        if result is not None and not isinstance(result, dict):
            raise ValueError("async delegation authority result is invalid")
        delivery_status = entry.get("delivery_status")
        if (
            delivery_status is not None
            and delivery_status not in _DELIVERY_STATUS_RANK
        ):
            raise ValueError(
                "async delegation authority delivery state is invalid"
            )
        records[delegation_id] = entry
    return {"version": _PERSISTENCE_VERSION, "records": records}


def _read_persisted_snapshot_unlocked(
) -> tuple[Dict[str, Any], Optional[FileIdentity]]:
    path = _persistence_path()
    raw, identity = read_private_json(
        path,
        max_bytes=_MAX_PERSISTENCE_BYTES,
        missing_ok=True,
    )
    if raw is None and identity is None:
        return {"version": _PERSISTENCE_VERSION, "records": {}}, None
    return _validate_persisted_data(raw), identity


def _read_persisted_unlocked() -> Dict[str, Any]:
    data, _identity = _read_persisted_snapshot_unlocked()
    return data


def _write_persisted_unlocked(
    data: Dict[str, Any],
    *,
    expected: object = _UNSPECIFIED_IDENTITY,
) -> None:
    path = _persistence_path()
    validated = _validate_persisted_data(data)
    if expected is _UNSPECIFIED_IDENTITY:
        with interprocess_authority_lock(path):
            _current, current_identity = _read_persisted_snapshot_unlocked()
            atomic_write_private_json(
                path,
                validated,
                expected=current_identity,
                max_bytes=_MAX_PERSISTENCE_BYTES,
                sort_keys=True,
            )
        return
    if expected is not None and not isinstance(expected, FileIdentity):
        raise TypeError("async delegation expected identity is invalid")
    atomic_write_private_json(
        path,
        validated,
        expected=expected,
        max_bytes=_MAX_PERSISTENCE_BYTES,
        sort_keys=True,
    )


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


def _merge_persisted_entry(
    existing: object,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge one delegation update without regressing terminal/delivery state."""
    if not isinstance(existing, dict):
        return dict(candidate)

    merged = dict(existing)
    merged.update(candidate)
    existing_status = str(existing.get("status") or "")
    candidate_status = str(candidate.get("status") or "")
    existing_terminal = bool(existing_status and existing_status != "running")
    if existing_terminal and candidate_status == "running":
        merged["status"] = existing_status
        if isinstance(existing.get("record"), dict):
            merged["record"] = dict(existing["record"])

    existing_delivery = str(existing.get("delivery_status") or "")
    candidate_delivery = str(candidate.get("delivery_status") or "")
    if (
        _DELIVERY_STATUS_RANK.get(existing_delivery, -1)
        > _DELIVERY_STATUS_RANK.get(candidate_delivery, -1)
    ):
        merged["delivery_status"] = existing_delivery
        for timestamp_name in ("queued_at", "delivered_at"):
            if timestamp_name in existing:
                merged[timestamp_name] = existing[timestamp_name]

    for payload_name in ("result", "event"):
        if payload_name not in candidate and payload_name in existing:
            merged[payload_name] = existing[payload_name]

    record = merged.get("record")
    delivery_status = str(merged.get("delivery_status") or "")
    if isinstance(record, dict) and delivery_status in {
        "pending",
        "queued",
        "delivered",
    }:
        record = dict(record)
        record["delivery_status"] = delivery_status
        merged["record"] = record
    return merged


def _persist_record(
    record: Dict[str, Any],
    *,
    result: Optional[Dict[str, Any]] = None,
    event: Optional[Dict[str, Any]] = None,
    delivery_status: Optional[str] = None,
) -> bool:
    """Durably merge one async delegation record into the global authority."""
    delegation_id = str(record.get("delegation_id") or "")
    if not delegation_id:
        return False
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
            with interprocess_authority_lock(_persistence_path()):
                data, expected = _read_persisted_snapshot_unlocked()
                existing = data["records"].get(delegation_id)
                data["records"][delegation_id] = _merge_persisted_entry(
                    existing,
                    entry,
                )
                _cleanup_persisted_data_locked(data, now=now)
                _write_persisted_unlocked(data, expected=expected)
        return True
    except Exception as exc:
        logger.warning("Failed to persist async delegation %s: %s", delegation_id, exc)
        return False


def _mark_persisted_delivery(delegation_id: str, status: str) -> bool:
    if (
        not delegation_id
        or status not in _DELIVERY_STATUS_RANK
        or status in {"", "running", "finalizing"}
    ):
        return False
    now = time.time()
    try:
        with _persist_lock:
            with interprocess_authority_lock(_persistence_path()):
                data, expected = _read_persisted_snapshot_unlocked()
                entry = data["records"].get(delegation_id)
                if not isinstance(entry, dict):
                    return False
                candidate = dict(entry)
                candidate["delivery_status"] = status
                candidate["updated_at"] = now
                if status == "queued":
                    candidate["queued_at"] = now
                elif status == "delivered":
                    candidate["delivered_at"] = now
                data["records"][delegation_id] = _merge_persisted_entry(
                    entry,
                    candidate,
                )
                _cleanup_persisted_data_locked(data, now=now)
                _write_persisted_unlocked(data, expected=expected)
        return True
    except Exception as exc:
        logger.warning("Failed to update async delegation delivery %s: %s", delegation_id, exc)
        return False


def mark_async_delegation_delivered(evt: Dict[str, Any]) -> bool:
    """Durably ACK an async-delegation notification consumed by a driver."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        return False
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return False
    if not _mark_persisted_delivery(delegation_id, "delivered"):
        return False
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["delivery_status"] = "delivered"
            record["delivered_at"] = time.time()
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


def recover_async_delegations() -> Dict[str, Any]:
    """Replay undelivered completions and mark previous-process runners lost."""
    global _recovery_attempted
    queued = 0
    lost = 0
    now = time.time()
    to_publish: List[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    try:
        from tools.process_registry import process_registry
    except Exception as exc:
        return {"queued": 0, "lost": 0, "error": str(exc)}
    try:
        with _persist_lock:
            with interprocess_authority_lock(_persistence_path()):
                data, expected = _read_persisted_snapshot_unlocked()
                records = data["records"]
                for rid, entry in records.items():
                    record = (
                        entry.get("record")
                        if isinstance(entry.get("record"), dict)
                        else {}
                    )
                    status = str(
                        entry.get("status") or record.get("status") or ""
                    )
                    delivery_status = str(
                        entry.get("delivery_status") or ""
                    )
                    event = (
                        entry.get("event")
                        if isinstance(entry.get("event"), dict)
                        else None
                    )
                    if status == "running":
                        owner_values = (
                            record.get("runtime_owner_id"),
                            record.get("runtime_owner_pid"),
                            record.get("runtime_owner_start_token"),
                        )
                        if all(value is not None for value in owner_values):
                            if _persistence_owner_is_live(record):
                                # Another live Hermes runtime still owns this
                                # child. Preserve it; startup recovery may only
                                # declare work lost after exact owner death.
                                continue
                        else:
                            logger.warning(
                                "Preserving legacy running async delegation %s: "
                                "runtime owner identity is unavailable",
                                rid,
                            )
                            continue
                        record = dict(record)
                        record["status"] = "lost"
                        record["completed_at"] = now
                        record["last_heartbeat_at"] = (
                            record.get("last_heartbeat_at")
                            or record.get("dispatched_at")
                            or now
                        )
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
                    if (
                        status != "running"
                        and delivery_status != "delivered"
                        and event
                    ):
                        entry["delivery_status"] = "queued"
                        entry["queued_at"] = now
                        entry["updated_at"] = now
                        queued += 1
                        restored = dict(record)
                        restored["delivery_status"] = "queued"
                        to_publish.append((rid, dict(event), restored))
                removed = _cleanup_persisted_data_locked(data, now=now)
                if lost or queued or removed:
                    _write_persisted_unlocked(data, expected=expected)
    except Exception as exc:
        logger.warning("Failed to recover async delegation authority: %s", exc)
        return {"queued": 0, "lost": 0, "error": str(exc)}

    published = 0
    for rid, event, restored in to_publish:
        if not _publish_completion_once(process_registry, rid, event):
            continue
        published += 1
        with _records_lock:
            if rid not in _records:
                _records[rid] = restored
    _recovery_attempted = True
    return {"queued": published, "lost": lost}


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
        with interprocess_authority_lock(_persistence_path()):
            data, expected = _read_persisted_snapshot_unlocked()
            persisted_before = len(data["records"])
            persisted_removed = _cleanup_persisted_data_locked(data, now=now)
            persisted_after = len(data["records"])
            if persisted_removed:
                _write_persisted_unlocked(data, expected=expected)
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
        if k
        not in {
            "interrupt_fn",
            "heartbeat_stop",
            "runtime_owner_id",
            "runtime_owner_pid",
            "runtime_owner_start_token",
        }
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
    try:
        owner_id, owner_pid, owner_start_token = _runtime_persistence_owner()
    except Exception as exc:
        return {
            "status": "rejected",
            "error": f"Async delegation runtime identity is unavailable: {exc}",
        }
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
        "runtime_owner_id": owner_id,
        "runtime_owner_pid": owner_pid,
        "runtime_owner_start_token": owner_start_token,
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
    if not _persist_record(record, delivery_status="running"):
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": "Async delegation durable dispatch could not be committed.",
        }

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
        if not _persist_record(
            record,
            result=result,
            event=evt,
            delivery_status="pending",
        ):
            raise OSError("async delegation completion persistence failed")
        if not _mark_persisted_delivery(delegation_id, "queued"):
            raise OSError("async delegation queued state persistence failed")
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "queued"
                live["queued_at"] = time.time()
        # Publish only after the durable record says queued, so a fast
        # consumer cannot ACK a completion whose persistence still says pending.
        _publish_completion_once(process_registry, delegation_id, evt)
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
    _recover_once()

    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    try:
        owner_id, owner_pid, owner_start_token = _runtime_persistence_owner()
    except Exception as exc:
        return {
            "status": "rejected",
            "error": (
                f"Async delegation batch runtime identity is unavailable: {exc}"
            ),
        }
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
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "last_heartbeat_at": dispatched_at,
        "heartbeat_count": 0,
        "delivery_status": "running",
        "runtime_owner_id": owner_id,
        "runtime_owner_pid": owner_pid,
        "runtime_owner_start_token": owner_start_token,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
    }
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
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record
    if not _persist_record(record, delivery_status="running"):
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": "Async delegation durable dispatch could not be committed.",
        }

    heartbeat_stop = _start_heartbeat_thread(delegation_id)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["heartbeat_stop"] = heartbeat_stop

    executor = _get_executor(max_async_children)

    def _worker() -> None:
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
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            failed_record = _records.pop(delegation_id, None)
        if failed_record:
            hb_stop = failed_record.get("heartbeat_stop")
            if hasattr(hb_stop, "set"):
                hb_stop.set()
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

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
        "parent_session_id": event_record.get("parent_session_id"),
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
    try:
        if not _persist_record(
            event_record,
            result=combined,
            event=evt,
            delivery_status="pending",
        ):
            raise OSError("async delegation batch completion persistence failed")
        if not _mark_persisted_delivery(delegation_id, "queued"):
            raise OSError(
                "async delegation batch queued state persistence failed"
            )
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "queued"
                live["queued_at"] = time.time()
        _publish_completion_once(process_registry, delegation_id, evt)
    except Exception as exc:  # pragma: no cover
        _mark_persisted_delivery(delegation_id, "pending")
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
    with _replay_ids_lock:
        _replayed_persisted_ids.clear()
    try:
        _persistence_path().unlink(missing_ok=True)
    except TypeError:
        path = _persistence_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass
