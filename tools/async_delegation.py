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
import inspect
import logging
import os
import queue
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

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
_DB_LOCK = threading.Lock()
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
_MAX_DELIVERY_ATTEMPTS = 8
_DELIVERY_CLAIM_LEASE_SECONDS = 300.0
# Staleness cap for restart replay: a pending completion older than this is
# terminally dropped instead of re-run as a fresh full-context turn (see
# restore_undelivered_completions). 48h keeps overnight/weekend results
# deliverable while stopping weeks-old sessions from replaying after upgrades.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0

_DEFAULT_MAX_ASYNC_CHILDREN = 3
_ACTIVE_STATUSES = frozenset({
    "scheduled", "running", "stalling", "interrupting", "finalizing",
})
_DURABLE_NONTERMINAL_STATUSES = frozenset({
    "review_waiting", "review_requeued",
})
_STATUS_PHASE = {
    "intent": 0,
    "scheduled": 1,
    "running": 2,
    "stalling": 3,
    "interrupting": 4,
    "finalizing": 5,
    "review_waiting": 6,
    "review_requeued": 6,
    "interrupted": 7,
}
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
# Age/size retention defaults for terminal async delegation records. Active
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
# Deterministic dispatch IDs are admitted under a stable striped lock. This
# serializes the full intent→scheduled→running acceptance handshake for one ID
# without retaining an unbounded lock per historical delegation.
_DISPATCH_ADMISSION_LOCKS = tuple(threading.RLock() for _ in range(64))
_RUNNING_CHECKPOINT_TIMEOUT_SECONDS = 10.0
_recovery_attempted = False
_replayed_persisted_ids: set[tuple[str, str]] = set()
_PERSISTENCE_VERSION = 1
_BESTPLAN_TERMINALIZATION_PENDING = "bestplan_terminalization_pending"
_OWNED_REVIEW_RECOVERY_QUEUE = object()
_bestplan_review_recovery_queue: queue.Queue = queue.Queue()
_bestplan_review_recovery_wake = threading.Event()
_bestplan_review_recovery_thread: threading.Thread | None = None
_bestplan_review_recovery_thread_lock = threading.Lock()
_manual_review_recovery_queue: queue.Queue = queue.Queue()
_manual_review_recovery_wake = threading.Event()
_manual_review_recovery_thread: threading.Thread | None = None
_manual_review_recovery_thread_lock = threading.Lock()
_manual_review_recovery_pending: set[tuple[str, str]] = set()
_manual_review_recovery_pending_lock = threading.Lock()


def _is_retained_nonterminal(
    record: Mapping[str, Any], *, status: object | None = None,
) -> bool:
    """Keep every durable work phase out of terminal retention cleanup."""

    phase = str(record.get("status") if status is None else status)
    return bool(
        phase == "intent"
        or phase in _ACTIVE_STATUSES
        or phase in _DURABLE_NONTERMINAL_STATUSES
    )
# Lightweight liveness ping for status consumers (/agents, TUI/Desktop
# delegation.status). Completion delivery still rides the shared process queue;
# this heartbeat only proves that the async-delegation supervisor in this
# process still owns the handle, so a UI can distinguish "still running" from
# "no record / likely lost with process restart" without waiting for the final
# re-entry event.
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_HEARTBEAT_STALE_SECONDS = _HEARTBEAT_INTERVAL_SECONDS * 3

# Managed WebUI startup imports these immutable receipt types from the Agent
# checkout.  Keep the public wire types available across the Agent's durable
# tracker implementation changes; the WebUI receipt codec deliberately
# serializes nested frozen dataclasses and enums without importing internals.
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
    event_postconditions: tuple[ManagedAsyncEventPostcondition, ...] = ()
    verification_sha256: str = ""
    errors: tuple[str, ...] = ()

# Progress-based stale-delegation detection. A frozen progress token is
# interrupted first, then force-finalized after a grace window if the runner
# never returns. Legitimate long-running children that keep changing their
# token are not wall-clock timed out.
_STALE_CHECK_INTERVAL = 30.0
_STALE_IDLE_SECONDS = 450.0
_STALE_IN_TOOL_SECONDS = 1200.0
_STALL_GRACE_SECONDS = 120.0
_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (async_delegation)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        # Raw api_server session id (X-Hermes-Session-Id) of the ORIGINATING
        # request — the wake self-post target. Without persisting it,
        # completions recovered after a process restart are unroutable on
        # api_server (the in-memory record that carried it is gone).
        ("origin_session_id", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every durable dispatch, completion, and delivery-claim, deferring the close
    to the garbage collector. On a long-running gateway that exhausts
    ``RLIMIT_NOFILE`` (the cron-ledger sibling of this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot the dispatching turn's routing origin for the completion event.

    Captured on the PARENT thread at dispatch time (the daemon worker doesn't
    carry the contextvars) and persisted with the durable record, so a
    completion replayed after a restart can reconstruct a full SessionSource
    even when the session-store origin and in-memory source cache are gone.
    scope_id matters most: on a relay-fronted deployment the connector's
    fail-closed egress guard needs the tenant discriminator (or a user
    binding) to route a scoped reply; without it, post-restart scoped
    completions bounce with "target not routed to an onboarded tenant"
    (staging 2026-08-09 defect #4). Best-effort — empty values are simply
    omitted so CLI/contextvar-unaware paths persist nothing new.
    """
    origin: Dict[str, Any] = {}
    try:
        from gateway.session_context import get_session_env

        for evt_key, env_name in (
            ("scope_id", "HERMES_SESSION_SCOPE_ID"),
            ("user_id", "HERMES_SESSION_USER_ID"),
            ("user_name", "HERMES_SESSION_USER_NAME"),
        ):
            value = get_session_env(env_name, "")
            if value:
                origin[evt_key] = value
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        pass
    return origin


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal", "goals", "context", "toolsets", "role", "model", "is_batch",
            # Routing origin (scope_id/user_id/user_name): persisted so a
            # restart-recovered completion can reconstruct a full
            # SessionSource — see _capture_routing_origin.
            "scope_id", "user_id", "user_name",
        )
        if key in record
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload),
             record.get("origin_session_id", "")),
        )
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('running','finalizing')"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            (delegation_id, session_key, origin_ui, parent_id, dispatched_at,
             pid, started, task_json, origin_session_id) = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                # Restore the durable wake target so completions recovered
                # after a restart remain routable to api_server sessions.
                "origin_session_id": origin_session_id or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            # Routing origin persisted at dispatch (see _capture_routing_origin):
            # restores scope_id/user_id for the reconstructed SessionSource so
            # relay egress priming works after a restart.
            for _k in ("scope_id", "user_id", "user_name"):
                if task.get(_k):
                    event[_k] = task[_k]
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).

    Staleness cap: a pending completion older than
    ``_MAX_COMPLETION_REPLAY_AGE_S`` is terminally dropped instead of
    replayed. Replaying a weeks-old completion re-runs its parent session as
    a full-context turn (a July session replayed in August burned a
    102K-token context on the staging fleet) for a result nobody is waiting
    on anymore; the payload stays queryable on the dropped row.
    """
    recover_abandoned_delegations()
    now = time.time()
    restored = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for delegation_id, payload, completed_at, dispatched_at in rows:
            age_basis = completed_at or dispatched_at
            if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
                conn.execute(
                    """UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""",
                    (now, delegation_id),
                )
                logger.warning(
                    "Async delegation %s: pending completion is %.1fh old "
                    "(cap %.1fh); terminally dropping the replay (result "
                    "remains queryable).",
                    delegation_id, (now - age_basis) / 3600.0,
                    _MAX_COMPLETION_REPLAY_AGE_S / 3600.0,
                )
                continue
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
            restored += 1
    return restored


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
    }


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
    """Number of async delegation UNITS still active or finishing delivery.

    A unit is one dispatch: a single subagent OR a whole fan-out batch. A batch
    counts as ONE here because it occupies one async-pool slot (the capacity
    semantics ``dispatch_async_delegation_batch`` relies on). For the count of
    actual concurrent child subagents (batch expanded), use
    ``active_task_count()``.
    """
    with _records_lock:
        return sum(
            1
            for record in _records.values()
            if record.get("status") in _ACTIVE_STATUSES
            or record.get("delivery_status") == "finalizing"
        )


def active_for_session(origin_ui_session_id: str) -> int:
    """Number of live async delegations owned by one UI session."""
    if not origin_ui_session_id:
        return 0
    with _records_lock:
        return sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
            and str(r.get("origin_ui_session_id") or "")
            == origin_ui_session_id
        )


def active_task_count() -> int:
    """Number of async delegation TASKS (child subagents) currently running.

    Unlike ``active_count()`` (units/slots), this expands a batch to its child
    count: a running batch of N tasks contributes N, a single subagent
    contributes 1. This is the truthful "how many subagents are actually
    working right now" figure for observability, where a 3-task batch shown as
    "1" undercounts real concurrent work. Falls back to counting a batch as 1
    if its goal list is missing.
    """
    with _records_lock:
        total = 0
        for r in _records.values():
            if r.get("status") not in {"running", "finalizing"}:
                continue
            if r.get("is_batch"):
                goals = r.get("goals")
                total += len(goals) if isinstance(goals, (list, tuple)) and goals else 1
            else:
                total += 1
        return total


def _matches_session_selectors(
    record: Dict[str, Any],
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    return (
        (origin_ui_session_id and str(record.get("origin_ui_session_id") or "") == origin_ui_session_id)
        or (session_key and str(record.get("session_key") or "") == session_key)
        or (parent_session_id and str(record.get("parent_session_id") or "") == parent_session_id)
    )


def has_live_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    """Whether a session still owns any live async delegation.

    Live = running / stalling / finalizing — the same states the reapers'
    keepalive treats as active work.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return False
    with _records_lock:
        return any(
            r.get("status") in {"running", "stalling", "finalizing"}
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
            for r in _records.values()
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _current_origin_session_id() -> str:
    """Return the raw API-server session id of the originating request.

    Child-agent construction can overwrite the ordinary session context. The
    request-scoped chat-id binding remains stable and is the safe wake target.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return ""
        return get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""


def _pid_exists(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
    except (ValueError, TypeError):
        return False
    try:
        from gateway.status import _pid_exists as status_pid_exists

        return bool(status_pid_exists(pid))
    except Exception:
        return False


def _process_start_time(pid: int) -> Optional[int]:
    """Return the stable process-start fingerprint used to guard PID reuse."""
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_liveness(record: Dict[str, Any]) -> Optional[bool]:
    """Return True/False for proved liveness, None when identity is unknown."""
    raw_pid = record.get("owner_pid")
    if raw_pid in (None, ""):
        return None
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if not _pid_exists(pid):
        return False
    expected_started_at = record.get("owner_started_at")
    if expected_started_at is None:
        # A PID alone is not an ownership identity: it may have been reused,
        # including by a fresh Hermes process with the same PID. Preserve the
        # record without replaying or orphaning it until process-start evidence
        # is available.
        return None
    current_started_at = _process_start_time(pid)
    if current_started_at is None:
        return None
    try:
        return int(current_started_at) == int(expected_started_at)
    except (TypeError, ValueError):
        return False


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if not _is_retained_nonterminal(r)
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
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True, indent=2, default=str
    )
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _db_path() -> Path:
    """Return the legacy SQLite ledger path used by durable delivery APIs."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "state.db"
    except Exception:
        return Path(os.path.expanduser("~/.hermes")) / "state.db"


def _initialize_schema(conn: sqlite3.Connection) -> None:
    try:
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="state.db (async_delegation)")
    except Exception:
        # Journaling is an optimization; schema creation remains authoritative.
        logger.debug("Async delegation WAL setup failed", exc_info=True)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        ("origin_session_id", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")


def _connect() -> sqlite3.Connection:
    path = Path(_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Commit or roll back and always close the legacy ledger connection."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _persist_dispatch(record: Dict[str, Any]) -> None:
    """Persist the compatibility dispatch shape to the SQLite ledger."""
    now = time.time()
    try:
        from gateway.status import get_process_start_time

        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
        if key in record
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (
                record["delegation_id"],
                str(record.get("session_key", "") or ""),
                str(record.get("origin_ui_session_id", "") or ""),
                record.get("parent_session_id"),
                record["dispatched_at"],
                now,
                os.getpid(),
                owner_started_at,
                json.dumps(task_payload, default=str),
                str(record.get("origin_session_id", "") or ""),
            ),
        )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (
                event.get("status", "completed"),
                event.get("completed_at", now),
                now,
                json.dumps(event, default=str),
                json.dumps(result, default=str),
                event["delegation_id"],
            ),
        )


def recover_abandoned_delegations() -> int:
    """Classify SQLite records whose owner process disappeared."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            (
                delegation_id,
                session_key,
                origin_ui,
                parent_id,
                dispatched_at,
                pid,
                started,
                task_json,
                origin_session_id,
            ) = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            try:
                task = json.loads(task_json or "{}")
            except (TypeError, ValueError):
                task = {}
            event = {
                "type": "async_delegation",
                "delegation_id": delegation_id,
                "session_key": session_key,
                "origin_ui_session_id": origin_ui,
                "origin_session_id": origin_session_id or "",
                "parent_session_id": parent_id,
                "goal": task.get("goal", ""),
                "goals": task.get("goals"),
                "context": task.get("context"),
                "toolsets": task.get("toolsets"),
                "role": task.get("role"),
                "model": task.get("model"),
                "is_batch": bool(task.get("is_batch")),
                "status": "unknown",
                "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at,
                "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (
                    now,
                    now,
                    json.dumps(event, default=str),
                    json.dumps(result, default=str),
                    delegation_id,
                ),
            )
            recovered += 1
    return recovered


def _restore_sqlite_undelivered(target_queue) -> int:
    recover_abandoned_delegations()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending'
                 AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
    restored = 0
    for (payload,) in rows:
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            event["restored"] = True
            target_queue.put(event)
            restored += 1
    return restored


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    # The JSON tracker is the authoritative store for current delegations.
    # Keep this read API aligned with recovery and delivery-claim paths; the
    # SQLite ledger below is retained only for records written by older Agent
    # versions.
    resolved_id = str(delegation_id or "")
    if not resolved_id:
        return None
    try:
        with _persist_lock:
            data = _read_persisted_unlocked()
            entry = (data.get("records") or {}).get(resolved_id)
        if isinstance(entry, dict):
            record = entry.get("record") if isinstance(entry.get("record"), dict) else entry
            result = entry.get("result")
            return {
                "delegation_id": resolved_id,
                "origin_session": record.get("session_key") or record.get("origin_session") or "",
                "state": entry.get("status") or record.get("status"),
                "dispatched_at": record.get("dispatched_at"),
                "completed_at": record.get("completed_at"),
                "result": result,
                "delivery_state": entry.get("delivery_status") or record.get("delivery_status"),
                "delivery_attempts": entry.get("delivery_attempts", record.get("delivery_attempts", 0)),
                "delivery_claim": entry.get("delivery_claim") or "",
                "delivery_claimed_at": entry.get("delivery_claimed_at"),
                "origin_session_id": record.get("origin_session_id") or "",
            }
    except Exception:
        logger.debug("JSON async delegation lookup failed", exc_info=True)

    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""",
            (resolved_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": resolved_id,
        "origin_session": row[0],
        "state": row[1],
        "dispatched_at": row[2],
        "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5],
        "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
    }


def _persistable_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe record for the portable checkpoint."""
    filtered = {
        k: v
        for k, v in record.items()
        if k
        not in {
            "interrupt_fn",
            "heartbeat_stop",
            "progress_fn",
            "_terminal_callback",
        }
    }
    try:
        return json.loads(json.dumps(filtered, ensure_ascii=False, default=str))
    except Exception:
        return {str(k): str(v) for k, v in filtered.items()}


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
        if k not in {"interrupt_fn", "heartbeat_stop", "_terminal_callback"}
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
        if _is_retained_nonterminal(record, status=status):
            continue
        if record.get(_BESTPLAN_TERMINALIZATION_PENDING) is True:
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
        if _is_retained_nonterminal(record):
            continue
        if record.get(_BESTPLAN_TERMINALIZATION_PENDING) is True:
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
) -> bool:
    """Checkpoint one record and report whether the durable write succeeded."""
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
            tracker_path = record.get("origin_tracker_path") or None
            data = _read_persisted_unlocked(tracker_path)
            existing = data.setdefault("records", {}).get(delegation_id, {})
            if isinstance(existing, dict) and existing:
                existing_record = (
                    existing.get("record")
                    if isinstance(existing.get("record"), dict)
                    else existing
                )
                existing_status = str(
                    existing.get("status") or existing_record.get("status") or ""
                )
                incoming_status = str(record.get("status") or "")
                existing_phase = _STATUS_PHASE.get(existing_status, 4)
                incoming_phase = _STATUS_PHASE.get(incoming_status, 4)
                operator_cancel_advance = bool(
                    incoming_status == "interrupting"
                    and existing_status in _DURABLE_NONTERMINAL_STATUSES
                    and record.get("interrupt_requested_at") is not None
                    and record.get("bestplan_local_execution") is True
                )
                if incoming_phase < existing_phase and not operator_cancel_advance:
                    logger.warning(
                        "Rejected stale async checkpoint %s: %s -> %s",
                        delegation_id,
                        existing_status,
                        incoming_status,
                    )
                    return False
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
        return True
    except Exception as exc:
        logger.warning("Failed to persist async delegation %s: %s", delegation_id, exc)
        return False


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


def _persisted_status(record: Dict[str, Any]) -> str:
    """Read one durable lifecycle status under the persistence lock."""
    delegation_id = str(record.get("delegation_id") or "")
    tracker_path = record.get("origin_tracker_path") or None
    if not delegation_id:
        return ""
    with _persist_lock:
        data = _read_persisted_unlocked(tracker_path)
        entry = (data.get("records") or {}).get(delegation_id)
    if not isinstance(entry, dict):
        return ""
    persisted_record = entry.get("record") if isinstance(entry.get("record"), dict) else {}
    return str(entry.get("status") or persisted_record.get("status") or "")


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


def mark_completion_delivered(delegation_id: str | Dict[str, Any]) -> bool:
    """Backward-compatible ACK by durable delegation id."""
    if isinstance(delegation_id, dict):
        return mark_async_delegation_delivered(delegation_id)
    resolved_id = str(delegation_id or "")
    if not resolved_id:
        return False
    with _records_lock:
        record = _records.get(resolved_id)
        if isinstance(record, dict):
            record["delivery_status"] = "delivered"
            record["delivered_at"] = time.time()
            tracker_path = str(record.get("origin_tracker_path") or "") or None
        else:
            tracker_path = None
    updated = _mark_persisted_delivery(
        resolved_id,
        "delivered",
        tracker_path=tracker_path,
    )
    if updated:
        return True
    # Compatibility for callers that still use the SQLite durable ledger.
    try:
        now = time.time()
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE async_delegations SET delivery_state='delivered',
                   delivered_at=?, updated_at=?
                   WHERE delegation_id=? AND delivery_state!='delivered'""",
                (now, now, resolved_id),
            )
            return cur.rowcount == 1
    except Exception:
        logger.debug("SQLite async completion ACK failed", exc_info=True)
        return False


def _tracker_path_for_delegation(delegation_id: str) -> str | None:
    with _records_lock:
        record = _records.get(delegation_id)
        if isinstance(record, dict):
            path = record.get("origin_tracker_path")
            return str(path) if path else None
    return None


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Atomically claim one durable completion for gateway delivery."""
    if not delegation_id or not claim_id:
        return False
    tracker_path = _tracker_path_for_delegation(delegation_id)
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            entry = (data.get("records") or {}).get(delegation_id)
            if not isinstance(entry, dict):
                entry = None
            if entry is None:
                raise KeyError("not in JSON tracker")
            existing = str(entry.get("delivery_claim") or "")
            if existing and existing != claim_id:
                claimed_at = entry.get("delivery_claimed_at")
                try:
                    claim_is_live = (
                        claimed_at is not None
                        and time.time() - float(claimed_at)
                        < _DELIVERY_CLAIM_LEASE_SECONDS
                    )
                except (TypeError, ValueError):
                    claim_is_live = True
                if claim_is_live:
                    return False
            entry["delivery_claim"] = claim_id
            entry["delivery_claimed_at"] = time.time()
            entry["delivery_attempts"] = int(entry.get("delivery_attempts") or 0) + 1
            _write_persisted_unlocked(data, tracker_path)
            verify = _read_persisted_unlocked(tracker_path)
            return (
                (verify.get("records") or {})
                .get(delegation_id, {})
                .get("delivery_claim")
                == claim_id
            )
    except Exception:
        try:
            now = time.time()
            with _DB_LOCK, _transaction() as conn:
                row = conn.execute(
                    "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()
                if row is None:
                    # Legacy, non-durable events do not need a claim token.
                    return True
                cur = conn.execute(
                    """UPDATE async_delegations SET delivery_claim=?,
                       delivery_claimed_at=?, delivery_attempts=delivery_attempts+1,
                       updated_at=? WHERE delegation_id=? AND delivery_state='pending'
                       AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
                    (claim_id, now, now, delegation_id, now - 300),
                )
                return cur.rowcount == 1
        except Exception:
            logger.warning("Failed to claim async completion %s", delegation_id, exc_info=True)
            return False


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a delivery claim when injection did not complete."""
    return _update_completion_claim(delegation_id, claim_id, delivered=False)


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge a successfully injected completion and clear its claim."""
    if not _update_completion_claim(delegation_id, claim_id, delivered=True):
        return False
    return True


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; ordinary process events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    """Complete a durable delegation event after injecting it into a turn."""
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    """Release a durable delegation event claim for a later consumer."""
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a completion whose target session is gone."""
    if not delegation_id or not claim_id:
        return False
    tracker_path = _tracker_path_for_delegation(delegation_id)
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            entry = (data.get("records") or {}).get(delegation_id)
            if not isinstance(entry, dict) or str(entry.get("delivery_claim") or "") != claim_id:
                return False
            entry["delivery_status"] = "dropped"
            entry["delivery_claim"] = ""
            entry["updated_at"] = time.time()
            _write_persisted_unlocked(data, tracker_path)
        return True
    except Exception:
        logger.warning("Failed to drop async completion %s", delegation_id, exc_info=True)
        return False


def _update_completion_claim(
    delegation_id: str, claim_id: str, *, delivered: bool
) -> bool:
    if not delegation_id or not claim_id:
        return False
    tracker_path = _tracker_path_for_delegation(delegation_id)
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            entry = (data.get("records") or {}).get(delegation_id)
            if not isinstance(entry, dict) or str(entry.get("delivery_claim") or "") != claim_id:
                return False
            entry["delivery_claim"] = ""
            entry["updated_at"] = time.time()
            if delivered:
                entry["delivery_status"] = "delivered"
                entry["delivered_at"] = time.time()
            _write_persisted_unlocked(data, tracker_path)
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["delivery_status"] = "delivered" if delivered else "queued"
        return True
    except Exception:
        logger.warning("Failed to update async completion %s", delegation_id, exc_info=True)
        return False


def _remove_persisted_record(
    delegation_id: str,
    *,
    tracker_path: str | Path | None = None,
) -> bool:
    """Remove and verify a rejected dispatch's persisted entry."""
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            records = data.setdefault("records", {})
            records.pop(delegation_id, None)
            _write_persisted_unlocked(data, tracker_path)
            verify = _read_persisted_unlocked(tracker_path)
            return delegation_id not in (verify.get("records") or {})
    except Exception as exc:
        logger.warning(
            "Failed to remove persisted phantom for %s: %s", delegation_id, exc
        )
        return False


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
    event = {
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
        "status": "lost",
        "summary": result["summary"],
        "error": result["error"],
    }
    if (
        event["bestplan_plan_id"]
        and record.get("bestplan_local_execution") is True
    ):
        event["bestplan_local_execution"] = True
    if record.get("is_batch"):
        event.update({
            "is_batch": True,
            "goals": record.get("goals") or [],
            "results": [],
            "resolved_runtimes": record.get("resolved_runtimes") or [],
            "total_duration_seconds": result["duration_seconds"],
        })
    return event


def _event_for_interrupted_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Build terminal evidence for a cancellation completed after restart."""

    event = _event_for_lost_record(record)
    result = dict(event["result"])
    result.update({
        "status": "interrupted",
        "summary": "Async delegation was interrupted after child extinction.",
        "error": "interrupted after child extinction",
        "exit_reason": "interrupted",
    })
    event.update({
        "result": result,
        "status": "interrupted",
        "summary": result["summary"],
        "error": result["error"],
    })
    return event


def _bestplan_review_resume_request(
    record: Dict[str, Any],
    *,
    tracker_path: str | Path | None = None,
    cancel_finalize_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """Rebuild a non-secret review handoff from durable store identity."""

    if (
        record.get("bestplan_local_execution") is not True
        or not record.get("bestplan_plan_id")
        or not record.get("bestplan_review_job_id")
    ):
        return None
    try:
        state_db_path = _canonical_bestplan_state_db_path(
            record.get("bestplan_state_db_path")
        )
        if not state_db_path or not Path(state_db_path).is_file():
            return None
        from agent.review_engine import ReviewStore, ReviewValidationError

        job_id = str(record["bestplan_review_job_id"])
        store = ReviewStore(state_db_path)
        try:
            job = store.get_job(job_id)
        except ReviewValidationError:
            pipeline = store.get_execution_pipeline(
                str(record.get("bestplan_plan_id") or "")
            )
            plan_id = str(record.get("bestplan_plan_id") or "")
            session_id = str(record.get("origin_session_id") or "")
            profile = str(record.get("origin_profile") or "")
            pipeline_is_eligible = (
                pipeline.cancel_requested
                and pipeline.state in {"cancel_requested", "cancelled"}
                if cancel_finalize_only
                else pipeline.state == "pending" and not pipeline.cancel_requested
            )
            if (
                not pipeline_is_eligible
                or pipeline.delegation_id
                != str(record.get("delegation_id") or "")
                or pipeline.job_id != job_id
                or pipeline.owner_session_id != session_id
                or pipeline.owner_profile != profile
                or not pipeline.workspace
            ):
                return None
            request: Dict[str, Any] = {
                "kind": "bestplan_execution_resume",
                "delegation_id": pipeline.delegation_id,
                "job_id": pipeline.job_id,
                "plan_id": plan_id,
                "state_db_path": state_db_path,
                "tracker_path": str(_persistence_path(tracker_path)),
                "adapter_version": pipeline.adapter_version,
                "session_id": session_id,
                "profile": profile,
                "workspace": pipeline.workspace,
            }
            if cancel_finalize_only:
                request["_cancel_finalize_only"] = True
            return request
    except Exception:
        logger.debug("BestPlan review recovery identity is invalid", exc_info=True)
        return None
    plan_id = str(record.get("bestplan_plan_id") or "")
    session_id = str(record.get("origin_session_id") or "")
    profile = str(record.get("origin_profile") or "")
    if (
        not job.adapter_version
        or job.source_kind != "bestplan_integration"
        or job.source_id != plan_id
        or job.owner_session_id != session_id
        or job.owner_profile != profile
        or not job.workspace
        or (
            cancel_finalize_only
            and not (job.cancel_requested or job.state == "cancelled")
        )
    ):
        return None
    request = {
        "kind": "bestplan_review_resume",
        "delegation_id": str(record.get("delegation_id") or ""),
        "job_id": job.job_id,
        "plan_id": plan_id,
        "state_db_path": state_db_path,
        "tracker_path": str(_persistence_path(tracker_path)),
        "adapter_version": job.adapter_version,
        "session_id": session_id,
        "profile": profile,
        "workspace": job.workspace,
    }
    if cancel_finalize_only:
        request["_cancel_finalize_only"] = True
    return request


def enqueue_bestplan_review_job(
    *,
    state_db_path: str,
    job_id: str,
) -> bool:
    """Queue one exact durable BestPlan review after a manual attachment."""

    try:
        canonical_state_db_path = _canonical_bestplan_state_db_path(
            state_db_path
        )
    except ValueError:
        return False
    if (
        not canonical_state_db_path
        or not Path(canonical_state_db_path).is_file()
        or not isinstance(job_id, str)
        or not job_id
        or len(job_id) > 256
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in job_id
        )
    ):
        return False

    with _records_lock:
        matches: list[tuple[str, Dict[str, Any]]] = []
        for delegation_id, candidate in _records.items():
            if (
                not isinstance(candidate, dict)
                or candidate.get("bestplan_local_execution") is not True
                or candidate.get("bestplan_review_job_id") != job_id
            ):
                continue
            try:
                candidate_state_db_path = _canonical_bestplan_state_db_path(
                    candidate.get("bestplan_state_db_path")
                )
            except ValueError:
                continue
            if candidate_state_db_path == canonical_state_db_path:
                matches.append((delegation_id, candidate))
        if len(matches) != 1:
            return False
        delegation_id, live = matches[0]
        if live.get("status") not in {
            "running",
            "stalling",
            "interrupting",
            "review_waiting",
            "review_requeued",
        }:
            return False
        if live.get("_bestplan_review_enqueue_token"):
            return False
        if _durable_review_cancelled(live):
            return False
        tracker_path = str(live.get("origin_tracker_path") or "")
        request = _bestplan_review_resume_request(
            live,
            tracker_path=tracker_path or None,
        )
        if (
            request is None
            or request.get("kind") != "bestplan_review_resume"
            or request.get("delegation_id") != delegation_id
            or request.get("job_id") != job_id
            or request.get("state_db_path") != canonical_state_db_path
        ):
            return False
        if live.get("status") == "review_requeued":
            return True
        enqueue_token = uuid.uuid4().hex
        live["_bestplan_review_enqueue_token"] = enqueue_token
        prior_status = str(live.get("status") or "")
        snapshot = dict(live)
        snapshot.pop("_bestplan_review_enqueue_token", None)
        snapshot["status"] = "review_requeued"
        snapshot["delivery_status"] = "review_requeued"
        snapshot["review_recovery_reason_code"] = "manual_attachment"
        snapshot["review_recovery_requested_at"] = time.time()
        snapshot.pop("completed_at", None)

    if not _persist_record(snapshot, delivery_status="review_requeued"):
        with _records_lock:
            current = _records.get(delegation_id)
            if (
                current is live
                and current.get("_bestplan_review_enqueue_token")
                == enqueue_token
            ):
                current.pop("_bestplan_review_enqueue_token", None)
        return False

    with _records_lock:
        current = _records.get(delegation_id)
        if (
            current is not live
            or current.get("_bestplan_review_enqueue_token") != enqueue_token
            or str(current.get("status") or "") != prior_status
            or current.get("bestplan_review_job_id") != job_id
        ):
            if current is live:
                current.pop("_bestplan_review_enqueue_token", None)
            return False
        current.pop("_bestplan_review_enqueue_token", None)
        current.update(
            {
                "status": "review_requeued",
                "delivery_status": "review_requeued",
                "review_recovery_reason_code": "manual_attachment",
                "review_recovery_requested_at": snapshot[
                    "review_recovery_requested_at"
                ],
                "interrupt_fn": None,
            }
        )
        current.pop("completed_at", None)
        heartbeat_stop = current.get("heartbeat_stop")
        if hasattr(heartbeat_stop, "set"):
            heartbeat_stop.set()

    _bestplan_review_recovery_queue.put(request)
    _start_bestplan_review_recovery_consumer()
    return True


def _execution_pipeline_matches_tracker_identity(
    pipeline: object,
    record: Mapping[str, Any],
    *,
    state_path: str,
) -> bool:
    """Bind a pre-review pipeline to its canonical plan and tracker."""

    plan_id = str(record.get("bestplan_plan_id") or "")
    delegation_id = str(record.get("delegation_id") or "")
    job_id = str(record.get("bestplan_review_job_id") or "")
    session_id = str(record.get("origin_session_id") or "")
    raw_profile = record.get("origin_profile")
    if not isinstance(raw_profile, str):
        return False
    profile = raw_profile
    raw_tracker_path = str(record.get("origin_tracker_path") or "")
    if not all(
        (
            plan_id,
            delegation_id,
            job_id,
            session_id,
            raw_tracker_path,
        )
    ):
        return False
    try:
        state_db = Path(state_path)
        tracker = Path(raw_tracker_path)
        canonical_tracker = tracker.resolve(strict=False)
        expected_tracker = state_db.parent / "async_delegations.json"
        if (
            not state_db.is_file()
            or not tracker.is_absolute()
            or str(canonical_tracker) != raw_tracker_path
            or canonical_tracker != expected_tracker
        ):
            return False
        connection = sqlite3.connect(
            f"{state_db.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            plan_row = connection.execute(
                "SELECT plan_id, session_id, profile, workspace "
                "FROM bestplan_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return False
    if plan_row is None:
        return False
    workspace = str(plan_row["workspace"] or "")
    return bool(
        getattr(pipeline, "plan_id", None) == plan_id
        and getattr(pipeline, "delegation_id", None) == delegation_id
        and getattr(pipeline, "job_id", None) == job_id
        and getattr(pipeline, "owner_session_id", None)
        == plan_row["session_id"]
        == session_id
        and getattr(pipeline, "owner_profile", None)
        == plan_row["profile"]
        == profile
        and bool(workspace)
        and getattr(pipeline, "workspace", None) == workspace
    )


def _durable_review_cancelled(record: Mapping[str, Any]) -> bool:
    """Read the durable cancellation bit before any recovery handoff."""

    if record.get("bestplan_local_execution") is not True:
        return False
    try:
        state_path = _canonical_bestplan_state_db_path(
            record.get("bestplan_state_db_path")
        )
        job_id = str(record.get("bestplan_review_job_id") or "")
        if not state_path or not job_id:
            return False
        from agent.review_engine import ReviewStore, ReviewValidationError

        store = ReviewStore(state_path)
        try:
            job = store.get_job(job_id)
        except ReviewValidationError:
            pipeline = store.get_execution_pipeline(
                str(record.get("bestplan_plan_id") or "")
            )
            if not _execution_pipeline_matches_tracker_identity(
                pipeline,
                record,
                state_path=state_path,
            ):
                return False
            return bool(
                pipeline.cancel_requested or pipeline.state == "cancelled"
            )
        return bool(
            job.cancel_requested
            or job.state in {"cancel_requested", "cancelled"}
        )
    except Exception:
        return False


def _finalize_durable_review_cancel(
    record: Mapping[str, Any],
) -> bool:
    """Finalize only after the caller proves its child action has unwound."""

    if record.get("bestplan_local_execution") is not True:
        return False
    try:
        state_path = _canonical_bestplan_state_db_path(
            record.get("bestplan_state_db_path")
        )
        job_id = str(record.get("bestplan_review_job_id") or "")
        if not state_path or not job_id:
            return False
        from agent.review_engine import ReviewStore, ReviewValidationError

        store = ReviewStore(state_path)
        try:
            job = store.get_job(job_id)
        except ReviewValidationError:
            pipeline = store.get_execution_pipeline(
                str(record.get("bestplan_plan_id") or "")
            )
            if not _execution_pipeline_matches_tracker_identity(
                pipeline,
                record,
                state_path=state_path,
            ):
                return False
            if pipeline.state == "cancelled" and pipeline.cancel_requested:
                return True
            if (
                pipeline.state != "cancel_requested"
                or not pipeline.cancel_requested
            ):
                return False
            store.finalize_execution_pipeline_cancel(
                plan_id=pipeline.plan_id,
                delegation_id=str(record.get("delegation_id") or ""),
                job_id=job_id,
            )
            return True
        if job.state == "cancelled":
            return True
        if (
            not job.cancel_requested
            or job.state != "cancel_requested"
            or job.owner_id is None
            or job.lease_expires_at_ns is None
        ):
            return False
        store.finalize_cancel(
            job_id=job_id,
            owner_id=job.owner_id,
            fencing_token=job.fencing_token,
            operation_id="async-cancel-finalized-" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{record.get('delegation_id')}:{job_id}",
            ).hex,
        )
        return True
    except Exception:
        logger.warning(
            "BestPlan durable cancellation could not be finalized",
            exc_info=True,
        )
        return False


def _handoff_running_bestplan_review(
    delegation_id: str,
    *,
    reason_code: str,
) -> bool:
    """Move one live local BestPlan failure onto its durable review rail."""

    with _records_lock:
        live = _records.get(delegation_id)
        if (
            live is None
            or live.get("bestplan_local_execution") is not True
            or live.get("status") not in {"running", "stalling", "interrupting"}
        ):
            return False
        if _durable_review_cancelled(live):
            return False
        tracker_path = str(live.get("origin_tracker_path") or "")
        request = _bestplan_review_resume_request(
            live, tracker_path=tracker_path or None,
        )
        if request is None:
            return False
        live["status"] = "review_requeued"
        live["delivery_status"] = "review_requeued"
        live["review_recovery_reason_code"] = str(reason_code or "deferred")
        live["review_recovery_requested_at"] = time.time()
        live.pop("completed_at", None)
        heartbeat_stop = live.get("heartbeat_stop")
        if hasattr(heartbeat_stop, "set"):
            heartbeat_stop.set()
        live["interrupt_fn"] = None
        snapshot = dict(live)
    if not _persist_record(snapshot, delivery_status="review_requeued"):
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None and live.get("status") == "review_requeued":
                live["status"] = "review_waiting"
                live["delivery_status"] = "review_waiting"
        return False
    _bestplan_review_recovery_queue.put(request)
    _start_bestplan_review_recovery_consumer()
    return True


def recover_async_delegations(
    tracker_path: str | Path | None = None,
    *,
    target_queue=None,
    mark_restored: bool = False,
    review_recovery_queue=_OWNED_REVIEW_RECOVERY_QUEUE,
) -> Dict[str, Any]:
    """Replay undelivered completions and mark previous-process runners lost."""
    global _recovery_attempted
    use_owned_review_queue = (
        review_recovery_queue is _OWNED_REVIEW_RECOVERY_QUEUE
    )
    if use_owned_review_queue:
        review_recovery_queue = _bestplan_review_recovery_queue
    queued = 0
    lost = 0
    review_requeued = 0
    review_waiting = 0
    now = time.time()
    restored_records: List[tuple[str, Dict[str, Any]]] = []
    notifications: List[
        tuple[tuple[str, str], str, Dict[str, Any], Dict[str, Any]]
    ] = []
    review_notifications: List[tuple[str, Dict[str, str], Dict[str, Any]]] = []
    recovered_active_records: List[tuple[str, Dict[str, Any]]] = []
    if target_queue is None:
        try:
            from tools.process_registry import process_registry

            target_queue = process_registry.completion_queue
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
            owner_liveness = _owner_liveness(record)
            lost_now = False
            exact_local_bestplan = bool(record.get("bestplan_plan_id")) and (
                record.get("bestplan_local_execution") is True
            )
            if (
                exact_local_bestplan
                and owner_liveness is None
                and status in {
                    "intent",
                    "scheduled",
                    "running",
                    "interrupting",
                    "review_waiting",
                    "review_requeued",
                }
            ):
                # Unknown is not dead: expose the exact durable work to status
                # and stop handlers, but never replay a possibly live owner.
                record = dict(record)
                record["owner_liveness"] = "unknown"
                entry["record"] = record
                entry["updated_at"] = now
                recovered_active_records.append((str(rid), dict(record)))
                continue
            if status in {"review_waiting", "review_requeued"}:
                cancel_finalize_pending = False
                if _durable_review_cancelled(record):
                    # A dead process proves every process-local child is
                    # extinct. Finalize the durable cancellation before the
                    # tracker becomes terminal.
                    if (
                        _finalize_durable_review_cancel(record)
                        and _mark_bestplan_cancelled_terminal(record)
                    ):
                        record = dict(record)
                        record["status"] = "interrupted"
                        record["delivery_status"] = "interrupted"
                        record["completed_at"] = now
                        entry["record"] = record
                        entry["status"] = "interrupted"
                        entry["delivery_status"] = "interrupted"
                        entry["updated_at"] = now
                        continue
                    cancel_finalize_pending = True
                resume_request = _bestplan_review_resume_request(
                    record, tracker_path=tracker_path,
                )
                if resume_request is None and cancel_finalize_pending:
                    resume_request = _bestplan_review_resume_request(
                        record,
                        tracker_path=tracker_path,
                        cancel_finalize_only=True,
                    )
                if resume_request is None:
                    if cancel_finalize_pending:
                        record = dict(record)
                        record["status"] = "review_waiting"
                        record["delivery_status"] = "review_waiting"
                        record["review_recovery_reason_code"] = (
                            "cancel_finalize_failed"
                        )
                        entry["record"] = record
                        entry["status"] = "review_waiting"
                        entry["delivery_status"] = "review_waiting"
                        entry.pop("event", None)
                        entry.pop("result", None)
                        entry["updated_at"] = now
                        review_waiting += 1
                        recovered_active_records.append((str(rid), dict(record)))
                    continue
                if review_recovery_queue is not None:
                    if cancel_finalize_pending:
                        resume_request = dict(resume_request)
                        resume_request["_cancel_finalize_only"] = True
                    record = dict(record)
                    record["status"] = "review_requeued"
                    record["delivery_status"] = "review_requeued"
                    entry["record"] = record
                    entry["status"] = "review_requeued"
                    entry["delivery_status"] = "review_requeued"
                    entry["updated_at"] = now
                    review_notifications.append((str(rid), resume_request, record))
                    review_requeued += 1
                else:
                    record = dict(record)
                    record["status"] = "review_waiting"
                    record["delivery_status"] = "review_waiting"
                    entry["record"] = record
                    entry["status"] = "review_waiting"
                    entry["delivery_status"] = "review_waiting"
                    entry["updated_at"] = now
                    review_waiting += 1
                    recovered_active_records.append((str(rid), dict(record)))
                continue
            if status == "scheduled" and owner_liveness is False:
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
            elif status == "scheduled" and owner_liveness is None:
                record = dict(record)
                record["owner_liveness"] = "unknown"
                entry["record"] = record
                entry["updated_at"] = now
                continue
            if status == "intent":
                if owner_liveness is True:
                    continue
                if owner_liveness is None:
                    record = dict(record)
                    record["owner_liveness"] = "unknown"
                    entry["record"] = record
                    entry["updated_at"] = now
                    continue
                cancel_finalize_pending = False
                if _durable_review_cancelled(record):
                    if (
                        _finalize_durable_review_cancel(record)
                        and _mark_bestplan_cancelled_terminal(record)
                    ):
                        record = dict(record)
                        record["status"] = "interrupted"
                        record["delivery_status"] = "interrupted"
                        record["completed_at"] = now
                        entry["record"] = record
                        entry["status"] = "interrupted"
                        entry["delivery_status"] = "interrupted"
                        entry["updated_at"] = now
                        continue
                    cancel_finalize_pending = True
                resume_request = _bestplan_review_resume_request(
                    record, tracker_path=tracker_path,
                )
                if resume_request is None and cancel_finalize_pending:
                    resume_request = _bestplan_review_resume_request(
                        record,
                        tracker_path=tracker_path,
                        cancel_finalize_only=True,
                    )
                if resume_request is None:
                    if cancel_finalize_pending:
                        record = dict(record)
                        record["status"] = "review_waiting"
                        record["delivery_status"] = "review_waiting"
                        record["review_recovery_reason_code"] = (
                            "cancel_finalize_failed"
                        )
                        entry["record"] = record
                        entry["status"] = "review_waiting"
                        entry["delivery_status"] = "review_waiting"
                        entry["updated_at"] = now
                        review_waiting += 1
                        recovered_active_records.append(
                            (str(rid), dict(record))
                        )
                    continue
                next_status = (
                    "review_requeued"
                    if review_recovery_queue is not None
                    else "review_waiting"
                )
                record = dict(record)
                record["status"] = next_status
                record["delivery_status"] = next_status
                record["review_recovery_requested_at"] = now
                entry["record"] = record
                entry["status"] = next_status
                entry["delivery_status"] = next_status
                entry.pop("event", None)
                entry.pop("result", None)
                entry["updated_at"] = now
                if review_recovery_queue is None:
                    review_waiting += 1
                    recovered_active_records.append((str(rid), dict(record)))
                else:
                    if cancel_finalize_pending:
                        resume_request = dict(resume_request)
                        resume_request["_cancel_finalize_only"] = True
                    review_notifications.append(
                        (str(rid), resume_request, record)
                    )
                    review_requeued += 1
                continue
            if status in {"running", "interrupting"}:
                if owner_liveness is True:
                    continue
                if owner_liveness is None:
                    record = dict(record)
                    record["owner_liveness"] = "unknown"
                    entry["record"] = record
                    entry["updated_at"] = now
                    continue
                cancel_finalize_pending = False
                if _durable_review_cancelled(record):
                    # The persisted owner is dead, so its process-local child
                    # is extinct. Close the durable cancellation before the
                    # tracker becomes terminal.
                    if (
                        _finalize_durable_review_cancel(record)
                        and _mark_bestplan_cancelled_terminal(record)
                    ):
                        record = dict(record)
                        record["status"] = "interrupted"
                        record["delivery_status"] = "interrupted"
                        record["completed_at"] = now
                        entry["record"] = record
                        entry["status"] = "interrupted"
                        entry["delivery_status"] = "interrupted"
                        entry["updated_at"] = now
                        continue
                    cancel_finalize_pending = True
                resume_request = _bestplan_review_resume_request(
                    record, tracker_path=tracker_path,
                )
                if resume_request is None and cancel_finalize_pending:
                    resume_request = _bestplan_review_resume_request(
                        record,
                        tracker_path=tracker_path,
                        cancel_finalize_only=True,
                    )
                if resume_request is not None:
                    if cancel_finalize_pending:
                        resume_request = dict(resume_request)
                        resume_request["_cancel_finalize_only"] = True
                    record = dict(record)
                    next_status = (
                        "review_requeued"
                        if review_recovery_queue is not None
                        else "review_waiting"
                    )
                    record["status"] = next_status
                    record["delivery_status"] = next_status
                    record["review_recovery_requested_at"] = now
                    entry["record"] = record
                    entry["status"] = next_status
                    entry["delivery_status"] = next_status
                    entry.pop("event", None)
                    entry.pop("result", None)
                    entry["updated_at"] = now
                    status = next_status
                    delivery_status = next_status
                    if review_recovery_queue is None:
                        review_waiting += 1
                        recovered_active_records.append(
                            (str(rid), dict(record))
                        )
                    else:
                        review_notifications.append(
                            (str(rid), resume_request, record)
                        )
                        review_requeued += 1
                    continue
                if cancel_finalize_pending:
                    record = dict(record)
                    record["status"] = "review_waiting"
                    record["delivery_status"] = "review_waiting"
                    record["review_recovery_reason_code"] = (
                        "cancel_finalize_failed"
                    )
                    entry["record"] = record
                    entry["status"] = "review_waiting"
                    entry["delivery_status"] = "review_waiting"
                    entry.pop("event", None)
                    entry.pop("result", None)
                    entry["updated_at"] = now
                    review_waiting += 1
                    recovered_active_records.append((str(rid), dict(record)))
                    continue
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
                lost_now = True
            terminalization_pending = (
                record.get(_BESTPLAN_TERMINALIZATION_PENDING) is True
            )
            if terminalization_pending and not exact_local_bestplan:
                continue
            if exact_local_bestplan and (lost_now or terminalization_pending):
                if not isinstance(event, dict) or not _mark_bestplan_completed_unverified(
                    record, event,
                ):
                    record[_BESTPLAN_TERMINALIZATION_PENDING] = True
                    entry["record"] = record
                    entry["delivery_status"] = "pending"
                    entry["updated_at"] = now
                    continue
                record = dict(record)
                record.pop(_BESTPLAN_TERMINALIZATION_PENDING, None)
                entry["record"] = record
                entry["updated_at"] = now
            replay_identity = (str(_persistence_path(tracker_path)), str(rid))
            delivery_claim = str(entry.get("delivery_claim") or "")
            claim_stale = False
            if delivery_claim:
                try:
                    claim_stale = (
                        entry.get("delivery_claimed_at") is None
                        or now - float(entry.get("delivery_claimed_at"))
                        >= _DELIVERY_CLAIM_LEASE_SECONDS
                    )
                except (TypeError, ValueError):
                    claim_stale = True
            if (
                status not in _ACTIVE_STATUSES
                and delivery_status != "delivered"
                and event
                and (
                    replay_identity not in _replayed_persisted_ids
                    or claim_stale
                )
            ):
                # Persist queued delivery before publishing.  The queue is an
                # in-process notification rail; disk is the recovery truth.
                entry["delivery_status"] = "queued"
                entry["queued_at"] = now
                entry["updated_at"] = now
                notifications.append((replay_identity, str(rid), event, dict(record)))
        _cleanup_persisted_data_locked(data, now=now)
        _write_persisted_unlocked(data, tracker_path)
        for rid, request, record in review_notifications:
            review_recovery_queue.put(request)
            recovered_active_records.append((rid, dict(record)))
        for replay_identity, rid, event, record in notifications:
            queued_event = dict(event)
            if mark_restored:
                queued_event["restored"] = True
            target_queue.put(queued_event)
            _replayed_persisted_ids.add(replay_identity)
            queued += 1
            restored = dict(record)
            restored["delivery_status"] = "queued"
            restored_records.append((rid, restored))
    if restored_records or recovered_active_records:
        with _records_lock:
            for rid, restored in restored_records:
                live = _records.get(rid)
                if live is None:
                    _records[rid] = restored
                elif live.get(_BESTPLAN_TERMINALIZATION_PENDING) is True:
                    live.pop(_BESTPLAN_TERMINALIZATION_PENDING, None)
                    live["delivery_status"] = "queued"
                    live["queued_at"] = time.time()
            for rid, recovered in recovered_active_records:
                if rid not in _records:
                    recovered["interrupt_fn"] = None
                    _records[rid] = recovered
    _recovery_attempted = True
    if use_owned_review_queue:
        _start_bestplan_review_recovery_consumer()
    result = {"queued": queued, "lost": lost}
    if review_requeued:
        result["review_requeued"] = review_requeued
    if review_waiting:
        result["review_waiting"] = review_waiting
    return result


def _defer_bestplan_review_recovery(
    request: Dict[str, str],
    *,
    reason_code: str,
) -> None:
    """Return one failed-closed recovery handoff to its durable wait state."""

    delegation_id = str(request.get("delegation_id") or "")
    tracker_path = str(request.get("tracker_path") or "")
    job_id = str(request.get("job_id") or "")
    if not delegation_id or not tracker_path or not job_id:
        return
    now = time.time()
    with _persist_lock:
        data = _read_persisted_unlocked(tracker_path)
        entry = (data.get("records") or {}).get(delegation_id)
        record = (
            entry.get("record")
            if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
            else None
        )
        if (
            not isinstance(entry, dict)
            or not isinstance(record, dict)
            or record.get("bestplan_review_job_id") != job_id
            or str(entry.get("status") or record.get("status") or "")
            not in {"review_requeued", "review_waiting"}
        ):
            return
        record = dict(record)
        record["status"] = "review_waiting"
        record["delivery_status"] = "review_waiting"
        record["review_recovery_reason_code"] = str(reason_code or "deferred")
        entry["record"] = record
        entry["status"] = "review_waiting"
        entry["delivery_status"] = "review_waiting"
        entry["updated_at"] = now
        _write_persisted_unlocked(data, tracker_path)
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("bestplan_review_job_id") == job_id:
            live["status"] = "review_waiting"
            live["delivery_status"] = "review_waiting"
            live["review_recovery_reason_code"] = str(
                reason_code or "deferred"
            )


def _try_defer_bestplan_review_recovery(
    request: Dict[str, str],
    *,
    reason_code: str,
) -> bool:
    """Keep a failed defer write from terminating the recovery consumer."""

    try:
        _defer_bestplan_review_recovery(
            request, reason_code=reason_code,
        )
        return True
    except Exception:
        logger.error(
            "BestPlan review defer checkpoint failed for %s (%s)",
            request.get("delegation_id") or "<unknown>",
            reason_code,
            exc_info=True,
        )
        return False


def _durable_review_cancel_record(
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the exact durable cancel identity without invoking a worker."""

    delegation_id = str(request.get("delegation_id") or "")
    job_id = str(request.get("job_id") or "")
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("bestplan_review_job_id") == job_id:
            return dict(live)
    return {
        "delegation_id": delegation_id,
        "bestplan_plan_id": str(request.get("plan_id") or ""),
        "bestplan_local_execution": True,
        "bestplan_state_db_path": str(request.get("state_db_path") or ""),
        "bestplan_review_job_id": job_id,
        "origin_tracker_path": str(request.get("tracker_path") or ""),
    }


def _terminalize_durable_review_cancel_tracker(
    request: Mapping[str, Any],
) -> bool:
    """Mark the tracker interrupted only after durable cancel finalization."""

    delegation_id = str(request.get("delegation_id") or "")
    job_id = str(request.get("job_id") or "")
    if not delegation_id or not job_id:
        return False
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("bestplan_review_job_id") == job_id:
            live_status = str(live.get("status") or "")
            if live_status not in {
                "intent", "scheduled", "running", "stalling", "interrupting",
                "review_waiting", "review_requeued", "interrupted",
            }:
                return False
            if live_status != "interrupted":
                live["status"] = "interrupting"
                live["delivery_status"] = "interrupting"
            live_snapshot = dict(live)
        else:
            live_snapshot = None
    if live_snapshot is not None:
        if live_snapshot.get("status") != "interrupted":
            _finalize_batch(
                delegation_id,
                {
                    "results": [],
                    "error": "interrupted after child extinction",
                },
                "interrupted",
            )
        with _records_lock:
            current = _records.get(delegation_id)
            if (
                current is None
                or current.get("bestplan_review_job_id") != job_id
                or current.get("status") != "interrupted"
            ):
                return False
            current_snapshot = dict(current)
        if not _mark_bestplan_cancelled_terminal(current_snapshot):
            return False
        if current_snapshot.get(_BESTPLAN_TERMINALIZATION_PENDING) is not True:
            return True
        tracker_path = str(current_snapshot.get("origin_tracker_path") or "")
        if not tracker_path:
            return False
        try:
            with _persist_lock:
                data = _read_persisted_unlocked(tracker_path)
                entry = (data.get("records") or {}).get(delegation_id)
                result = entry.get("result") if isinstance(entry, dict) else None
                event = entry.get("event") if isinstance(entry, dict) else None
            if not isinstance(result, dict) or not isinstance(event, dict):
                return False
            repaired = dict(current_snapshot)
            repaired.pop(_BESTPLAN_TERMINALIZATION_PENDING, None)
            if not _persist_and_queue_terminal(repaired, result, event):
                return False
            with _records_lock:
                current = _records.get(delegation_id)
                if (
                    current is not None
                    and current.get("bestplan_review_job_id") == job_id
                    and current.get("status") == "interrupted"
                ):
                    current.pop(_BESTPLAN_TERMINALIZATION_PENDING, None)
            return True
        except Exception:
            logger.error(
                "BestPlan cancelled terminal delivery repair failed for %s",
                delegation_id,
                exc_info=True,
            )
            return False

    tracker_path = str(request.get("tracker_path") or "")
    if not tracker_path:
        return False
    now = time.time()
    try:
        with _persist_lock:
            data = _read_persisted_unlocked(tracker_path)
            entry = (data.get("records") or {}).get(delegation_id)
            record = (
                entry.get("record")
                if isinstance(entry, dict)
                and isinstance(entry.get("record"), dict)
                else None
            )
            if (
                not isinstance(entry, dict)
                or not isinstance(record, dict)
                or record.get("bestplan_review_job_id") != job_id
                or record.get("bestplan_local_execution") is not True
            ):
                return False
            if not _mark_bestplan_cancelled_terminal(record):
                return False
            if str(entry.get("status") or record.get("status") or "") == (
                "interrupted"
            ):
                return True
            record = dict(record)
            record["status"] = "interrupted"
            record["delivery_status"] = "interrupted"
            record["completed_at"] = now
            entry["record"] = record
            entry["status"] = "interrupted"
            entry["delivery_status"] = "interrupted"
            entry.pop("event", None)
            entry.pop("result", None)
            entry["updated_at"] = now
            _write_persisted_unlocked(data, tracker_path)
        return True
    except Exception:
        logger.error(
            "BestPlan cancelled tracker terminalization failed for %s",
            delegation_id,
            exc_info=True,
        )
        return False


def _retry_durable_review_cancel_finalization(
    request: Dict[str, Any],
) -> None:
    """Schedule model-free durable cancel finalization with normal backoff."""

    retry_request = dict(request)
    retry_request["_cancel_finalize_only"] = True
    _try_defer_bestplan_review_recovery(
        retry_request, reason_code="cancel_finalize_failed",
    )
    _schedule_bestplan_review_recovery_retry(
        retry_request, reason_code="cancel_finalize_failed",
    )


def _complete_bestplan_review_recovery(
    request: Dict[str, str],
    completion: Mapping[str, Any],
) -> bool:
    """Publish one landed recovery through the normal durable terminal rail."""

    delegation_id = str(request.get("delegation_id") or "")
    tracker_path = str(request.get("tracker_path") or "")
    job_id = str(request.get("job_id") or "")
    if not delegation_id or not tracker_path or not job_id:
        return False
    now = time.time()
    with _persist_lock:
        data = _read_persisted_unlocked(tracker_path)
        entry = (data.get("records") or {}).get(delegation_id)
        record = (
            entry.get("record")
            if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
            else None
        )
        if not isinstance(entry, dict) or not isinstance(record, dict):
            return False
        status = str(entry.get("status") or record.get("status") or "")
        if status == "completed":
            return False
        if (
            status not in {"review_requeued", "review_waiting"}
            or record.get("bestplan_review_job_id") != job_id
            or record.get("bestplan_local_execution") is not True
        ):
            return False
        record = dict(record)
        record["status"] = "completed"
        record["completed_at"] = now
        record["last_heartbeat_at"] = now
        record["delivery_status"] = "finalizing"
        record.pop("review_recovery_reason_code", None)
        event = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": record.get("session_key", ""),
            "origin_ui_session_id": record.get("origin_ui_session_id", ""),
            "origin_session_id": record.get("origin_session_id", ""),
            "origin_profile": record.get("origin_profile", ""),
            "origin_tracker_path": record.get("origin_tracker_path", ""),
            "parent_session_id": record.get("parent_session_id"),
            "bestplan_plan_id": record.get("bestplan_plan_id", ""),
            "bestplan_local_execution": True,
            "resolved_runtimes": record.get("resolved_runtimes") or [],
            "goal": record.get("goal", ""),
            "goals": record.get("goals"),
            "context": record.get("context"),
            "toolsets": record.get("toolsets"),
            "role": record.get("role"),
            "model": record.get("model"),
            "status": "completed",
            "is_batch": True,
            "results": list(completion.get("results") or []),
            "error": completion.get("error"),
            "total_duration_seconds": round(
                now - float(record.get("dispatched_at") or now), 2,
            ),
            "dispatched_at": record.get("dispatched_at") or now,
            "completed_at": now,
        }
        if not _mark_bestplan_completed_unverified(record, event):
            return False
        entry["record"] = record
        entry["status"] = "completed"
        entry["result"] = dict(completion)
        entry["event"] = event
        entry["delivery_status"] = "pending"
        entry["updated_at"] = now
        _write_persisted_unlocked(data, tracker_path)
    if not _mark_persisted_delivery(
        delegation_id, "queued", tracker_path=tracker_path,
    ):
        return False
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(event)
    except Exception:
        _mark_persisted_delivery(
            delegation_id, "pending", tracker_path=tracker_path,
        )
        return False
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("bestplan_review_job_id") == job_id:
            live.update(record)
            live["delivery_status"] = "queued"
    return True


def _fail_bestplan_review_recovery(
    request: Dict[str, str], *, reason_code: str,
) -> bool:
    """Publish one truthful terminal integrity failure, never a pass."""

    delegation_id = str(request.get("delegation_id") or "")
    tracker_path = str(request.get("tracker_path") or "")
    job_id = str(request.get("job_id") or "")
    if not delegation_id or not tracker_path or not job_id:
        return False
    now = time.time()
    with _persist_lock:
        data = _read_persisted_unlocked(tracker_path)
        entry = (data.get("records") or {}).get(delegation_id)
        record = (
            entry.get("record")
            if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
            else None
        )
        if not isinstance(entry, dict) or not isinstance(record, dict):
            return False
        status = str(entry.get("status") or record.get("status") or "")
        if (
            status not in {"review_requeued", "review_waiting"}
            or record.get("bestplan_review_job_id") != job_id
            or record.get("bestplan_local_execution") is not True
        ):
            return False
        record = dict(record)
        record["status"] = "error"
        record["completed_at"] = now
        record["last_heartbeat_at"] = now
        record["delivery_status"] = "finalizing"
        record["review_recovery_reason_code"] = reason_code
        event = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": record.get("session_key", ""),
            "origin_ui_session_id": record.get("origin_ui_session_id", ""),
            "origin_session_id": record.get("origin_session_id", ""),
            "origin_profile": record.get("origin_profile", ""),
            "origin_tracker_path": record.get("origin_tracker_path", ""),
            "parent_session_id": record.get("parent_session_id"),
            "bestplan_plan_id": record.get("bestplan_plan_id", ""),
            "bestplan_local_execution": True,
            "resolved_runtimes": record.get("resolved_runtimes") or [],
            "goal": record.get("goal", ""),
            "goals": record.get("goals"),
            "context": record.get("context"),
            "toolsets": record.get("toolsets"),
            "role": record.get("role"),
            "model": record.get("model"),
            "status": "error",
            "is_batch": True,
            "results": [],
            "error": f"automatic review integrity failure: {reason_code}",
            "total_duration_seconds": round(
                now - float(record.get("dispatched_at") or now), 2,
            ),
            "dispatched_at": record.get("dispatched_at") or now,
            "completed_at": now,
        }
        if not _mark_bestplan_completed_unverified(record, event):
            return False
        entry["record"] = record
        entry["status"] = "error"
        entry["result"] = {
            "results": [],
            "error": event["error"],
        }
        entry["event"] = event
        entry["delivery_status"] = "pending"
        entry["updated_at"] = now
        _write_persisted_unlocked(data, tracker_path)
    if not _mark_persisted_delivery(
        delegation_id, "queued", tracker_path=tracker_path,
    ):
        return False
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(event)
    except Exception:
        _mark_persisted_delivery(
            delegation_id, "pending", tracker_path=tracker_path,
        )
        return False
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("bestplan_review_job_id") == job_id:
            live.update(record)
            live["delivery_status"] = "queued"
    return True


def consume_bestplan_review_recoveries(
    review_recovery_queue,
    *,
    worker: Callable[[Dict[str, str]], Dict[str, Any]],
    max_items: int | None = None,
) -> Dict[str, int]:
    """Drain durable review handoffs through one live, non-persisted worker."""

    if not callable(worker):
        raise TypeError("BestPlan review recovery worker must be callable")
    if max_items is not None and (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or max_items < 1
    ):
        raise ValueError("BestPlan review recovery limit is invalid")
    consumed = 0
    completed = 0
    deferred = 0
    while max_items is None or consumed < max_items:
        try:
            request = review_recovery_queue.get_nowait()
        except queue.Empty:
            break
        if not isinstance(request, dict):
            deferred += 1
            consumed += 1
            continue
        cancel_record = _durable_review_cancel_record(request)
        if (
            request.get("_cancel_finalize_only") is True
            or _durable_review_cancelled(cancel_record)
        ):
            if (
                _finalize_durable_review_cancel(cancel_record)
                and _terminalize_durable_review_cancel_tracker(request)
            ):
                consumed += 1
                continue
            _retry_durable_review_cancel_finalization(request)
            deferred += 1
            consumed += 1
            continue
        worker_request = {
            key: value
            for key, value in request.items()
            if not str(key).startswith("_")
        }
        delegation_id = str(request.get("delegation_id") or "")
        cancel_event = threading.Event()
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None and live.get("bestplan_review_job_id") == (
                request.get("job_id")
            ):
                live["interrupt_fn"] = cancel_event.set
                live["review_cancel_event"] = cancel_event
                if live.get("status") == "review_waiting":
                    live["status"] = "review_requeued"
                live["delivery_status"] = "review_requeued"
        try:
            try:
                worker_parameters = inspect.signature(worker).parameters
            except (TypeError, ValueError):
                worker_parameters = {}
            accepts_cancel = "cancel_event" in worker_parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in worker_parameters.values()
            )
            result = (
                worker(worker_request, cancel_event=cancel_event)
                if accepts_cancel
                else worker(worker_request)
            )
            if not isinstance(result, dict):
                raise TypeError("BestPlan review recovery result is invalid")
            nested_status = (
                result["result"].get("status")
                if isinstance(result.get("result"), dict)
                else None
            )
            with _records_lock:
                current = _records.get(delegation_id)
                cancelled_record = dict(current or cancel_record)
            cancelled_after_worker = (
                cancel_event.is_set()
                or _durable_review_cancelled(cancelled_record)
            )
            if cancelled_after_worker:
                if not (
                    _finalize_durable_review_cancel(cancelled_record)
                    and _terminalize_durable_review_cancel_tracker(request)
                ):
                    _retry_durable_review_cancel_finalization(request)
                    deferred += 1
            elif (
                result.get("status") == "completed"
                or nested_status == "completed"
            ):
                completion = (
                    result["result"].get("completion")
                    if isinstance(result.get("result"), dict)
                    else result.get("completion")
                )
                if not isinstance(completion, Mapping):
                    raise TypeError(
                        "BestPlan review completion is invalid"
                    )
                if _complete_bestplan_review_recovery(request, completion):
                    completed += 1
                else:
                    reason_code = "completion_persist_failed"
                    _try_defer_bestplan_review_recovery(
                        request, reason_code=reason_code,
                    )
                    _schedule_bestplan_review_recovery_retry(
                        request, reason_code=reason_code,
                    )
                    deferred += 1
            elif (
                result.get("status") == "resumed"
                and nested_status == "checkpoint_advanced"
            ):
                review_recovery_queue.put(dict(request))
            elif nested_status == "blocked_requires_authority":
                defer_persisted = _try_defer_bestplan_review_recovery(
                    request, reason_code="blocked_requires_authority",
                )
                if not defer_persisted:
                    _schedule_bestplan_review_recovery_retry(
                        request, reason_code="defer_persist_failed",
                    )
                deferred += 1
        except Exception as exc:  # fail closed; durable state remains resumable
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            cancelled = cancel_event.is_set()
            if not cancelled:
                with _records_lock:
                    current = _records.get(delegation_id)
                    cancelled = bool(
                        current is not None
                        and _durable_review_cancelled(current)
                    )
            if cancelled:
                with _records_lock:
                    current = _records.get(delegation_id)
                    cancelled_record = dict(current or {})
                if not cancelled_record:
                    cancelled_record = _durable_review_cancel_record(request)
                if (
                    _finalize_durable_review_cancel(cancelled_record)
                    and _terminalize_durable_review_cancel_tracker(request)
                ):
                    pass
                else:
                    _retry_durable_review_cancel_finalization(request)
                    deferred += 1
            elif code in _BESTPLAN_REVIEW_INTEGRITY_FAILURE_CODES:
                if not _fail_bestplan_review_recovery(
                    request, reason_code=code,
                ):
                    _try_defer_bestplan_review_recovery(
                        request, reason_code="integrity_terminalization_failed",
                    )
                    _schedule_bestplan_review_recovery_retry(
                        request,
                        reason_code="integrity_terminalization_failed",
                    )
                    deferred += 1
            else:
                defer_persisted = _try_defer_bestplan_review_recovery(
                    request, reason_code=code,
                )
                if (
                    _bestplan_review_failure_is_retryable(code)
                    or not defer_persisted
                ):
                    _schedule_bestplan_review_recovery_retry(
                        request, reason_code=code,
                    )
                deferred += 1
            logger.warning(
                "BestPlan review recovery deferred for %s (%s)",
                request.get("delegation_id") or "<unknown>",
                code,
            )
        finally:
            with _records_lock:
                live = _records.get(delegation_id)
                if (
                    live is not None
                    and live.get("review_cancel_event") is cancel_event
                ):
                    live.pop("review_cancel_event", None)
                    live["interrupt_fn"] = None
        consumed += 1
    return {
        "consumed": consumed,
        "completed": completed,
        "deferred": deferred,
    }


_BESTPLAN_REVIEW_OPERATOR_WAIT_CODES = frozenset({
    "blocked_requires_authority",
    "execution_authority_unavailable",
    "execution_owner_live",
    "execution_owner_unknown",
    "execution_runtime_drift",
    "execution_source_drift",
    "landing_target_drift",
    "review_profile_unavailable",
    "review_operator_authority_required",
    "review_runtime_fingerprint_changed",
    "review_workspace_changed",
})

_BESTPLAN_REVIEW_INTEGRITY_FAILURE_CODES = frozenset({
    "execution_intent_invalid",
    "execution_owner_identity_invalid",
    "execution_plan_invalid",
    "execution_request_invalid",
    "execution_tracker_invalid",
    "review_action_result_invalid",
    "review_adapter_invalid",
    "review_cancel_invalid",
    "review_checkpoint_incomplete",
    "review_checkpoint_invalid",
    "review_checkpoint_stale",
    "review_job_identity_changed",
    "review_plan_identity_changed",
    "review_plan_invalid",
    "review_receipt_stale",
    "review_receipts_incomplete",
    "review_request_invalid",
    "review_state_invalid",
})


def _bestplan_review_failure_is_retryable(code: str) -> bool:
    """Retry unknown operational failures unless evidence needs an operator."""

    normalized = str(code or "")
    return (
        normalized not in _BESTPLAN_REVIEW_OPERATOR_WAIT_CODES
        and normalized not in _BESTPLAN_REVIEW_INTEGRITY_FAILURE_CODES
        and normalized != "review_cancelled"
    )


def _manual_review_recovery_key(
    request: Mapping[str, object],
) -> tuple[str, str] | None:
    state_db_path = request.get("state_db_path")
    job_id = request.get("job_id")
    if not isinstance(state_db_path, str) or not isinstance(job_id, str):
        return None
    return state_db_path, job_id


def _validate_manual_review_recovery_request(
    request: Mapping[str, object],
) -> dict[str, str] | None:
    """Rebuild one manual request from the store and compare every field."""

    try:
        from agent.review_engine import build_manual_review_resume_request

        expected = build_manual_review_resume_request(
            state_db_path=request.get("state_db_path"),
            job_id=str(request.get("job_id") or ""),
        )
    except Exception:
        return None
    return expected if dict(request) == expected else None


def enqueue_manual_review_recovery(
    request: Mapping[str, object],
) -> bool:
    """Queue one exact manual review recovery request at most once."""

    validated = _validate_manual_review_recovery_request(request)
    if validated is None:
        return False
    key = _manual_review_recovery_key(validated)
    assert key is not None
    with _manual_review_recovery_pending_lock:
        if key in _manual_review_recovery_pending:
            return True
        _manual_review_recovery_pending.add(key)
    _manual_review_recovery_queue.put(validated)
    _start_manual_review_recovery_consumer()
    return True


def recover_manual_review_jobs(
    *,
    state_db_path: str | Path,
    profile: str,
    recovery_queue=_OWNED_REVIEW_RECOVERY_QUEUE,
) -> Dict[str, int]:
    """Queue recoverable manual jobs from one exact configured state store."""

    requested_profile = str(profile or "").strip()
    if not requested_profile or len(requested_profile) > 256:
        raise ValueError("manual recovery profile is invalid")
    try:
        canonical_state = Path(state_db_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("manual recovery state database is unavailable") from exc
    if not canonical_state.is_file():
        raise ValueError("manual recovery state database is unavailable")

    from agent.review_engine import (
        ReviewStore,
        build_manual_review_resume_request,
    )

    store = ReviewStore(canonical_state)
    recovery_now_ns = time.time_ns()
    store.finalize_expired_manual_cancellations(
        owner_profile=requested_profile,
        now_ns=recovery_now_ns,
    )
    jobs = store.list_recoverable_manual_jobs(
        owner_profile=requested_profile,
        now_ns=recovery_now_ns,
    )
    use_owned_queue = recovery_queue is _OWNED_REVIEW_RECOVERY_QUEUE
    target_queue = (
        _manual_review_recovery_queue if use_owned_queue else recovery_queue
    )
    if not hasattr(target_queue, "put"):
        raise TypeError("manual recovery queue is invalid")

    queued = 0
    for job in jobs:
        request = build_manual_review_resume_request(
            state_db_path=canonical_state,
            job_id=job.job_id,
        )
        if request.get("profile") != requested_profile:
            raise ValueError("manual recovery profile differs from durable owner")
        if use_owned_queue:
            key = _manual_review_recovery_key(request)
            assert key is not None
            with _manual_review_recovery_pending_lock:
                if key in _manual_review_recovery_pending:
                    continue
                _manual_review_recovery_pending.add(key)
        target_queue.put(request)
        queued += 1

    if use_owned_queue and queued:
        _start_manual_review_recovery_consumer()
    return {"queued": queued}


def _manual_review_requires_operator(
    request: Mapping[str, object],
    result: Mapping[str, object],
) -> bool:
    if str(result.get("review_state") or "") == "blocked_requires_authority":
        return True
    try:
        from agent.review_engine import ReviewStore

        events = ReviewStore(str(request["state_db_path"])).list_events(
            str(request["job_id"])
        )
    except Exception:
        return False
    return bool(events and events[-1].kind in {
        "blocked_requires_authority",
        "target_drift",
    })


def _schedule_manual_review_recovery_retry(
    request: dict[str, str],
    *,
    attempts: int,
) -> None:
    delay = min(30.0, 0.25 * (2 ** min(max(attempts, 0), 7)))
    retry_request: dict[str, object] = dict(request)
    retry_request["_transient_attempt"] = attempts + 1

    def requeue() -> None:
        _manual_review_recovery_queue.put(retry_request)
        _manual_review_recovery_wake.set()

    timer = threading.Timer(delay, requeue)
    timer.name = "manual-review-retry"
    timer.daemon = True
    timer.start()


def _terminalize_manual_review_integrity_failure(
    request: Mapping[str, object],
    exc: BaseException,
) -> bool:
    """Persist one corrupt manual recovery as a truthful terminal failure."""

    try:
        from agent.review_engine import ReviewStore

        ReviewStore(str(request["state_db_path"])).terminalize_manual_recovery_integrity(
            job_id=str(request["job_id"]),
            reason_code=type(exc).__name__,
        )
        return True
    except Exception:
        logger.error(
            "Manual review integrity failure could not be terminalized for %s",
            request.get("job_id") or "<unknown>",
            exc_info=True,
        )
        return False


def consume_manual_review_recoveries(
    recovery_queue,
    *,
    worker: Callable[[Dict[str, str]], Dict[str, Any]],
    max_items: int | None = None,
) -> Dict[str, int]:
    """Drain exact manual requests with durable retry and no callback reuse."""

    if not callable(worker):
        raise TypeError("manual review recovery worker must be callable")
    if max_items is not None and (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or max_items < 1
    ):
        raise ValueError("manual review recovery limit is invalid")
    consumed = 0
    completed = 0
    deferred = 0
    while max_items is None or consumed < max_items:
        try:
            raw_request = recovery_queue.get_nowait()
        except queue.Empty:
            break
        consumed += 1
        transient_attempt = 0
        validation_request = raw_request
        if isinstance(raw_request, Mapping):
            validation_request = dict(raw_request)
            raw_attempt = validation_request.pop("_transient_attempt", 0)
            if (
                isinstance(raw_attempt, bool)
                or not isinstance(raw_attempt, int)
                or raw_attempt < 0
            ):
                validation_request = {}
            else:
                transient_attempt = raw_attempt
        validated = (
            _validate_manual_review_recovery_request(validation_request)
            if isinstance(validation_request, Mapping)
            else None
        )
        if validated is None:
            if isinstance(raw_request, Mapping):
                invalid_key = _manual_review_recovery_key(raw_request)
                if invalid_key is not None:
                    with _manual_review_recovery_pending_lock:
                        _manual_review_recovery_pending.discard(invalid_key)
            deferred += 1
            continue
        key = _manual_review_recovery_key(validated)
        assert key is not None
        with _manual_review_recovery_pending_lock:
            _manual_review_recovery_pending.add(key)
        should_retry = False
        operator_wait = False
        integrity_failed = False
        item_completed = False
        try:
            result = worker(dict(validated))
            if not isinstance(result, dict):
                raise TypeError("manual review recovery result is invalid")
            if result.get("completed") is True:
                item_completed = True
                completed += 1
            else:
                operator_wait = _manual_review_requires_operator(
                    validated, result
                )
                should_retry = not operator_wait
                deferred += 1
        except Exception as exc:
            from agent.review_engine import (
                ReviewLeaseConflict,
                ReviewRequiresAuthority,
                ReviewStoreConflict,
                ReviewValidationError,
            )

            if isinstance(exc, ReviewRequiresAuthority):
                operator_wait = True
            elif isinstance(exc, ReviewLeaseConflict):
                should_retry = True
            elif isinstance(exc, (ReviewStoreConflict, ReviewValidationError)):
                integrity_failed = _terminalize_manual_review_integrity_failure(
                    validated, exc,
                )
                should_retry = not integrity_failed
            else:
                should_retry = True
            deferred += 1
            if should_retry:
                logger.warning(
                    "Manual review recovery deferred for %s",
                    validated.get("job_id") or "<unknown>",
                    exc_info=True,
                )
        if item_completed or operator_wait or integrity_failed:
            with _manual_review_recovery_pending_lock:
                _manual_review_recovery_pending.discard(key)
        elif should_retry:
            _schedule_manual_review_recovery_retry(
                validated, attempts=transient_attempt
            )
    return {
        "completed": completed,
        "consumed": consumed,
        "deferred": deferred,
    }


def _default_manual_review_recovery_worker(
    request: Dict[str, str],
) -> Dict[str, Any]:
    """Rebuild a fresh manual host from the owning profile and state store."""

    from types import SimpleNamespace

    from gateway.run import _profile_runtime_scope
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
    from hermes_state import SessionDB
    from agent.review_engine import resume_manual_review_job

    profile = str(request.get("profile") or "").strip()
    try:
        profile_home = get_profile_dir(normalize_profile_name(profile)).resolve(
            strict=True
        )
        state_db_path = Path(request["state_db_path"]).resolve(strict=True)
        if state_db_path != (profile_home / "state.db").resolve(strict=False):
            raise ValueError("manual recovery state is outside its profile")
    except (KeyError, OSError, RuntimeError, ValueError):
        raise RuntimeError("manual_review_profile_unavailable") from None
    with _profile_runtime_scope(profile_home):
        database = SessionDB(db_path=state_db_path)
        agent = SimpleNamespace(
            session_id=request["session_id"],
            _session_db=database,
            platform="manual_recovery",
        )
        return resume_manual_review_job(agent, request)


def _manual_review_recovery_consumer() -> None:
    while True:
        _manual_review_recovery_wake.wait()
        _manual_review_recovery_wake.clear()
        try:
            consume_manual_review_recoveries(
                _manual_review_recovery_queue,
                worker=_default_manual_review_recovery_worker,
            )
        except Exception:
            logger.error(
                "Manual review recovery drain failed; consumer remains live",
                exc_info=True,
            )


def _start_manual_review_recovery_consumer() -> None:
    global _manual_review_recovery_thread
    with _manual_review_recovery_thread_lock:
        thread = _manual_review_recovery_thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=_manual_review_recovery_consumer,
                name="manual-review-recovery",
                daemon=True,
            )
            _manual_review_recovery_thread = thread
            thread.start()
    _manual_review_recovery_wake.set()


def _profile_home_for_bestplan_recovery(
    request: Mapping[str, object],
) -> Path:
    """Resolve one owning profile and its exact configured state database."""

    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
    from hermes_constants import get_hermes_home
    from tools.delegate_tool import BestplanReviewRecoveryDeferred

    requested_profile = str(request.get("profile") or "").strip()
    try:
        if requested_profile:
            profile_home = get_profile_dir(
                normalize_profile_name(requested_profile)
            )
            if (
                requested_profile.casefold() != "default"
                and not profile_home.is_dir()
            ):
                raise ValueError("owning profile is unavailable")
        else:
            profile_home = get_hermes_home()
        profile_home = profile_home.resolve(strict=True)
        state_db_path = Path(str(request["state_db_path"])).resolve(
            strict=True
        )
        if state_db_path != (profile_home / "state.db").resolve(
            strict=False
        ):
            raise ValueError("BestPlan recovery state is outside its profile")
    except KeyError:
        raise BestplanReviewRecoveryDeferred(
            "review_state_unavailable"
        ) from None
    except (OSError, RuntimeError, ValueError):
        raise BestplanReviewRecoveryDeferred(
            "review_profile_unavailable"
        ) from None
    return profile_home


def _default_bestplan_review_recovery_worker(
    request: Dict[str, str],
    *,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
    """Resume through live config; no authority or credential is persisted."""

    from gateway.run import _profile_runtime_scope
    from tools.delegate_tool import (
        BestplanReviewRecoveryDeferred,
        LocalBestplanReviewRecoveryAdapter,
        resume_bestplan_execution_request,
        resume_bestplan_review_request,
    )

    profile_home = _profile_home_for_bestplan_recovery(request)

    with _profile_runtime_scope(profile_home):
        if request.get("kind") == "bestplan_execution_resume":
            try:
                tracker_path = str(request["tracker_path"])
                payload = json.loads(
                    Path(tracker_path).read_text(encoding="utf-8")
                )
                record = payload["records"][request["delegation_id"]][
                    "record"
                ]
                current_request = _bestplan_review_resume_request(
                    record, tracker_path=tracker_path,
                )
            except Exception:
                raise BestplanReviewRecoveryDeferred(
                    "execution_tracker_invalid"
                ) from None
            if current_request is None:
                raise BestplanReviewRecoveryDeferred(
                    "execution_tracker_invalid"
                )
            if current_request.get("kind") == "bestplan_review_resume":
                adapter = LocalBestplanReviewRecoveryAdapter()
                if cancel_event is not None:
                    adapter.bind_cancel_event(cancel_event)
                return resume_bestplan_review_request(
                    current_request, adapter=adapter,
                )
            if current_request != request:
                raise BestplanReviewRecoveryDeferred(
                    "execution_request_invalid"
                )
            return resume_bestplan_execution_request(
                request, cancel_event=cancel_event,
            )
        if request.get("kind") != "bestplan_review_resume":
            raise BestplanReviewRecoveryDeferred("review_request_invalid")
        adapter = LocalBestplanReviewRecoveryAdapter()
        if cancel_event is not None:
            adapter.bind_cancel_event(cancel_event)
        return resume_bestplan_review_request(request, adapter=adapter)


def _schedule_bestplan_review_recovery_retry(
    request: Dict[str, str],
    *,
    reason_code: str,
) -> None:
    """Wake one transient recovery again with a bounded exponential delay."""

    retry_request = dict(request)
    attempts = retry_request.get("_transient_attempt", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        attempts = 0
    attempts = min(attempts + 1, 8)
    retry_request["_transient_attempt"] = attempts
    delay = min(30.0, 0.25 * (2 ** (attempts - 1)))

    def requeue() -> None:
        _bestplan_review_recovery_queue.put(retry_request)
        _bestplan_review_recovery_wake.set()

    timer = threading.Timer(delay, requeue)
    timer.name = f"bestplan-review-retry-{reason_code}"
    timer.daemon = True
    timer.start()


def _bestplan_review_recovery_consumer() -> None:
    """Drain every startup/live recovery wake on one daemon worker."""

    while True:
        _bestplan_review_recovery_wake.wait()
        _bestplan_review_recovery_wake.clear()
        try:
            consume_bestplan_review_recoveries(
                _bestplan_review_recovery_queue,
                worker=_default_bestplan_review_recovery_worker,
            )
        except Exception:
            logger.error(
                "BestPlan review recovery drain failed; consumer remains live",
                exc_info=True,
            )


def _start_bestplan_review_recovery_consumer() -> None:
    """Start the owned recovery consumer once and wake it after queue writes."""

    global _bestplan_review_recovery_thread
    with _bestplan_review_recovery_thread_lock:
        thread = _bestplan_review_recovery_thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=_bestplan_review_recovery_consumer,
                name="bestplan-review-recovery",
                daemon=True,
            )
            _bestplan_review_recovery_thread = thread
            thread.start()
    _bestplan_review_recovery_wake.set()


def restore_undelivered_completions(target_queue) -> int:
    """Restore durable completion events onto the registry's supplied queue."""
    result = recover_async_delegations(
        target_queue=target_queue,
        mark_restored=True,
        review_recovery_queue=_bestplan_review_recovery_queue,
    )
    _start_bestplan_review_recovery_consumer()
    queued = int(result.get("queued", 0))
    configured_state: Path | None = None
    try:
        candidate_state = Path(_db_path()).expanduser().resolve(strict=True)
        if candidate_state.is_file():
            configured_state = candidate_state
            from hermes_cli.profiles import get_active_profile_name

            recover_manual_review_jobs(
                state_db_path=configured_state,
                profile=get_active_profile_name(),
            )
    except Exception:
        logger.debug("Manual review startup recovery failed", exc_info=True)
    # Older gateway producers persisted their completion in SQLite. Read that
    # ledger only when it exists so normal JSON checkpoint startup remains
    # side-effect free, while restart recovery still covers both formats.
    try:
        if configured_state is not None:
            queued += _restore_sqlite_undelivered(target_queue)
    except Exception:
        logger.debug("SQLite async completion restore failed", exc_info=True)
    return queued


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

    # First pass: age-based pruning by terminal status. Active records are
    # never removed here; a live process should keep surfacing them even if
    # heartbeat looks stale.
    for rid, record in list(_records.items()):
        status = str(record.get("status") or "")
        if _is_retained_nonterminal(record, status=status):
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
        if not _is_retained_nonterminal(r)
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


def _ensure_stale_monitor() -> None:
    """Start the shared progress-based stale-delegation monitor."""
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop,
            name="async-delegate-stale-monitor",
            daemon=True,
        )
        _monitor_thread.start()


def _stale_monitor_loop() -> None:
    """Interrupt frozen progress, then finalize children that never unwind."""
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        now = time.time()
        stalled: List[tuple[str, bool, float, bool]] = []
        expired: List[str] = []
        any_monitorable = False
        with _records_lock:
            for record in _records.values():
                status = record.get("status")
                if status == "stalling":
                    any_monitorable = True
                    interrupted_at = record.get("_interrupted_at") or now
                    if now - interrupted_at >= _STALL_GRACE_SECONDS:
                        expired.append(str(record.get("delegation_id") or ""))
                    continue
                if status != "running":
                    continue
                progress_fn = record.get("progress_fn")
                if progress_fn is None:
                    continue
                any_monitorable = True
                try:
                    token, in_tool = progress_fn()
                except Exception:
                    token, in_tool = record.get("_progress_token"), False
                if token != record.get("_progress_token"):
                    record["_progress_token"] = token
                    record["_progress_ts"] = now
                    continue
                quiet_for = now - (record.get("_progress_ts") or now)
                limit = _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
                if quiet_for >= limit:
                    record["status"] = "stalling"
                    record["delivery_status"] = "stalling"
                    record["_interrupted_at"] = now
                    record["_stall_quiet_seconds"] = round(quiet_for, 2)
                    record["_stall_threshold_seconds"] = limit
                    record["_stall_in_tool"] = bool(in_tool)
                    stalled.append(
                        (
                            str(record.get("delegation_id") or ""),
                            bool(record.get("is_batch")),
                            quiet_for,
                            bool(in_tool),
                        )
                    )
        for delegation_id, _is_batch, quiet_for, in_tool in stalled:
            logger.warning(
                "Async delegation %s made no progress for %.0fs (in_tool=%s) "
                "— interrupting; grace window %.0fs",
                delegation_id,
                quiet_for,
                in_tool,
                _STALL_GRACE_SECONDS,
            )
            with _records_lock:
                record = _records.get(delegation_id)
                fn = record.get("interrupt_fn") if record else None
            if callable(fn):
                try:
                    fn()
                except Exception as exc:
                    logger.debug(
                        "Async delegation %s stall interrupt failed: %s",
                        delegation_id,
                        exc,
                    )
        for delegation_id in expired:
            _finalize_stalled(delegation_id)
        if not any_monitorable:
            return


def _finalize_stalled(delegation_id: str) -> None:
    """Force-finalize a stalled child whose runner never returned."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") != "stalling":
            return
        completed_at = time.time()
        duration = round(
            completed_at - (record.get("dispatched_at") or completed_at), 2
        )
        quiet_seconds = record.get("_stall_quiet_seconds")
        threshold_seconds = record.get("_stall_threshold_seconds")
        stall_in_tool = record.get("_stall_in_tool")
        is_batch = bool(record.get("is_batch"))
    error = (
        f"Async delegation {delegation_id} stalled: the detached subagent "
        "stopped making progress (no new API calls, tool activity, or "
        "streamed tokens), did not respond to interruption, and never "
        "produced a completion event. Re-dispatch the task if it is still needed."
    )
    terminal_result = {
        "status": "stalled",
        "summary": None,
        "error": error,
        "api_calls": 0,
        "duration_seconds": duration,
        "exit_reason": "stalled",
        "stalled_after_quiet_seconds": quiet_seconds,
        "stall_threshold_seconds": threshold_seconds,
        "stall_phase": (
            "in_tool" if stall_in_tool
            else "idle" if stall_in_tool is not None
            else None
        ),
        "stall_grace_seconds": _STALL_GRACE_SECONDS,
    }
    if is_batch:
        _finalize_batch(
            delegation_id,
            {
                **terminal_result,
                "results": [],
                "total_duration_seconds": duration,
            },
            "stalled",
        )
    else:
        _finalize(delegation_id, terminal_result, "stalled")


def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Best-effort projection of a progress token for status UIs."""
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if isinstance(part, (list, tuple)) and len(part) >= 2:
            entry: Dict[str, Any] = {
                "api_calls": part[0],
                "current_tool": part[1],
            }
            if len(part) >= 3 and isinstance(part[2], (int, float)):
                entry["seconds_since_activity"] = round(
                    max(0.0, now - float(part[2])), 1
                )
            out.append(entry)
        else:
            out.append(None)
    return out


def _serialise_record(record: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Return a JSON-safe status snapshot with derived liveness fields."""
    out = {
        k: v
        for k, v in record.items()
        if k
        not in {
            "interrupt_fn",
            "heartbeat_stop",
            "progress_fn",
            "_terminal_callback",
            "bestplan_state_db_path",
        }
        and not k.startswith("_")
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
    if record.get("status") in {"running", "stalling"}:
        progress_ts = record.get("_progress_ts")
        if progress_ts:
            out["seconds_since_progress"] = round(
                max(0.0, now - float(progress_ts)), 1
            )
        progress_fn = record.get("progress_fn")
        if callable(progress_fn):
            try:
                token, in_tool = progress_fn()
                activity = _children_activity_from_token(token, now)
                if activity is not None:
                    out["children_activity"] = activity
                out["in_tool"] = bool(in_tool)
            except Exception:
                pass
    if record.get("status") in {"stalling", "stalled"}:
        for source, target in (
            ("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
            ("_stall_threshold_seconds", "stall_threshold_seconds"),
            ("_stall_in_tool", "stall_in_tool"),
        ):
            if record.get(source) is not None:
                out[target] = record.get(source)
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
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
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
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "status": "scheduled",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "last_heartbeat_at": dispatched_at,
        "heartbeat_count": 0,
        "delivery_status": "scheduled",
        "interrupt_fn": interrupt_fn,
        "progress_fn": progress_fn,
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
        "owner_pid": os.getpid(),
        "owner_started_at": _process_start_time(os.getpid()),
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1
            for r in _records.values()
            if r.get("status") in _ACTIVE_STATUSES
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

    executor = _get_executor(max_async_children)
    start_gate = threading.Event()
    execute_gate = threading.Event()
    abort_gate = threading.Event()
    running_checkpoint_ready = threading.Event()
    running_checkpoint = {"ok": False}

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
        running_checkpoint["ok"] = _persist_record(
            running_record, delivery_status="running"
        )
        if not running_checkpoint["ok"]:
            with _records_lock:
                live = _records.get(delegation_id)
                interrupted_snapshot = (
                    dict(live)
                    if live is not None and live.get("status") == "interrupting"
                    else None
                )
            # A successful interrupt may durably advance the record while this
            # worker still holds a stale running snapshot. Accept that
            # pre-execution state only when the interrupting checkpoint is
            # actually on disk; otherwise fail closed as usual.
            running_checkpoint["ok"] = bool(
                interrupted_snapshot is not None
                and _persisted_status(interrupted_snapshot) == "interrupting"
            )
        running_checkpoint_ready.set()
        execute_gate.wait()
        if abort_gate.is_set() or not running_checkpoint["ok"]:
            _remove_persisted_record(delegation_id)
            with _records_lock:
                _records.pop(delegation_id, None)
            return

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
        # Submit a gated worker before writing any durable accepted state.  A
        # rejected executor submission therefore cannot leave a disk phantom.
        future = executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    if not _persist_record(record, delivery_status="scheduled"):
        abort_gate.set()
        start_gate.set()
        execute_gate.set()
        future.cancel()
        with _records_lock:
            _records.pop(delegation_id, None)
        _remove_persisted_record(delegation_id)
        return {
            "status": "rejected",
            "error": "Async delegation scheduled state could not be persisted",
        }

    start_gate.set()
    if not running_checkpoint_ready.wait(
        timeout=_RUNNING_CHECKPOINT_TIMEOUT_SECONDS
    ):
        abort_gate.set()
        execute_gate.set()
        return {
            "status": "rejected",
            "error": "Async delegation running checkpoint timed out",
        }
    if not running_checkpoint["ok"]:
        abort_gate.set()
        execute_gate.set()
        return {
            "status": "rejected",
            "error": "Async delegation running state could not be persisted",
        }

    heartbeat_stop = _start_heartbeat_thread(delegation_id)
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("status") == "running":
            live["heartbeat_stop"] = heartbeat_stop
        else:
            heartbeat_stop.set()
    execute_gate.set()
    if progress_fn is not None:
        _ensure_stale_monitor()

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
        # If the record was already terminalized (e.g. by interrupt_all),
        # don't overwrite it or push a duplicate completion event.
        if record.get("status") not in _ACTIVE_STATUSES:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["last_heartbeat_at"] = record["completed_at"]
        record["delivery_status"] = "finalizing"
        hb_stop = record.get("heartbeat_stop")
        if hasattr(hb_stop, "set"):
            hb_stop.set()
        record["interrupt_fn"] = None  # drop the closure; child is done
        terminal_callback = record.get("_terminal_callback")
        record["_terminal_callback"] = None
        # Snapshot fields needed for the event while holding the lock.
        event_record = dict(record)
        _prune_completed_locked()
        _cleanup_locked(now=record["completed_at"])

    _invoke_terminal_callback(terminal_callback, result, status)
    _push_completion_event(event_record, result, status)


def _invoke_terminal_callback(
    callback: Optional[Callable[[Dict[str, Any], str], None]],
    result: Dict[str, Any],
    status: str,
) -> None:
    """Invoke one non-durable terminal callback without breaking delivery."""
    if not callable(callback):
        return
    try:
        callback(result, status)
    except Exception:
        logger.warning(
            "Async delegation process-local terminal callback failed",
            exc_info=True,
        )


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
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
        "origin_session_id": record.get("origin_session_id", ""),
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
    # Routing origin captured at dispatch (see _capture_routing_origin):
    # additive, lets the gateway reconstruct a full SessionSource (incl.
    # scope_id for relay tenant egress) when its own caches are cold.
    for _k in ("scope_id", "user_id", "user_name"):
        if record.get(_k):
            evt[_k] = record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for key in (
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if key in result:
            evt[key] = result[key]
    _persist_and_queue_terminal(record, result, evt)


def _set_delivery_failure(delegation_id: str, error: str) -> None:
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None:
            live["delivery_status"] = "pending"
            live["delivery_error"] = error


def _persist_and_queue_terminal(
    record: Dict[str, Any],
    result: Dict[str, Any],
    evt: Dict[str, Any],
    *,
    publish: bool = True,
) -> bool:
    """Publish only after terminal state and queued delivery are durable."""
    delegation_id = str(record.get("delegation_id") or "")
    tracker_path = str(record.get("origin_tracker_path") or "") or None
    if not _persist_record(
        record, result=result, event=evt, delivery_status="pending"
    ):
        error = "terminal persistence failed; event not published"
        _set_delivery_failure(delegation_id, error)
        logger.error("Async delegation %s: %s", delegation_id, error)
        return False
    if not publish:
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live[_BESTPLAN_TERMINALIZATION_PENDING] = True
                live["delivery_status"] = "pending"
                live.pop("delivery_error", None)
        return True
    if not _mark_persisted_delivery(
        delegation_id, "queued", tracker_path=tracker_path
    ):
        error = "queued delivery persistence failed; event not published"
        _set_delivery_failure(delegation_id, error)
        logger.error("Async delegation %s: %s", delegation_id, error)
        return False
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        _mark_persisted_delivery(
            delegation_id, "pending", tracker_path=tracker_path
        )
        error = f"completion queue publication failed: {exc}"
        _set_delivery_failure(delegation_id, error)
        logger.error("Async delegation %s: %s", delegation_id, error)
        return False
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None:
            live["delivery_status"] = "queued"
            live["queued_at"] = time.time()
            live.pop("delivery_error", None)
    return True


def _canonical_bestplan_state_db_path(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("BestPlan state database locator must be path-like")
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise ValueError("BestPlan state database locator must be text")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("BestPlan state database locator has invalid text") from None
    if (
        not encoded
        or len(encoded) > 4096
        or "\x00" in raw
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
    ):
        raise ValueError("BestPlan state database locator is out of bounds")
    path = Path(raw)
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("BestPlan state database locator is invalid") from None
    if not path.is_absolute() or str(canonical) != raw:
        raise ValueError("BestPlan state database locator is not canonical")
    return raw


def _mark_bestplan_completed_unverified(
    record: Dict[str, Any],
    event: Dict[str, Any],
) -> bool:
    plan_id = str(record.get("bestplan_plan_id") or "")
    if not plan_id:
        return False
    tracker_path = str(record.get("origin_tracker_path") or "")
    handed_off_path = record.get("bestplan_state_db_path")
    if record.get("bestplan_local_execution") is True and not handed_off_path:
        return False
    if not handed_off_path and not tracker_path:
        return False
    try:
        from agent.bestplan_state import BestplanStore

        resolved_path = (
            Path(_canonical_bestplan_state_db_path(handed_off_path))
            if handed_off_path
            else Path(tracker_path).parent / "state.db"
        )
        plan_store = BestplanStore(db_path=resolved_path)
        try:
            completed = bool(
                plan_store.mark_completed_unverified(plan_id, event)
            )
        finally:
            plan_store.close()
        if not completed:
            logger.warning(
                "BestPlan completion evidence persistence rejected for %s",
                plan_id,
            )
        return completed
    except Exception:
        logger.warning(
            "BestPlan completion evidence persistence failed for %s",
            plan_id,
            exc_info=True,
        )
        return False


def _mark_bestplan_cancelled_terminal(record: Dict[str, Any]) -> bool:
    """Close the canonical plan once for an extinct cancelled delegation."""

    plan_id = str(record.get("bestplan_plan_id") or "")
    state_path = record.get("bestplan_state_db_path")
    if not plan_id or not state_path:
        return False
    try:
        from agent.bestplan_state import BestplanStore, PlanState

        plan_store = BestplanStore(
            db_path=Path(_canonical_bestplan_state_db_path(state_path))
        )
        try:
            row = plan_store.get_plan(plan_id)
        finally:
            plan_store.close()
        if not isinstance(row, Mapping):
            return False
        if (
            row.get("state")
            in {PlanState.COMPLETED_UNVERIFIED, PlanState.FAILED}
            and row.get("dispatch_state") == "terminal"
            and not row.get("dispatch_owner")
        ):
            return True
    except Exception:
        logger.warning(
            "BestPlan cancelled terminal state could not be read for %s",
            plan_id,
            exc_info=True,
        )
        return False
    return _mark_bestplan_completed_unverified(
        record, _event_for_interrupted_record(record),
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
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
    delegation_id: Optional[str] = None,
    origin_profile: str = "",
    origin_tracker_path: str = "",
    bestplan_plan_id: str = "",
    bestplan_state_db_path: str = "",
    bestplan_review_job_id: str = "",
    bestplan_local_execution: bool = False,
    resolved_runtimes: Optional[List[Dict[str, Any]]] = None,
    terminal_callback: Optional[
        Callable[[Dict[str, Any], str], None]
    ] = None,
) -> Dict[str, Any]:
    """Atomically admit one deterministic-ID batch dispatch."""
    try:
        canonical_state_db_path = _canonical_bestplan_state_db_path(
            bestplan_state_db_path
        )
    except ValueError:
        return {
            "status": "rejected",
            "error": "bestplan_state_locator_invalid",
        }
    resolved_id = str(delegation_id or _new_delegation_id())
    admission_lock = _DISPATCH_ADMISSION_LOCKS[
        hash(resolved_id) % len(_DISPATCH_ADMISSION_LOCKS)
    ]
    with admission_lock:
        return _dispatch_async_delegation_batch_admitted(
            goals=goals,
            context=context,
            toolsets=toolsets,
            role=role,
            model=model,
            session_key=session_key,
            parent_session_id=parent_session_id,
            runner=runner,
            origin_ui_session_id=origin_ui_session_id,
            origin_session_id=origin_session_id,
            interrupt_fn=interrupt_fn,
            max_async_children=max_async_children,
            progress_fn=progress_fn,
            delegation_id=resolved_id,
            origin_profile=origin_profile,
            origin_tracker_path=origin_tracker_path,
            bestplan_plan_id=bestplan_plan_id,
            bestplan_state_db_path=canonical_state_db_path,
            bestplan_review_job_id=bestplan_review_job_id,
            bestplan_local_execution=(
                bool(bestplan_plan_id) and bestplan_local_execution is True
            ),
            resolved_runtimes=resolved_runtimes,
            terminal_callback=terminal_callback,
        )


def _dispatch_async_delegation_batch_admitted(
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
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
    delegation_id: Optional[str] = None,
    origin_profile: str = "",
    origin_tracker_path: str = "",
    bestplan_plan_id: str = "",
    bestplan_state_db_path: str = "",
    bestplan_review_job_id: str = "",
    bestplan_local_execution: bool = False,
    resolved_runtimes: Optional[List[Dict[str, Any]]] = None,
    terminal_callback: Optional[
        Callable[[Dict[str, Any], str], None]
    ] = None,
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
    with _records_lock:
        local_existing = _records.get(delegation_id)
        if local_existing and local_existing.get("acceptance_aborted"):
            return {
                "status": "rejected",
                "delegation_id": delegation_id,
                "error": "previous strict dispatch acceptance was aborted before execution",
            }
    if origin_tracker_path:
        with _persist_lock:
            persisted = _read_persisted_unlocked(origin_tracker_path)
            existing = (persisted.get("records") or {}).get(delegation_id)
        if isinstance(existing, dict):
            phase = str(existing.get("status") or (existing.get("record") or {}).get("status") or "")
            if phase in _ACTIVE_STATUSES or phase in _DURABLE_NONTERMINAL_STATUSES:
                owner_record = existing.get("record") or {}
                owner_liveness = _owner_liveness(owner_record)
                if phase == "scheduled" and owner_liveness is False:
                    phase = "intent"
                elif phase in {"running", "interrupting"} and owner_liveness is False:
                    # Running means side effects may have begun. A dead owner is
                    # terminally ambiguous: recover it as lost, never retry or
                    # falsely ACK it as a live idempotent dispatch.
                    recover_async_delegations(origin_tracker_path)
                    with _persist_lock:
                        recovered = _read_persisted_unlocked(origin_tracker_path)
                        existing = (recovered.get("records") or {}).get(
                            delegation_id, existing
                        )
                    phase = str(
                        existing.get("status")
                        or (existing.get("record") or {}).get("status")
                        or "lost"
                    )
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
    from agent.bestplan_state import sanitize_runtime_metadata

    safe_resolved_runtimes = sanitize_runtime_metadata(
        resolved_runtimes or [],
        execution_protocol=2 if bestplan_plan_id else 1,
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
        "origin_session_id": origin_session_id,
        **_capture_routing_origin(),
        "progress_fn": progress_fn,
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
        "origin_profile": origin_profile,
        "origin_tracker_path": origin_tracker_path,
        "parent_session_id": parent_session_id,
        "bestplan_plan_id": bestplan_plan_id,
        "bestplan_state_db_path": bestplan_state_db_path,
        "bestplan_review_job_id": (
            str(bestplan_review_job_id)
            if bestplan_local_execution is True and bestplan_plan_id
            else ""
        ),
        "bestplan_local_execution": (
            bool(bestplan_plan_id) and bestplan_local_execution is True
        ),
        "resolved_runtimes": safe_resolved_runtimes,
        "status": "intent",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "last_heartbeat_at": dispatched_at,
        "heartbeat_count": 0,
        "delivery_status": "intent",
        "interrupt_fn": interrupt_fn,
        "_terminal_callback": terminal_callback,
        "is_batch": True,
        "owner_pid": os.getpid(),
        "owner_started_at": _process_start_time(os.getpid()),
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") in _ACTIVE_STATUSES
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
    if not _persist_record(record, delivery_status="intent"):
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": "async dispatch intent could not be persisted",
        }
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
    running_checkpoint_ready = threading.Event()
    execute_gate = threading.Event()
    running_checkpoint = {"ok": False, "error": ""}

    def _repair_pre_execution_intent(reason: str) -> bool:
        with _records_lock:
            live = _records.get(delegation_id)
            if live is None:
                return False
            live["status"] = "intent"
            live["delivery_status"] = "intent"
            # Keep retries rejected until the durable intent repair succeeds.
            live["acceptance_aborted"] = True
            live["last_error"] = reason
            repaired = dict(live)
        # A pre-execution rollback is the one legitimate backwards transition.
        # Remove the active checkpoint first so monotonic persistence cannot
        # confuse it with a stale writer racing a terminal record.
        if not _remove_persisted_record(
            delegation_id, tracker_path=origin_tracker_path or None
        ):
            return False
        persisted = _persist_record(repaired, delivery_status="intent")
        if persisted:
            with _records_lock:
                live = _records.get(delegation_id)
                if live is not None and live.get("status") == "intent":
                    live["acceptance_aborted"] = False
        return persisted

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
        running_persisted = _persist_record(
            running_record, delivery_status="running"
        )
        if not running_persisted:
            with _records_lock:
                live = _records.get(delegation_id)
                interrupted_snapshot = (
                    dict(live)
                    if live is not None and live.get("status") == "interrupting"
                    else None
                )
            if (
                interrupted_snapshot is not None
                and _persisted_status(interrupted_snapshot) == "interrupting"
            ):
                running_checkpoint["ok"] = True
            else:
                repaired = _repair_pre_execution_intent(
                    "durable running checkpoint failed before execution"
                )
                running_checkpoint["error"] = (
                    "async running checkpoint could not be persisted"
                    + ("" if repaired else "; intent repair also failed")
                )
            running_checkpoint_ready.set()
            return
        running_checkpoint["ok"] = True
        running_checkpoint_ready.set()
        execute_gate.wait()
        if abort_gate.is_set():
            _repair_pre_execution_intent(
                "dispatch acceptance timed out before execution"
            )
            return
        combined: Dict[str, Any] = {}
        status = "error"
        review_handed_off = False
        try:
            combined = runner() or {}
            child_results = combined.get("results") or []
            if bestplan_plan_id:
                # Only the host-owned BestPlan coordinator may use the frozen
                # result phase; ordinary delegated workers retain the legacy
                # completed/success contract.
                status = (
                    (
                        "completed"
                        if bestplan_local_execution
                        else "candidate_ready"
                    )
                    if child_results
                    and all(r.get("status") == "frozen" for r in child_results)
                    else "error"
                )
            elif child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            reason_code = str(
                getattr(exc, "code", "") or type(exc).__name__
            )
            if bestplan_local_execution and _handoff_running_bestplan_review(
                delegation_id, reason_code=reason_code,
            ):
                review_handed_off = True
            else:
                logger.exception(
                    "Async delegation batch %s crashed", delegation_id
                )
                combined = {
                    "results": [],
                    "error": (
                        "candidate_batch_failed"
                        if bestplan_plan_id
                        else f"{type(exc).__name__}: {exc}"
                    ),
                    "total_duration_seconds": round(
                        time.time() - dispatched_at, 2
                    ),
                }
                status = "error"
        finally:
            if not review_handed_off:
                with _records_lock:
                    terminal_record = dict(
                        _records.get(delegation_id) or {}
                    )
                if _finalize_durable_review_cancel(terminal_record):
                    combined = {
                        "results": [],
                        "error": "interrupted after child extinction",
                        "total_duration_seconds": round(
                            time.time() - dispatched_at, 2
                        ),
                    }
                    status = "interrupted"
                _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        future = executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        _remove_persisted_record(
            delegation_id, tracker_path=origin_tracker_path or None
        )
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
    scheduled_persisted = False
    if scheduled_record is not None:
        scheduled_persisted = _persist_record(
            scheduled_record, delivery_status="scheduled"
        )
    durable_scheduled = scheduled_persisted
    if durable_scheduled and origin_tracker_path:
        with _persist_lock:
            verified = _read_persisted_unlocked(origin_tracker_path)
            durable = (verified.get("records") or {}).get(delegation_id) or {}
        durable_scheduled = str(durable.get("status") or "") == "scheduled"
    if not durable_scheduled:
        abort_gate.set()
        start_gate.set()
        execute_gate.set()
        future.cancel()
        with _records_lock:
            live = _records.get(delegation_id)
            live_status = str(live.get("status") or "") if live else ""
            if live is not None and live_status in _ACTIVE_STATUSES:
                live["status"] = "intent"
                live["delivery_status"] = "intent"
                repaired = dict(live)
            else:
                repaired = None
        if repaired is not None:
            _remove_persisted_record(
                delegation_id, tracker_path=origin_tracker_path or None
            )
            _persist_record(repaired, delivery_status="intent")
        return {
            "status": "rejected",
            "error": "async scheduled phase could not be persisted",
        }

    start_gate.set()
    if not running_checkpoint_ready.wait(
        timeout=_RUNNING_CHECKPOINT_TIMEOUT_SECONDS
    ):
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None:
                live["acceptance_aborted"] = True
                live["last_error"] = (
                    "async running checkpoint timed out before execution"
                )
        abort_gate.set()
        execute_gate.set()
        return {
            "status": "rejected",
            "error": "async running checkpoint timed out before execution",
        }
    if not running_checkpoint["ok"]:
        abort_gate.set()
        execute_gate.set()
        return {
            "status": "rejected",
            "error": running_checkpoint["error"],
        }

    heartbeat_stop = _start_heartbeat_thread(delegation_id)
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None and live.get("status") == "running":
            live["heartbeat_stop"] = heartbeat_stop
        else:
            heartbeat_stop.set()
    execute_gate.set()
    if progress_fn is not None:
        _ensure_stale_monitor()

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
        if record.get("status") not in _ACTIVE_STATUSES:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["last_heartbeat_at"] = record["completed_at"]
        record["delivery_status"] = "finalizing"
        hb_stop = record.get("heartbeat_stop")
        if hasattr(hb_stop, "set"):
            hb_stop.set()
        record["interrupt_fn"] = None
        terminal_callback = record.get("_terminal_callback")
        record["_terminal_callback"] = None
        event_record = dict(record)
        _prune_completed_locked()
        _cleanup_locked(now=record["completed_at"])

    _invoke_terminal_callback(terminal_callback, combined, status)
    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
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
    # Routing origin captured at dispatch (see _capture_routing_origin).
    for _k in ("scope_id", "user_id", "user_name"):
        if event_record.get(_k):
            evt[_k] = event_record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for key in (
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if key in combined:
            evt[key] = combined[key]
    terminalized = _mark_bestplan_completed_unverified(event_record, evt)
    if (
        plan_id
        and event_record.get("bestplan_local_execution") is True
        and not terminalized
    ):
        event_record[_BESTPLAN_TERMINALIZATION_PENDING] = True
        _persist_and_queue_terminal(
            event_record,
            combined,
            evt,
            publish=False,
        )
        return
    _persist_and_queue_terminal(event_record, combined, evt)


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    _recover_once()
    _maybe_cleanup()
    now = time.time()
    with _records_lock:
        records = list(_records.values())
    return [_serialise_record(r, now) for r in records]


def _signal_interrupt_with_durable_review_cancel(
    target: Dict[str, Any],
    *,
    signal_children: Callable[[], object],
) -> bool:
    """Persist a stop and report a durable pre-review no-child proof."""

    if (
        target.get("bestplan_local_execution") is not True
        or not target.get("bestplan_plan_id")
        or not target.get("bestplan_review_job_id")
        or not target.get("bestplan_state_db_path")
    ):
        signal_children()
        return False

    try:
        from agent.review_engine import (
            ReviewLeaseConflict,
            ReviewStore,
            ReviewValidationError,
        )

        state_db_path = _canonical_bestplan_state_db_path(
            target.get("bestplan_state_db_path")
        )
        store = ReviewStore(state_db_path)
        job_id = str(target["bestplan_review_job_id"])
        try:
            job = store.get_job(job_id)
        except ReviewValidationError:
            cancelled_pipeline = store.request_execution_pipeline_cancel(
                plan_id=str(target.get("bestplan_plan_id") or ""),
                delegation_id=str(target.get("delegation_id") or ""),
                job_id=job_id,
            )
            if cancelled_pipeline:
                pipeline = store.get_execution_pipeline(
                    str(target.get("bestplan_plan_id") or "")
                )
                no_active_attempt = bool(
                    pipeline.active_attempt_ordinal is None
                    and pipeline.attempt_owner_pid is None
                    and pipeline.attempt_owner_process_start_id is None
                )
                signal_children()
                return no_active_attempt
            # Review creation won the same database fence. Continue through
            # the normal durable review cancellation path.
            job = store.get_job(job_id)
    except ReviewValidationError:
        raise ReviewLeaseConflict(
            "BestPlan cancellation has no durable execution target"
        ) from None

    if (
        job.source_kind != "bestplan_integration"
        or job.source_id != str(target.get("bestplan_plan_id") or "")
    ):
        raise ReviewLeaseConflict("review job identity does not match BestPlan")
    if job.state in {"landing_claimed", "landed"}:
        raise ReviewLeaseConflict("landing_already_claimed")
    if job.cancel_requested:
        signal_children()
        return False
    if job.owner_id is None or job.lease_expires_at_ns is None:
        interrupt_owner = "review-interrupt-" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{target.get('delegation_id')}:{job_id}",
        ).hex
        job = store.claim_job(
            job_id=job_id,
            owner_id=interrupt_owner,
            now_ns=time.time_ns(),
            lease_duration_ns=30_000_000_000,
            expected_fencing_token=job.fencing_token,
        )
    operation_id = "async-cancel-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{target.get('delegation_id')}:{job_id}",
    ).hex
    store.request_cancel(
        job_id=job_id,
        owner_id=str(job.owner_id),
        fencing_token=job.fencing_token,
        operation_id=operation_id,
        signal_children=signal_children,
    )
    return False


def _record_is_interruptible(record: Mapping[str, Any]) -> bool:
    """Select exact durable phases that an operator can safely stop."""

    phase = str(record.get("status") or "")
    if phase in {
        "scheduled", "running", "stalling",
        "review_requeued", "review_waiting",
    }:
        return True
    return bool(
        phase in {"intent", "interrupting"}
        and record.get("bestplan_local_execution") is True
        and record.get("bestplan_plan_id")
        and record.get("bestplan_review_job_id")
        and record.get("bestplan_state_db_path")
    )


def _interrupt_records(
    targets: List[Dict[str, Any]], *, reason: str, source: str
) -> int:
    """Cancel not-yet-started work or request a truthful running interrupt."""
    interruptible = {
        "intent",
        "scheduled",
        "running",
        "stalling",
        "interrupting",
        "review_requeued",
        "review_waiting",
    }
    count = 0
    for target in targets:
        delegation_id = str(target.get("delegation_id") or "")
        with _records_lock:
            live = _records.get(delegation_id)
            if live is None or live.get("status") not in interruptible:
                continue
            phase = str(live.get("status"))
            if (
                phase in {"intent", "scheduled"}
                and live.get("owner_liveness") != "unknown"
            ):
                # Claim cancellation atomically before the gated worker can
                # cross scheduled -> running. No callback acknowledgement is
                # needed because user code has not begun.
                live["status"] = "interrupting"
                live["delivery_status"] = "interrupting"
                live["interrupt_requested_at"] = time.time()
                live["interrupt_reason"] = reason
                scheduled_snapshot = dict(live)
            else:
                scheduled_snapshot = None

        if scheduled_snapshot is not None:
            if scheduled_snapshot.get("bestplan_local_execution") is True:
                try:
                    # The scheduled gate proves that no user child began. Keep
                    # the tracker nonterminal until the exact durable pipeline
                    # records both the stop request and child extinction.
                    _signal_interrupt_with_durable_review_cancel(
                        scheduled_snapshot,
                        signal_children=lambda: None,
                    )
                    if not _finalize_durable_review_cancel(scheduled_snapshot):
                        raise RuntimeError(
                            "scheduled BestPlan cancellation was not finalized"
                        )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    with _records_lock:
                        live = _records.get(delegation_id)
                        if live is not None and live.get("status") == "interrupting":
                            live["interrupt_error"] = error
                            failed_snapshot = dict(live)
                        else:
                            failed_snapshot = None
                    if failed_snapshot is not None:
                        _persist_record(
                            failed_snapshot,
                            delivery_status="interrupting",
                        )
                    logger.debug(
                        "%s: %s scheduled interrupt failed: %s",
                        source,
                        delegation_id,
                        exc,
                    )
                    continue
            count += 1
            if scheduled_snapshot.get("is_batch"):
                _finalize_batch(
                    delegation_id,
                    {"results": [], "error": "interrupted before execution"},
                    "interrupted",
                )
            else:
                _finalize(
                    delegation_id,
                    {
                        "status": "interrupted",
                        "summary": "Async delegation was interrupted before execution.",
                        "error": "interrupted before execution",
                        "exit_reason": "interrupted",
                    },
                    "interrupted",
                )
            continue

        fn = target.get("interrupt_fn")
        finalize_wait_without_worker = (
            phase == "review_waiting"
            and not callable(fn)
            and target.get("owner_liveness") != "unknown"
            and target.get("bestplan_local_execution") is True
            and bool(target.get("bestplan_review_job_id"))
        )
        if (
            not callable(fn)
            and target.get("bestplan_local_execution") is True
            and target.get("bestplan_review_job_id")
        ):
            fn = lambda: None
        if not callable(fn):
            error = "interrupt callback unavailable"
            with _records_lock:
                live = _records.get(delegation_id)
                if live is not None and live.get("status") in interruptible:
                    live["interrupt_error"] = error
                    live["interrupt_requested_at"] = time.time()
                    failed_snapshot = dict(live)
                else:
                    failed_snapshot = None
            if failed_snapshot is not None:
                _persist_record(
                    failed_snapshot,
                    delivery_status=str(failed_snapshot.get("delivery_status") or "running"),
                )
            continue
        try:
            durable_no_active_attempt = _signal_interrupt_with_durable_review_cancel(
                target,
                signal_children=fn,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with _records_lock:
                live = _records.get(delegation_id)
                if live is not None and live.get("status") in interruptible:
                    live["interrupt_error"] = error
                    live["interrupt_requested_at"] = time.time()
                    failed_snapshot = dict(live)
                else:
                    failed_snapshot = None
            if failed_snapshot is not None:
                _persist_record(
                    failed_snapshot,
                    delivery_status=str(failed_snapshot.get("delivery_status") or "running"),
                )
            logger.debug("%s: %s interrupt failed: %s", source, delegation_id, exc)
            continue

        if durable_no_active_attempt:
            with _records_lock:
                live = _records.get(delegation_id)
                if live is None or live.get("status") not in interruptible:
                    continue
                live["status"] = "interrupting"
                live["delivery_status"] = "interrupting"
                live["interrupt_requested_at"] = time.time()
                live["interrupt_reason"] = reason
                live.pop("interrupt_error", None)
                live["interrupt_fn"] = None
                interrupting_snapshot = dict(live)
            _persist_record(
                interrupting_snapshot, delivery_status="interrupting"
            )
            cancel_request = _bestplan_review_resume_request(
                interrupting_snapshot,
                tracker_path=interrupting_snapshot.get(
                    "origin_tracker_path"
                ) or None,
                cancel_finalize_only=True,
            )
            if (
                cancel_request is not None
                and _finalize_durable_review_cancel(interrupting_snapshot)
                and _terminalize_durable_review_cancel_tracker(cancel_request)
            ):
                count += 1
                continue
            if cancel_request is not None:
                _retry_durable_review_cancel_finalization(cancel_request)
            count += 1
            continue

        if finalize_wait_without_worker:
            if not _finalize_durable_review_cancel(target):
                error = "durable review cancellation was not finalized"
                with _records_lock:
                    live = _records.get(delegation_id)
                    if live is not None and live.get("status") == "review_waiting":
                        live["interrupt_error"] = error
                        live["interrupt_requested_at"] = time.time()
                        waiting_snapshot = dict(live)
                    else:
                        waiting_snapshot = None
                if waiting_snapshot is not None:
                    _persist_record(
                        waiting_snapshot, delivery_status="review_waiting",
                    )
                    resume_request = _bestplan_review_resume_request(
                        waiting_snapshot,
                        tracker_path=waiting_snapshot.get(
                            "origin_tracker_path"
                        ) or None,
                    )
                    if resume_request is not None:
                        _schedule_bestplan_review_recovery_retry(
                            resume_request,
                            reason_code="cancel_finalize_failed",
                        )
                count += 1
                continue
            with _records_lock:
                live = _records.get(delegation_id)
                if live is None or live.get("status") != "review_waiting":
                    continue
                live["status"] = "interrupting"
                live["delivery_status"] = "interrupting"
                live["interrupt_requested_at"] = time.time()
                live["interrupt_reason"] = reason
                live.pop("interrupt_error", None)
                live["interrupt_fn"] = None
            count += 1
            if target.get("is_batch"):
                _finalize_batch(
                    delegation_id,
                    {
                        "results": [],
                        "error": "interrupted after child extinction",
                    },
                    "interrupted",
                )
            else:
                _finalize(
                    delegation_id,
                    {
                        "status": "interrupted",
                        "summary": "Async delegation review was interrupted.",
                        "error": "interrupted after child extinction",
                        "exit_reason": "interrupted",
                    },
                    "interrupted",
                )
            continue

        with _records_lock:
            live = _records.get(delegation_id)
            if live is None or live.get("status") not in interruptible:
                continue
            live["status"] = "interrupting"
            live["delivery_status"] = "interrupting"
            live["interrupt_requested_at"] = time.time()
            live["interrupt_reason"] = reason
            live.pop("interrupt_error", None)
            hb_stop = live.get("heartbeat_stop")
            if hasattr(hb_stop, "set"):
                hb_stop.set()
            live["interrupt_fn"] = None
            interrupting_snapshot = dict(live)
        if not _persist_record(
            interrupting_snapshot, delivery_status="interrupting"
        ):
            with _records_lock:
                live = _records.get(delegation_id)
                if live is not None and live.get("status") == "interrupting":
                    live["delivery_error"] = "interrupting state persistence failed"
        count += 1
    return count


def interrupt_all(reason: str = "shutdown") -> int:
    """Cancel scheduled work and request interruption of running delegations."""
    with _records_lock:
        targets = [
            r for r in _records.values()
            if _record_is_interruptible(r)
        ]
    count = _interrupt_records(targets, reason=reason, source="interrupt_all")
    if count:
        logger.info("Requested interruption of %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Cancel/request interruption for delegations owned by one session."""
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if _record_is_interruptible(r)
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
        ]
    count = _interrupt_records(
        targets, reason=reason, source="interrupt_for_session"
    )
    if count:
        logger.info(
            "Requested interruption of %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers, _last_cleanup_at, _recovery_attempted, _monitor_thread
    _monitor_stop.set()
    with _monitor_lock:
        _monitor_thread = None
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
