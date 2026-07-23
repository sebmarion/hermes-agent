"""Deterministic gateway admission and quiescence receipt."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


DRAIN_READINESS_SCHEMA = "hermes.gateway_drain.v1"
_WORK_FIELDS = (
    "active_http_requests",
    "active_agent_turns",
    "active_delegations",
    "background_processes",
    "process_completion_queue_depth",
    "active_cron_jobs",
    "api_background_tasks",
    "running_kanban_workers",
    "gateway_background_tasks",
)


def _work_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _live_kanban_worker_pids() -> set[int]:
    """Return live worker PIDs from process argv, or raise if unverifiable."""

    marker = "work kanban task "
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_Process | ForEach-Object "
                "{ \"$($_.ProcessId)`t$($_.CommandLine)\" }"
            ),
        ]
    else:
        command = ["ps", "-Ao", "pid=,command="]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise RuntimeError("Kanban worker process proof unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("Kanban worker process proof unavailable")

    pids: set[int] = set()
    for line in (result.stdout or "").splitlines():
        if marker not in line:
            continue
        pid_text = line.strip().split(None, 1)[0] if line.strip() else ""
        try:
            pid = int(pid_text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Malformed Kanban worker process proof") from exc
        if pid > 0:
            pids.add(pid)
    return pids


def _kanban_database_paths(kanban_db: Any) -> list[Path]:
    """Enumerate active and recoverably archived board databases."""

    candidates: set[Path] = set()
    for board in kanban_db.list_boards(include_archived=True):
        raw_path = board.get("db_path")
        if not raw_path:
            raw_path = kanban_db.kanban_db_path(board.get("slug"))
        candidates.add(Path(raw_path).expanduser().resolve())

    archive_root = kanban_db.boards_root() / "_archived"
    if archive_root.exists():
        for archived_board in archive_root.iterdir():
            if not archived_board.is_dir():
                continue
            archived_db = archived_board / "kanban.db"
            if archived_db.is_file():
                candidates.add(archived_db.expanduser().resolve())
    return sorted(candidates, key=str)


def count_running_kanban_workers() -> int:
    """Count detached/claimed Kanban ownership without a writable DB.

    ``kanban_db.connect()`` performs schema setup and migrations, so health
    checks must not use it. Existing board databases are opened in SQLite URI
    read-only mode; aliases resolving to the same database are counted once.
    A missing database contributes zero, while an unreadable/corrupt existing
    database or incomplete ownership schema raises so readiness fails closed.

    Transitional/inconsistent rows are deliberately conservative: any task or
    run marker that can represent a claimed worker blocks cutover. Task rows
    pointing at a run are deduplicated with that run; orphan task/run claims
    remain independently visible.
    """

    from hermes_cli import kanban_db

    seen_paths: set[str] = set()
    ownership_units: set[tuple[str, str, str]] = set()
    for path in _kanban_database_paths(kanban_db):
        path_key = str(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        if not path.is_file():
            continue
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            task_rows = conn.execute(
                """
                SELECT id, current_run_id, worker_pid
                  FROM tasks
                 WHERE status = 'running'
                    OR worker_pid IS NOT NULL
                    OR claim_lock IS NOT NULL
                    OR current_run_id IS NOT NULL
                """
            ).fetchall()
            run_rows = conn.execute(
                """
                SELECT id, task_id, worker_pid
                  FROM task_runs
                 WHERE status = 'running'
                    OR ended_at IS NULL
                    OR worker_pid IS NOT NULL
                    OR claim_lock IS NOT NULL
                """
            ).fetchall()
            active_runs = {str(row[0]): row for row in run_rows}
            linked_task_pid = {
                str(current_run_id): int(worker_pid)
                for _task_id, current_run_id, worker_pid in task_rows
                if current_run_id is not None and worker_pid is not None
            }
            for run_id, _task_id, run_worker_pid in run_rows:
                worker_pid = run_worker_pid
                if worker_pid is None:
                    worker_pid = linked_task_pid.get(str(run_id))
                if worker_pid is not None:
                    ownership_units.add(
                        ("process", "pid", str(int(worker_pid)))
                    )
                else:
                    ownership_units.add((path_key, "run", str(run_id)))
            for task_id, current_run_id, worker_pid in task_rows:
                if (
                    current_run_id is not None
                    and str(current_run_id) in active_runs
                ):
                    continue
                if worker_pid is not None:
                    ownership_units.add(
                        ("process", "pid", str(int(worker_pid)))
                    )
                elif current_run_id is not None:
                    ownership_units.add(
                        (path_key, "run", str(current_run_id))
                    )
                else:
                    ownership_units.add((path_key, "task", str(task_id)))
        finally:
            conn.close()
    for pid in _live_kanban_worker_pids():
        ownership_units.add(("process", "pid", str(pid)))
    return len(ownership_units)


def build_drain_readiness(
    *,
    live_admission_rejecting: bool | None,
    drain_requested: bool,
    active_http_requests: int | None,
    active_agent_turns: int | None,
    active_delegations: int | None,
    background_processes: int | None,
    process_completion_queue_depth: int | None,
    active_cron_jobs: int | None,
    api_background_tasks: int | None,
    running_kanban_workers: int | None,
    gateway_background_tasks: int | None,
    pair_open_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed proof of admission state and active work.

    A cutover may proceed only when both ``quiescence.verified`` and
    ``quiescence.quiescent`` are true. Unknown work sources never collapse to
    zero, and a just-written drain marker remains transitional until the
    gateway runtime acknowledges the rejecting state.
    """

    pair_gate_active = bool(pair_open_gate and pair_open_gate.get("active"))
    effective_rejection_requested = bool(drain_requested or pair_gate_active)
    if live_admission_rejecting is True and effective_rejection_requested:
        admission_state = "rejecting_new_work"
        admission_verified = True
    elif live_admission_rejecting is True:
        admission_state = "transitioning_to_accept"
        admission_verified = False
    elif live_admission_rejecting is False and effective_rejection_requested:
        admission_state = "transitioning_to_reject"
        admission_verified = False
    elif live_admission_rejecting is False:
        admission_state = "accepting_new_work"
        admission_verified = True
    else:
        admission_state = "unknown"
        admission_verified = False

    raw_work = {
        "active_http_requests": active_http_requests,
        "active_agent_turns": active_agent_turns,
        "active_delegations": active_delegations,
        "background_processes": background_processes,
        "process_completion_queue_depth": process_completion_queue_depth,
        "active_cron_jobs": active_cron_jobs,
        "api_background_tasks": api_background_tasks,
        "running_kanban_workers": running_kanban_workers,
        "gateway_background_tasks": gateway_background_tasks,
    }
    work = {name: _work_count(raw_work[name]) for name in _WORK_FIELDS}
    work_status = {
        name: "verified" if work[name] is not None else "unverified"
        for name in _WORK_FIELDS
    }

    blockers: list[str] = []
    pair_gate_verified = pair_open_gate is None or bool(
        pair_open_gate.get("verified")
    )
    if not admission_verified:
        blockers.append("admission_unverified")
    elif admission_state != "rejecting_new_work":
        blockers.append("admission_not_rejecting")
    if not pair_gate_verified:
        blockers.append("pair_open_gate_unverified")
    for name in _WORK_FIELDS:
        count = work[name]
        if count is None:
            blockers.append(f"{name}_unverified")
        elif count > 0:
            blockers.append(name)

    sources_verified = admission_verified and pair_gate_verified and all(
        status == "verified" for status in work_status.values()
    )
    quiescent = sources_verified and not blockers

    admission = {
        "state": admission_state,
        "verified": admission_verified,
        "drain_requested": bool(drain_requested),
    }
    result = {
        "schema": DRAIN_READINESS_SCHEMA,
        "admission": admission,
        "work": work,
        "work_status": work_status,
        "quiescence": {
            "verified": sources_verified,
            "quiescent": quiescent,
            "blockers": blockers,
        },
    }
    if pair_open_gate is not None:
        admission["pair_open_gate_active"] = pair_gate_active
        admission["effective_rejection_requested"] = effective_rejection_requested
        result["pair_open_gate"] = dict(pair_open_gate)
    return result
