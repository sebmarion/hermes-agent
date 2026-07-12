"""Validated /bestplan envelopes and fail-closed bare-``go`` host ingress."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent.execution_plan import ExecutionPlan, PlanValidationError, compile_execution_plan

logger = logging.getLogger(__name__)

BESTPLAN_ENVELOPE_START = "<<<HERMES_BESTPLAN_V1>>>"
BESTPLAN_ENVELOPE_END = "<<<END_HERMES_BESTPLAN_V1>>>"
_ENVELOPE_RE = re.compile(
    re.escape(BESTPLAN_ENVELOPE_START)
    + r"\s*(?P<payload>\{.*?\})\s*"
    + re.escape(BESTPLAN_ENVELOPE_END),
    re.DOTALL,
)


class PlanState:
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED_UNVERIFIED = "completed_unverified"
    COMPLETED_VERIFIED = "completed_verified"
    REJECTED = "rejected"
    FAILED = "failed"


_OPEN_STATES = (
    PlanState.PENDING,
    PlanState.APPROVED,
    PlanState.RUNNING,
    PlanState.WAITING,
)
_MAX_V1_SLICES = 2
_LANE_FOR_SLICE = {
    ("implement", "fast_fallback"): "code_worker",
    ("review", "frontier_review"): "smart_reviewer",
}


class BestplanError(ValueError):
    """Raised when an envelope, state transition, or route is unsafe."""


class BaselineFingerprintError(BestplanError):
    """Raised when the host cannot compute a strong workspace baseline."""


@dataclass(frozen=True)
class PlanCapture:
    executable: bool
    response: str
    plan_id: Optional[str] = None
    digest: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ResolvedGo:
    # ``resolved`` means the host consumed this turn.  It is intentionally true
    # for fail-closed outcomes as well as successful dispatch.
    resolved: bool
    status: str
    plan_id: Optional[str] = None
    delegation_id: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def response(self) -> str:
        if self.status == "waiting":
            return (
                f"Plan {self.plan_id} was dispatched as delegation "
                f"{self.delegation_id or '(pending id)'}. Status: waiting for "
                "independent completion evidence."
            )
        if self.status in {"possibly_dispatched", "dispatch_in_flight"}:
            return (
                f"Plan {self.plan_id} may already be active as delegation "
                f"{self.delegation_id or '(identity persisted)'}. The dispatch outcome "
                "is unknown/possibly dispatched; retry is idempotent and will reconcile it."
            )
        return f"Plan execution was not started ({self.status}): {self.reason or self.error or 'fail-closed'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "status": self.status,
            "plan_id": self.plan_id,
            "delegation_id": self.delegation_id,
            "reason": self.reason,
            "error": self.error,
            "response": self.response,
        }

    def to_agent_result(
        self,
        *,
        conversation_history: list[dict[str, Any]],
        user_message: str,
        host_agent: Any = None,
    ) -> dict[str, Any]:
        """Build a terminal host result without entering the model loop."""
        messages = [dict(item) for item in conversation_history]
        messages.append({"role": "user", "content": user_message})
        messages.append({"role": "assistant", "content": self.response})
        if host_agent is not None:
            from agent.agent_runtime_helpers import repair_message_sequence

            repair_message_sequence(host_agent, messages)
            persist = getattr(host_agent, "_persist_session", None)
            if callable(persist):
                persist(messages, conversation_history)
        return {
            "final_response": self.response,
            "messages": messages,
            "api_calls": 0,
            "completed": True,
            "host_ingress": self.to_dict(),
        }


def _active_profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "").strip()


def _canonical_workspace(workspace: str) -> str:
    return str(Path(workspace or os.getcwd()).expanduser().resolve())


def compute_baseline_fingerprint(workspace: str) -> str:
    workspace = _canonical_workspace(workspace)
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=workspace,
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        tracked_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=root, capture_output=True, timeout=10, check=True,
        ).stdout
        untracked_raw = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=10, check=True,
        ).stdout
        # Git intentionally omits sockets/FIFOs/devices from its untracked
        # listing.  Walk only to reject those unsafe baseline inputs (regular
        # ignored files remain outside the Git baseline by design).
        scanned = 0
        deadline = time.monotonic() + 10
        for current, dirnames, filenames in os.walk(
            workspace,
            topdown=True,
            followlinks=False,
            onerror=lambda exc: (_ for _ in ()).throw(exc),
        ):
            if time.monotonic() > deadline:
                raise BaselineFingerprintError("workspace special-file scan timed out")
            if Path(current).resolve() == Path(root).resolve():
                dirnames[:] = [name for name in dirnames if name != ".git"]
            for name in [*dirnames, *filenames]:
                scanned += 1
                if scanned > 100_000:
                    raise BaselineFingerprintError("workspace special-file scan exceeded 100000 entries")
                candidate = Path(current) / name
                mode = candidate.lstat().st_mode
                if not (
                    stat.S_ISREG(mode)
                    or stat.S_ISDIR(mode)
                    or stat.S_ISLNK(mode)
                ):
                    raise BaselineFingerprintError(
                        f"workspace contains special file: {candidate.relative_to(workspace)}"
                    )
        digest = hashlib.sha256()
        digest.update(f"git:{root}\n{head}\n".encode())
        digest.update(tracked_diff)
        for relative_raw in sorted(part for part in untracked_raw.split(b"\0") if part):
            relative = os.fsdecode(relative_raw)
            path = Path(root) / relative
            digest.update(b"\0untracked\0")
            digest.update(relative_raw)
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(path)))
            else:
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise BaselineFingerprintError(
                        f"untracked special file cannot be fingerprinted: {relative}"
                    )
                digest.update(path.read_bytes())
        return digest.hexdigest()
    except BaselineFingerprintError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise BaselineFingerprintError(
            f"strong git baseline unavailable for {workspace}: {type(exc).__name__}: {exc}"
        ) from exc


def _manifest_digest(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_go_trigger(text: str) -> bool:
    return str(text or "").strip().casefold() == "go"


def _extract_envelope(response: str) -> tuple[str, ExecutionPlan, dict[str, Any]]:
    matches = list(_ENVELOPE_RE.finditer(str(response or "")))
    if len(matches) != 1:
        raise BestplanError("response must contain exactly one explicit bestplan envelope")
    raw_envelope = matches[0].group(0).strip()
    try:
        envelope = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise BestplanError(f"bestplan envelope is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"version", "manifest"}:
        raise BestplanError("bestplan envelope must contain only version and manifest")
    if envelope.get("version") != 1 or isinstance(envelope.get("version"), bool):
        raise BestplanError("bestplan envelope version must be integer 1")
    try:
        plan = compile_execution_plan(envelope.get("manifest"))
    except PlanValidationError as exc:
        raise BestplanError(str(exc)) from exc
    return raw_envelope, plan, plan.to_manifest()


def _validated_write_lease(workspace: str, lease: str) -> str:
    raw = str(lease or "").strip()
    if not raw:
        raise BestplanError("V1 implementation requires a nonempty write lease")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise BestplanError(f"write lease must be relative to workspace: {raw}")
    if any(part == ".." for part in candidate.parts):
        raise BestplanError(f"write lease traversal is forbidden: {raw}")
    root = Path(workspace).resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise BestplanError(f"write lease escapes workspace: {raw}")
    return candidate.as_posix()


def _v1_plan_constraints(plan: ExecutionPlan, *, workspace: Optional[str] = None) -> None:
    if len(plan.slices) > _MAX_V1_SLICES:
        raise BestplanError(f"V1 supports at most {_MAX_V1_SLICES} slices; got {len(plan.slices)}")
    if len(plan.dependency_waves) != 1:
        raise BestplanError("V1 supports only one independent wave; dependencies are not allowed")
    canonical_workspace = _canonical_workspace(workspace) if workspace is not None else None
    kinds = {item.kind for item in plan.slices}
    if kinds == {"review"}:
        if plan.mode != "sota":
            raise BestplanError("V1 review-only manifest requires mode=sota")
        if plan.risk != "high":
            raise BestplanError("V1 review-only manifest requires risk=high")
    elif kinds == {"implement"}:
        if plan.mode != "delegate":
            raise BestplanError("V1 implementation manifest requires mode=delegate")
    else:
        raise BestplanError("V1 cannot mix implementation and review slices")
    for item in plan.slices:
        if item.depends_on:
            raise BestplanError(f"V1 slice {item.id} has dependencies")
        if (item.kind, item.capability) not in _LANE_FOR_SLICE:
            raise BestplanError(
                f"V1 cannot route slice {item.id} (kind={item.kind}, capability={item.capability})"
            )
        if canonical_workspace is not None and _canonical_workspace(item.workspace or "") != canonical_workspace:
            raise BestplanError(
                f"V1 slice {item.id} workspace must equal captured workspace {canonical_workspace}"
            )
        if item.kind == "implement":
            if item.read_only:
                raise BestplanError(f"V1 implementation slice {item.id} requires read_only=false")
            if not item.allowed_paths:
                raise BestplanError(f"V1 implementation slice {item.id} requires a nonempty write lease")
            for lease in item.allowed_paths:
                _validated_write_lease(canonical_workspace or _canonical_workspace(item.workspace), lease)
        else:
            if item.capability != "frontier_review" or not item.read_only:
                raise BestplanError(
                    f"V1 review slice {item.id} requires frontier_review/read_only=true"
                )


def _plan_to_delegate_tasks(
    plan: ExecutionPlan, *, workspace: Optional[str] = None,
) -> list[dict[str, Any]]:
    _v1_plan_constraints(plan, workspace=workspace)
    tasks = []
    for item in plan.slices:
        tasks.append({
            "goal": item.goal,
            "context": "\n".join([
                f"Slice {item.id}: {item.goal}",
                f"Workspace: {item.workspace or '.'}",
                f"Allowed paths: {', '.join(item.allowed_paths)}",
                f"Expected artifacts: {', '.join(item.expected_artifacts)}",
                f"Acceptance: {'; '.join(item.acceptance)}",
            ]),
            "route": _LANE_FOR_SLICE[(item.kind, item.capability)],
            "role": "leaf",
            "_bestplan_read_only": item.read_only,
            "_bestplan_leases": list(item.allowed_paths),
        })
    return tasks


def _render_authoritative_manifest(
    plan: ExecutionPlan, *, workspace: str, digest: str,
) -> str:
    lines = [
        "Authoritative executable manifest (host-rendered):",
        f"- digest={digest}",
        f"- mode: {plan.mode}",
        f"- risk: {plan.risk}",
        f"- workspace: {_canonical_workspace(workspace)}",
    ]
    for item in plan.slices:
        route = _LANE_FOR_SLICE[(item.kind, item.capability)]
        leases = ", ".join(item.allowed_paths) if item.allowed_paths else "none (read-only)"
        acceptance = "; ".join(item.acceptance)
        lines.extend([
            f"- slice {item.id}:",
            f"  - route: {route}",
            f"  - goal: {item.goal}",
            f"  - kind/capability: {item.kind}/{item.capability}",
            f"  - read_only: {str(item.read_only).lower()}",
            f"  - write leases: {leases}",
            f"  - acceptance: {acceptance}",
        ])
    return "\n".join(lines)


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bestplan_plans (
    plan_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    session_id TEXT,
    profile TEXT NOT NULL,
    workspace TEXT NOT NULL,
    baseline_revision TEXT,
    baseline_fingerprint TEXT NOT NULL,
    raw_request TEXT,
    raw_plan_json TEXT NOT NULL,
    validated_manifest_json TEXT NOT NULL,
    state TEXT NOT NULL,
    approved_at REAL,
    approved_by TEXT,
    approval_digest TEXT,
    started_at REAL,
    completed_at REAL,
    delegation_ids_json TEXT,
    evidence_json TEXT,
    error TEXT,
    dispatch_id TEXT,
    dispatch_state TEXT,
    resolved_runtime_json TEXT,
    dispatch_owner TEXT,
    dispatch_started_at REAL,
    dispatch_updated_at REAL,
    sandbox_workspace TEXT
);
CREATE INDEX IF NOT EXISTS idx_bestplan_plans_session_state
    ON bestplan_plans(session_id, state);
"""


class BestplanStore:
    """SQLite authority for immutable plan envelopes and atomic claims."""

    def __init__(self, session_db=None, db_path: Optional[Path] = None):
        self._session_db = session_db
        self._db_path = Path(db_path) if db_path is not None else None
        self._lock = threading.RLock()
        self._owned_connection: sqlite3.Connection | None = None
        if self._session_db is None and self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._owned_connection = sqlite3.connect(
                str(self._db_path), check_same_thread=False, timeout=30,
            )
            self._owned_connection.row_factory = sqlite3.Row
            self._owned_connection.execute("PRAGMA journal_mode=WAL")
            self._owned_connection.execute("PRAGMA synchronous=FULL")
            self._owned_connection.executescript(_TABLE_SQL)
            self._owned_connection.commit()
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            if self._owned_connection is not None:
                self._owned_connection.close()
                self._owned_connection = None

    def _connection(self) -> sqlite3.Connection:
        if self._session_db is not None:
            return self._session_db._conn
        if self._owned_connection is not None:
            return self._owned_connection
        from hermes_state import SessionDB
        self._session_db = SessionDB()
        return self._session_db._conn

    def _execute_write(self, fn: Callable[[sqlite3.Connection], Any]):
        if self._session_db is not None:
            return self._session_db._execute_write(fn)
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _ensure_schema(self) -> None:
        columns = {
            "dispatch_id": "TEXT",
            "dispatch_state": "TEXT",
            "resolved_runtime_json": "TEXT",
            "dispatch_owner": "TEXT",
            "dispatch_started_at": "REAL",
            "dispatch_updated_at": "REAL",
            "sandbox_workspace": "TEXT",
        }

        def migrate(conn):
            conn.executescript(_TABLE_SQL)
            existing = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(bestplan_plans)")
            }
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE bestplan_plans ADD COLUMN {name} {sql_type}")

        self._execute_write(migrate)

    def _read_lock(self):
        return getattr(self._session_db, "_lock", self._lock)

    def create_plan(
        self,
        raw_request: str,
        plan: ExecutionPlan,
        *,
        session_id: str,
        workspace: str,
        profile: Optional[str] = None,
        baseline_fingerprint: Optional[str] = None,
        raw_envelope: Optional[str] = None,
    ) -> str:
        workspace = _canonical_workspace(workspace)
        manifest = plan.to_manifest()
        manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        if raw_envelope is None:
            raw = (
                f"{BESTPLAN_ENVELOPE_START}\n"
                + json.dumps(
                    {"version": 1, "manifest": manifest},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + f"\n{BESTPLAN_ENVELOPE_END}"
            )
        else:
            raw = str(raw_envelope)
        plan_id = f"bp_{uuid.uuid4().hex}"
        digest = _manifest_digest(manifest)

        def insert(conn):
            conn.execute(
                """INSERT INTO bestplan_plans (
                    plan_id, version, created_at, session_id, profile, workspace,
                    baseline_fingerprint, raw_request, raw_plan_json,
                    validated_manifest_json, state, approval_digest
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, time.time(), str(session_id),
                    str(_active_profile() if profile is None else profile), workspace,
                    baseline_fingerprint or compute_baseline_fingerprint(workspace),
                    str(raw_request or ""), raw, manifest_json, PlanState.PENDING, digest,
                ),
            )

        self._execute_write(insert)
        return plan_id

    def get_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._read_lock():
            row = self._connection().execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_for_session(self, session_id: str, *, open_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bestplan_plans WHERE session_id = ?"
        params: list[Any] = [str(session_id)]
        if open_only:
            sql += " AND state IN (?, ?, ?, ?)"
            params.extend(_OPEN_STATES)
        sql += " ORDER BY created_at DESC"
        with self._read_lock():
            rows = self._connection().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def approve_plan(self, plan_id: str, approver: str = "user") -> bool:
        def approve(conn):
            row = conn.execute(
                "SELECT validated_manifest_json, approval_digest FROM bestplan_plans "
                "WHERE plan_id = ? AND state = ?",
                (plan_id, PlanState.PENDING),
            ).fetchone()
            if row is None:
                return 0
            manifest = json.loads(row["validated_manifest_json"])
            digest = _manifest_digest(manifest)
            if row["approval_digest"] and row["approval_digest"] != digest:
                return 0
            return conn.execute(
                "UPDATE bestplan_plans SET state=?, approved_at=?, approved_by=?, "
                "approval_digest=? WHERE plan_id=? AND state=?",
                (PlanState.APPROVED, time.time(), approver, digest, plan_id, PlanState.PENDING),
            ).rowcount
        return bool(self._execute_write(approve))

    def reject_plan(self, plan_id: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=? WHERE plan_id=? AND state=?",
            (PlanState.REJECTED, plan_id, PlanState.PENDING),
        ).rowcount))

    def list_approved_matching(
        self, *, session_id: str, workspace: str,
        baseline_fingerprint: Optional[str] = None, profile: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        expected_profile = _active_profile() if profile is None else str(profile)
        expected_workspace = _canonical_workspace(workspace)
        expected_baseline = baseline_fingerprint or compute_baseline_fingerprint(workspace)
        return [
            row for row in self.list_for_session(session_id)
            if row["state"] == PlanState.APPROVED
            and row["profile"] == expected_profile
            and row["workspace"] == expected_workspace
            and row["baseline_fingerprint"] == expected_baseline
        ]

    def atomic_claim_approved(
        self,
        plan_id: str,
        baseline_fingerprint: str,
        *,
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        workspace: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Approve (when pending), revalidate, and claim in one transaction."""
        def claim(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone()
            if row is None or row["state"] not in (PlanState.PENDING, PlanState.APPROVED):
                return None
            if session_id is not None and row["session_id"] != str(session_id):
                return None
            if profile is not None and row["profile"] != str(profile):
                return None
            if workspace is not None and row["workspace"] != _canonical_workspace(workspace):
                return None
            if row["baseline_fingerprint"] != baseline_fingerprint:
                return None
            try:
                raw_envelope, _raw_plan, raw_manifest = _extract_envelope(row["raw_plan_json"])
                del raw_envelope, _raw_plan
                stored_manifest = json.loads(row["validated_manifest_json"])
                compiled_manifest = compile_execution_plan(stored_manifest).to_manifest()
            except Exception:
                return None
            if raw_manifest != compiled_manifest:
                return None
            digest = _manifest_digest(compiled_manifest)
            if row["approval_digest"] != digest:
                return None
            now = time.time()
            changed = conn.execute(
                """UPDATE bestplan_plans SET state=?, approved_at=COALESCE(approved_at, ?),
                   approved_by=COALESCE(approved_by, 'user:bare-go'), started_at=?
                   WHERE plan_id=? AND state IN (?, ?) AND approval_digest=?""",
                (
                    PlanState.RUNNING, now, now, plan_id,
                    PlanState.PENDING, PlanState.APPROVED, digest,
                ),
            ).rowcount
            if changed != 1:
                return None
            return dict(conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id = ?", (plan_id,),
            ).fetchone())
        return self._execute_write(claim)

    def prepare_dispatch_intent(
        self,
        plan_id: str,
        baseline_fingerprint: str,
        *,
        resolved_runtimes: list[dict[str, Any]],
        session_id: str,
        profile: str,
        workspace: str,
    ) -> Optional[dict[str, Any]]:
        """Atomically approve and persist the deterministic dispatch outbox."""
        dispatch_id = f"bestplan-{plan_id}"
        safe_runtimes = [
            {
                key: value for key, value in runtime.items()
                if key not in {"api_key", "credential", "token", "secret"}
            }
            for runtime in resolved_runtimes
        ]

        def prepare(conn):
            row = conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == PlanState.RUNNING and row["dispatch_state"] in {
                "intent", "unknown", "dispatching",
            }:
                return dict(row)
            if row["state"] not in (PlanState.PENDING, PlanState.APPROVED):
                return None
            if (
                row["session_id"] != str(session_id)
                or row["profile"] != str(profile)
                or row["workspace"] != _canonical_workspace(workspace)
                or row["baseline_fingerprint"] != baseline_fingerprint
            ):
                return None
            try:
                _raw, plan, raw_manifest = _extract_envelope(row["raw_plan_json"])
                _v1_plan_constraints(plan, workspace=workspace)
                stored_manifest = compile_execution_plan(
                    json.loads(row["validated_manifest_json"])
                ).to_manifest()
            except Exception:
                return None
            digest = _manifest_digest(stored_manifest)
            if raw_manifest != stored_manifest or row["approval_digest"] != digest:
                return None
            now = time.time()
            changed = conn.execute(
                """UPDATE bestplan_plans SET state=?, approved_at=COALESCE(approved_at, ?),
                   approved_by=COALESCE(approved_by, 'user:bare-go'), started_at=?,
                   dispatch_id=?, dispatch_state='intent', resolved_runtime_json=?,
                   delegation_ids_json=?, dispatch_updated_at=?, error=NULL
                   WHERE plan_id=? AND state IN (?, ?)""",
                (
                    PlanState.RUNNING, now, now, dispatch_id,
                    json.dumps(safe_runtimes, sort_keys=True),
                    json.dumps([dispatch_id]), now, plan_id,
                    PlanState.PENDING, PlanState.APPROVED,
                ),
            ).rowcount
            if changed != 1:
                return None
            return dict(conn.execute(
                "SELECT * FROM bestplan_plans WHERE plan_id=?", (plan_id,),
            ).fetchone())

        return self._execute_write(prepare)

    def begin_dispatch_attempt(self, plan_id: str) -> bool:
        now = time.time()
        return bool(self._execute_write(lambda conn: conn.execute(
            """UPDATE bestplan_plans SET dispatch_state='dispatching',
               dispatch_owner=?, dispatch_started_at=?, dispatch_updated_at=?
               WHERE plan_id=? AND state=? AND dispatch_state IN ('intent', 'unknown')""",
            (f"pid:{os.getpid()}", now, now, plan_id, PlanState.RUNNING),
        ).rowcount))

    def record_dispatch_unknown(self, plan_id: str, error: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            """UPDATE bestplan_plans SET dispatch_state='unknown', error=?,
               dispatch_updated_at=? WHERE plan_id=? AND state=?
               AND dispatch_state='dispatching'""",
            (str(error), time.time(), plan_id, PlanState.RUNNING),
        ).rowcount))

    def recover_dead_dispatch_owners(self) -> int:
        def recover(conn):
            rows = conn.execute(
                "SELECT plan_id, dispatch_owner FROM bestplan_plans "
                "WHERE state=? AND dispatch_state='dispatching'",
                (PlanState.RUNNING,),
            ).fetchall()
            changed = 0
            for row in rows:
                owner = str(row["dispatch_owner"] or "")
                if not owner.startswith("pid:"):
                    continue
                try:
                    pid = int(owner.split(":", 1)[1])
                    os.kill(pid, 0)
                    live = True
                except ProcessLookupError:
                    live = False
                except (PermissionError, ValueError):
                    live = True
                if live:
                    continue
                changed += conn.execute(
                    """UPDATE bestplan_plans SET dispatch_state='unknown',
                       dispatch_updated_at=?, error='recovered_dead_dispatch_owner'
                       WHERE plan_id=? AND dispatch_state='dispatching'
                       AND dispatch_owner=?""",
                    (time.time(), row["plan_id"], owner),
                ).rowcount
            return changed

        return int(self._execute_write(recover))

    def record_dispatch(
        self,
        plan_id: str,
        *,
        delegation_ids: list[str],
        sandbox_workspace: str = "",
    ) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            """UPDATE bestplan_plans SET state=?, delegation_ids_json=?,
               dispatch_state='dispatched', dispatch_updated_at=?, error=NULL,
               sandbox_workspace=?
               WHERE plan_id=? AND state=? AND dispatch_state='dispatching'""",
            (
                PlanState.WAITING, json.dumps(delegation_ids), time.time(),
                str(sandbox_workspace or ""), plan_id, PlanState.RUNNING,
            ),
        ).rowcount))

    def record_dispatch_failure(self, plan_id: str, error: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=?, error=?, completed_at=? "
            "WHERE plan_id=? AND state=?",
            (PlanState.FAILED, str(error), time.time(), plan_id, PlanState.RUNNING),
        ).rowcount))

    def record_dispatch_deferred(self, plan_id: str, error: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            """UPDATE bestplan_plans SET dispatch_state='intent', error=?,
               dispatch_updated_at=? WHERE plan_id=? AND state=?
               AND dispatch_state='dispatching'""",
            (str(error), time.time(), plan_id, PlanState.RUNNING),
        ).rowcount))

    def mark_completed_unverified(self, plan_id: str, evidence: dict[str, Any]) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=?, evidence_json=?, completed_at=? "
            "WHERE plan_id=? AND state=?",
            (PlanState.COMPLETED_UNVERIFIED, json.dumps(evidence, sort_keys=True), time.time(), plan_id, PlanState.WAITING),
        ).rowcount))

    def mark_completed_verified(self, plan_id: str) -> bool:
        return bool(self._execute_write(lambda conn: conn.execute(
            "UPDATE bestplan_plans SET state=?, completed_at=? WHERE plan_id=? AND state=?",
            (PlanState.COMPLETED_VERIFIED, time.time(), plan_id, PlanState.COMPLETED_UNVERIFIED),
        ).rowcount))


def capture_bestplan_response(
    response: str,
    *,
    session_id: str,
    workspace: str,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    store: Optional[BestplanStore] = None,
) -> PlanCapture:
    """Validate and persist the explicit envelope in a /bestplan response."""
    try:
        raw_envelope, plan, manifest = _extract_envelope(response)
        _v1_plan_constraints(plan, workspace=workspace)
    except BestplanError as exc:
        suffix = (
            "\n\n[Bestplan status: non-executable — the response did not contain "
            f"one valid machine envelope ({exc}).]"
        )
        return PlanCapture(False, str(response or "") + suffix, error=str(exc))
    digest = _manifest_digest(manifest)
    try:
        store = store or BestplanStore()
        plan_id = store.create_plan(
            "", plan, session_id=session_id, profile=profile, workspace=workspace,
            baseline_fingerprint=baseline_fingerprint, raw_envelope=raw_envelope,
        )
    except BaselineFingerprintError as exc:
        visible = _ENVELOPE_RE.sub("", str(response or "")).strip()
        suffix = f"\n\n[Bestplan status: non-executable — {exc}.]"
        return PlanCapture(False, visible + suffix, error=str(exc))
    advisory = _ENVELOPE_RE.sub("", str(response or "")).strip()
    authority = _render_authoritative_manifest(
        plan, workspace=workspace, digest=digest,
    )
    parts = []
    if advisory:
        parts.append("Model commentary (advisory only):\n" + advisory)
    parts.append(authority)
    parts.append(
        f"Bestplan executable receipt: {plan_id}. "
        "Reply with bare `go` to approve and dispatch exactly this host-rendered manifest."
    )
    return PlanCapture(True, "\n\n".join(parts), plan_id=plan_id, digest=digest)


def is_bestplan_invocation(message: Any) -> bool:
    """Recognize the raw or dynamic-skill-expanded /bestplan planning turn."""
    if not isinstance(message, str):
        return False
    stripped = message.lstrip()
    if re.match(r"^/bestplan(?:\s|$)", stripped, re.IGNORECASE):
        return True
    prefix = stripped[:500].casefold()
    return (
        prefix.startswith("[important: the user has invoked the ")
        and "bestplan" in prefix
        and " skill" in prefix
    )


def capture_bestplan_agent_result(
    result: dict[str, Any],
    *,
    invocation_message: Any,
    session_id: str,
    workspace: str,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    store: Optional[BestplanStore] = None,
    host_agent: Any = None,
) -> dict[str, Any]:
    """Attach the host-validated executable receipt to a planning result."""
    if not is_bestplan_invocation(invocation_message) or not isinstance(result, dict):
        return result
    capture = capture_bestplan_response(
        str(result.get("final_response") or ""),
        session_id=session_id,
        profile=profile,
        workspace=workspace,
        baseline_fingerprint=baseline_fingerprint,
        store=store,
    )
    updated = dict(result)
    updated["final_response"] = capture.response
    messages = list(updated.get("messages") or [])
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and item.get("role") == "assistant":
            replacement = dict(item)
            replacement["content"] = capture.response
            messages[index] = replacement
            break
    updated["messages"] = messages
    updated["bestplan_capture"] = {
        "executable": capture.executable,
        "plan_id": capture.plan_id,
        "digest": capture.digest,
        "error": capture.error,
    }
    if host_agent is not None:
        from agent.agent_runtime_helpers import repair_message_sequence

        repair_message_sequence(host_agent, messages)
        if callable(getattr(host_agent, "_persist_session", None)):
            host_agent._persist_session(messages, None)
    return updated


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:
        logger.debug("bestplan config load failed", exc_info=True)
        return {}


def is_go_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    cfg = config if config is not None else _load_config()
    return bool((cfg.get("autonomy") or {}).get("go_enabled"))


def recover_bestplan_dispatch_outbox(store: Optional[BestplanStore] = None) -> int:
    """Reconcile only dispatch attempts proven to belong to a dead process."""
    return (store or BestplanStore()).recover_dead_dispatch_owners()


def _configured_lane_error(tasks: list[dict[str, Any]], config: dict[str, Any]) -> Optional[str]:
    lanes = ((config.get("delegation") or {}).get("lanes") or {})
    if not isinstance(lanes, dict):
        lanes = {}
    for route in sorted({str(task.get("route") or "") for task in tasks}):
        lane = lanes.get(route)
        if not isinstance(lane, dict):
            return f"delegation.lanes.{route} is not configured"
        if not str(lane.get("provider") or "").strip():
            return f"delegation.lanes.{route}.provider is required"
        if not str(lane.get("model") or "").strip():
            return f"delegation.lanes.{route}.model is required"
    return None


def _delegation_ids(payload: Any) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        one = payload.get("delegation_id")
        if isinstance(one, str) and one:
            found.append(one)
        many = payload.get("delegation_ids")
        if isinstance(many, list):
            found.extend(str(item) for item in many if str(item or ""))
        for key in ("results", "dispatches"):
            values = payload.get(key)
            if isinstance(values, list):
                for value in values:
                    found.extend(_delegation_ids(value))
    return list(dict.fromkeys(found))


def try_resolve_go(
    message: str,
    *,
    session_id: str,
    workspace: str,
    parent_agent: Any,
    profile: str = "",
    baseline_fingerprint: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    store: Optional[BestplanStore] = None,
    delegate: Optional[Callable[..., Any]] = None,
    runtime_resolver: Optional[Callable[[list[dict[str, Any]], Any], list[dict[str, Any]]]] = None,
    strict_dispatcher: Optional[Callable[..., Any]] = None,
) -> ResolvedGo:
    """Resolve bare ``go`` before the model loop, failing closed around a plan."""
    cfg = config if config is not None else _load_config()
    if not is_go_enabled(cfg):
        return ResolvedGo(False, "disabled", reason="autonomy.go_enabled=false")
    if not _is_go_trigger(message):
        return ResolvedGo(False, "not_a_trigger", reason="only bare go is recognized")

    store = store or BestplanStore()
    recover_bestplan_dispatch_outbox(store)
    candidates = store.list_for_session(session_id)
    if not candidates:
        return ResolvedGo(False, "no_plan", reason="no pending plan exists for this session")

    expected_workspace = _canonical_workspace(workspace)
    expected_profile = str(profile)
    context_candidates = [
        row for row in candidates
        if row["profile"] == expected_profile and row["workspace"] == expected_workspace
    ]
    if not context_candidates:
        return ResolvedGo(True, "context_mismatch", reason="pending plan belongs to another profile or workspace")

    baseline = baseline_fingerprint or compute_baseline_fingerprint(workspace)
    exact = [row for row in context_candidates if row["baseline_fingerprint"] == baseline]
    if not exact:
        return ResolvedGo(True, "stale", reason="pending plan baseline no longer matches")
    if len(exact) != 1:
        return ResolvedGo(True, "ambiguous", reason=f"{len(exact)} plans match this turn")

    candidate = exact[0]
    plan_id = candidate["plan_id"]
    if candidate["state"] == PlanState.WAITING:
        return ResolvedGo(True, "already_claimed", plan_id=plan_id, reason="plan was already dispatched")

    try:
        _raw, plan, raw_manifest = _extract_envelope(candidate["raw_plan_json"])
        stored_manifest = compile_execution_plan(
            json.loads(candidate["validated_manifest_json"])
        ).to_manifest()
        if raw_manifest != stored_manifest:
            raise BestplanError("raw envelope and validated manifest differ")
        if candidate["approval_digest"] != _manifest_digest(stored_manifest):
            raise BestplanError("approval digest does not match manifest")
        tasks = _plan_to_delegate_tasks(plan, workspace=expected_workspace)
    except Exception as exc:
        return ResolvedGo(True, "invalid_plan", plan_id=plan_id, reason=str(exc), error=str(exc))

    if parent_agent is None:
        return ResolvedGo(True, "dispatch_unavailable", plan_id=plan_id, reason="live parent agent is required")
    try:
        from gateway.session_context import async_delivery_supported
        if not async_delivery_supported():
            return ResolvedGo(True, "async_unsupported", plan_id=plan_id, reason="host cannot deliver detached completion")
    except Exception:
        return ResolvedGo(True, "async_context_error", plan_id=plan_id, reason="delivery capability could not be verified")

    resolved_runtimes: list[dict[str, Any]]
    if candidate["state"] == PlanState.RUNNING:
        dispatch_id = str(candidate.get("dispatch_id") or f"bestplan-{plan_id}")
        if candidate.get("dispatch_state") == "dispatching":
            return ResolvedGo(
                True, "dispatch_in_flight", plan_id=plan_id,
                delegation_id=dispatch_id,
                reason="another idempotent dispatch attempt owns the durable intent",
            )
        try:
            stored_runtimes = json.loads(candidate.get("resolved_runtime_json") or "[]")
        except Exception:
            stored_runtimes = []
        if runtime_resolver is None:
            try:
                from tools.delegate_tool import resolve_bestplan_runtime_specs
                resolved_runtimes = resolve_bestplan_runtime_specs(
                    tasks, parent_agent, expected=stored_runtimes,
                )
            except Exception as exc:
                return ResolvedGo(True, "lane_unavailable", plan_id=plan_id, reason=str(exc))
        else:
            resolved_runtimes = runtime_resolver(tasks, parent_agent)
    else:
        if runtime_resolver is None and delegate is not None:
            lane_error = _configured_lane_error(tasks, cfg)
            if lane_error:
                return ResolvedGo(True, "lane_unavailable", plan_id=plan_id, reason=lane_error)
            lanes = ((cfg.get("delegation") or {}).get("lanes") or {})
            resolved_runtimes = [
                {
                    "route": task["route"],
                    "provider": lanes[task["route"]]["provider"],
                    "model": lanes[task["route"]]["model"],
                }
                for task in tasks
            ]
        else:
            try:
                if runtime_resolver is None:
                    from tools.delegate_tool import resolve_bestplan_runtime_specs
                    resolved_runtimes = resolve_bestplan_runtime_specs(tasks, parent_agent)
                else:
                    resolved_runtimes = runtime_resolver(tasks, parent_agent)
            except Exception as exc:
                return ResolvedGo(True, "lane_unavailable", plan_id=plan_id, reason=str(exc))
        claimed = store.prepare_dispatch_intent(
            plan_id, baseline, resolved_runtimes=resolved_runtimes,
            session_id=session_id, profile=expected_profile, workspace=expected_workspace,
        )
        if claimed is None:
            return ResolvedGo(True, "already_claimed", plan_id=plan_id, reason="atomic intent claim lost or validation changed")
        candidate = claimed

    dispatch_id = str(candidate.get("dispatch_id") or f"bestplan-{plan_id}")
    if not store.begin_dispatch_attempt(plan_id):
        row = store.get_plan(plan_id) or {}
        if row.get("state") == PlanState.WAITING:
            return ResolvedGo(True, "already_claimed", plan_id=plan_id, delegation_id=dispatch_id)
        return ResolvedGo(
            True, "dispatch_in_flight", plan_id=plan_id, delegation_id=dispatch_id,
            reason="durable dispatch attempt already owned",
        )

    if strict_dispatcher is None:
        if delegate is not None:
            strict_dispatcher = lambda **kwargs: delegate(
                tasks=kwargs["tasks"], parent_agent=kwargs["parent_agent"], background=True,
            )
        else:
            from tools.delegate_tool import dispatch_bestplan_tasks_async
            strict_dispatcher = dispatch_bestplan_tasks_async
    try:
        raw_result = strict_dispatcher(
            tasks=tasks,
            parent_agent=parent_agent,
            dispatch_id=dispatch_id,
            plan_id=plan_id,
            workspace=expected_workspace,
            resolved_runtimes=resolved_runtimes,
        )
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if not isinstance(result, dict) or result.get("status") != "dispatched":
            error = str((result or {}).get("error") if isinstance(result, dict) else result)
            if isinstance(result, dict) and result.get("status") == "rejected":
                store.record_dispatch_deferred(plan_id, error)
                return ResolvedGo(
                    True, "dispatch_deferred", plan_id=plan_id,
                    delegation_id=dispatch_id, reason=error,
                )
            store.record_dispatch_failure(plan_id, error)
            return ResolvedGo(
                True, "dispatch_failed", plan_id=plan_id,
                delegation_id=dispatch_id, reason=error, error=error,
            )
        delegation_ids = _delegation_ids(result)
        if not delegation_ids:
            raise BestplanError("delegate_task returned no delegation id")
    except Exception as exc:
        store.record_dispatch_unknown(plan_id, str(exc))
        return ResolvedGo(
            True, "possibly_dispatched", plan_id=plan_id,
            delegation_id=dispatch_id, reason=str(exc), error=str(exc),
        )

    if not store.record_dispatch(
        plan_id,
        delegation_ids=delegation_ids,
        sandbox_workspace=str(result.get("sandbox_workspace") or ""),
    ):
        return ResolvedGo(True, "dispatch_state_error", plan_id=plan_id, reason="delegation id was not persisted")
    return ResolvedGo(True, "waiting", plan_id=plan_id, delegation_id=delegation_ids[0])
