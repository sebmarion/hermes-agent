"""Local session-to-action radar for Hermes.

This module reads the local Hermes ``state.db`` and turns repeated session
friction into privacy-preserving action candidates.  It intentionally emits
session/message references and short machine labels by default, not raw
transcript excerpts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Iterable, Literal

from utils import atomic_json_write

Route = Literal["FIX", "CONFIG", "SKILL_PATCH", "CRON", "DECIDE", "IGNORE"]
Confidence = Literal["high", "medium", "low"]
CandidateStatus = Literal[
    "new", "accepted", "deferred", "resolved", "ignored", "regressed"
]

_CORRECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("did_you_check", r"\b(did you check|did you verify|did you test)\b"),
        ("still_broken", r"\b(still broken|still failing|doesn't work|not working)\b"),
        ("wtf", r"\b(wtf|what the fuck)\b"),
        ("are_we_done", r"\b(are we done|is it done|done\?)\b"),
        ("wrong_repo", r"\b(wrong repo|wrong project|wrong workspace|wrong directory)\b"),
        ("provider_drift", r"\b(provider|model|openrouter|local|zeus|qwen|glm|routing)\b"),
        ("verification_gap", r"\b(proof|evidence|verified|pass/fail|read[- ]back|screenshot)\b"),
        ("cron_noise", r"\b(cron|watchdog|scheduled|digest|silent|noise)\b"),
    )
)

_DONE_CLAIM_RE = re.compile(
    r"\b(done|fixed|verified|deployed|pushed|published|resolved|working|passes|pass:)\b",
    re.IGNORECASE,
)
_PROOF_RE = re.compile(
    r"\b(exit\s*0|200\s*ok|http\s*200|pytest|passed|pass:|screenshot|media:|read[- ]back|sha|commit|diff --check)\b",
    re.IGNORECASE,
)
_SECRETISH_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|bearer\s+[a-z0-9._=-]{12,})",
    re.IGNORECASE,
)
_PII_RE = re.compile(
    r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d\s().-]{8,}\d|/Users/[^\s/]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceRef:
    session_id: str
    message_id: int | None = None
    signal: str = ""
    observed_at: float = 0.0
    snippet: str | None = None


@dataclass
class ActionCandidate:
    id: str
    title: str
    route: Route
    confidence: Confidence
    score: float
    evidence_count: int
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)
    models: dict[str, int] = field(default_factory=dict)
    why_now: str = ""
    cost_of_ignoring: str = ""
    first_move: str = ""
    proof_gate: str = ""
    safety_boundary: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = [asdict(ref) for ref in self.evidence_refs]
        return data


class TrajectoryRadar:
    """Generate action candidates from local Hermes session history."""

    def __init__(self, db: Any):
        self.db = db
        self._conn = db._conn

    def generate(
        self,
        *,
        days: int = 14,
        source: str | None = None,
        limit: int = 10,
        include_snippets: bool = False,
    ) -> dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days must be positive")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be zero or positive")
        snapshot_to = time.time()
        cutoff = snapshot_to - (days * 86400)
        sessions = self._sessions(cutoff, snapshot_to, source)
        signals = self._signals(
            cutoff,
            snapshot_to,
            source,
            include_snippets=include_snippets,
        )
        candidates = self._build_candidates(sessions, signals)
        candidates.sort(key=lambda item: (-item.score, item.title))
        total_candidate_count = len(candidates)
        if limit > 0:
            candidates = candidates[:limit]
        return {
            "generated_at": datetime.fromtimestamp(
                snapshot_to, tz=timezone.utc
            ).isoformat(),
            "window": {
                "days": days,
                "from_epoch": cutoff,
                "to_epoch": snapshot_to,
            },
            "source_filter": source,
            "privacy": {
                "raw_transcripts_included": bool(include_snippets),
                "default_evidence": "session_id/message_id/signal only",
            },
            "totals": self._totals(sessions),
            "candidate_set_complete": limit <= 0 or total_candidate_count <= limit,
            "candidate_count_before_limit": total_candidate_count,
            "candidates": [candidate.to_dict() for candidate in candidates],
        }

    def _sessions(
        self,
        cutoff: float,
        snapshot_to: float,
        source: str | None,
    ) -> list[dict[str, Any]]:
        cols = (
            "s.id, s.source, s.model, s.started_at, s.ended_at, s.message_count, "
            "s.tool_call_count, s.input_tokens, s.output_tokens, s.cache_read_tokens, "
            "s.cache_write_tokens, s.cwd, s.git_repo_root, s.billing_provider, "
            "s.billing_base_url, s.title, "
            "COALESCE(activity.observed_at, s.started_at) AS observed_at"
        )
        params: list[Any] = [cutoff, snapshot_to, cutoff, snapshot_to]
        source_clause = ""
        if source:
            source_clause = " AND s.source = ?"
            params.append(source)
        cursor = self._conn.execute(
            f"""
            SELECT {cols}
              FROM sessions s
              LEFT JOIN (
                    SELECT session_id, MAX(timestamp) AS observed_at
                      FROM messages
                     WHERE active = 1
                       AND timestamp BETWEEN ? AND ?
                     GROUP BY session_id
              ) activity ON activity.session_id = s.id
             WHERE (
                       s.started_at BETWEEN ? AND ?
                       OR activity.observed_at IS NOT NULL
                   ){source_clause}
            """,
            tuple(params),
        )
        return [dict(row) for row in cursor.fetchall()]

    def _signals(
        self,
        cutoff: float,
        snapshot_to: float,
        source: str | None,
        *,
        include_snippets: bool,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [cutoff, snapshot_to]
        source_clause = ""
        if source:
            source_clause = " AND s.source = ?"
            params.append(source)
        cursor = self._conn.execute(
            f"""
            SELECT m.id AS message_id, m.session_id, m.role, m.content,
                   m.timestamp AS observed_at,
                   m.tool_name, m.tool_calls, s.source, s.model, s.cwd, s.git_repo_root,
                   s.billing_provider
              FROM messages m
              JOIN sessions s ON s.id = m.session_id
             WHERE m.timestamp BETWEEN ? AND ?{source_clause}
               AND m.active = 1
               AND m.role IN ('user', 'assistant', 'tool')
             ORDER BY m.timestamp ASC
            """,
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            item = dict(row)
            content = item.get("content") or ""
            if item.get("role") == "user":
                for label, pattern in _CORRECTION_PATTERNS:
                    if pattern.search(content):
                        out.append(self._signal(item, label, include_snippets))
            elif item.get("role") == "assistant":
                if _DONE_CLAIM_RE.search(content) and not _PROOF_RE.search(content):
                    out.append(self._signal(item, "done_claim_without_proof", include_snippets))
                if "[SILENT]" in content and len(content.strip()) > len("[SILENT]"):
                    out.append(self._signal(item, "cron_silent_contract_drift", include_snippets))
            elif item.get("role") == "tool":
                lower = content.lower()
                if "error" in lower or "traceback" in lower or "timed out" in lower:
                    out.append(self._signal(item, "tool_error", include_snippets))
        return out

    def _signal(self, row: dict[str, Any], label: str, include_snippets: bool) -> dict[str, Any]:
        signal = {
            "label": label,
            "session_id": row.get("session_id") or "",
            "message_id": row.get("message_id"),
            "source": row.get("source") or "unknown",
            "model": row.get("model") or "unknown",
            "project": _project_label(row),
            "observed_at": float(row.get("observed_at") or 0.0),
        }
        if include_snippets:
            signal["snippet"] = _safe_snippet(row.get("content") or "")
        return signal

    def _build_candidates(
        self,
        sessions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
    ) -> list[ActionCandidate]:
        by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for signal in signals:
            by_label[signal["label"]].append(signal)

        candidates: list[ActionCandidate] = []
        candidates.extend(self._verification_candidate(by_label))
        candidates.extend(self._routing_candidate(sessions, by_label))
        candidates.extend(self._cron_candidate(by_label))
        candidates.extend(self._context_candidate(by_label))
        candidates.extend(self._tool_error_candidates(by_label))
        return [c for c in candidates if c.evidence_count > 0]

    def _verification_candidate(self, by_label: dict[str, list[dict[str, Any]]]) -> list[ActionCandidate]:
        rows = _merge_signal_rows(
            by_label,
            ["did_you_check", "are_we_done", "verification_gap", "done_claim_without_proof"],
        )
        return [
            self._candidate(
                "done-means-proven-gatekeeper",
                "Done Means Proven Gatekeeper",
                "FIX",
                rows,
                base_score=80,
                why_now="Repeated verification/correction language indicates completion claims need proof gates before they reach Seb.",
                cost="Unverified 'done' claims create rework, distrust, and extra sessions asking for evidence.",
                first="Add a report-mode final-response linter for strong completion claims without command/API/read-back evidence.",
                proof="Replay fixture finals: weak 'Done.' is flagged; 'PASS: pytest … exit 0' is not; honest blockers are not flagged.",
                safety="Report/warn first. Do not hard-block planning, casual chat, or explicit 'not verified' blockers.",
            )
        ]

    def _routing_candidate(
        self,
        sessions: list[dict[str, Any]],
        by_label: dict[str, list[dict[str, Any]]],
    ) -> list[ActionCandidate]:
        hosted_sessions = [s for s in sessions if _looks_hosted(s)]
        local_rows = by_label.get("provider_drift", [])
        refs = list(local_rows)
        for session in hosted_sessions[:50]:
            refs.append(
                {
                    "label": "hosted_model_usage",
                    "session_id": session.get("id") or "",
                    "message_id": None,
                    "source": session.get("source") or "unknown",
                    "model": session.get("model") or "unknown",
                    "project": _project_label(session),
                    "observed_at": float(session.get("observed_at") or 0.0),
                }
            )
        return [
            self._candidate(
                "local-first-dispatch-firewall",
                "Local-First Dispatch Firewall",
                "CONFIG",
                refs,
                base_score=60,
                why_now="Recent sessions repeatedly mention provider/model routing, and hosted usage should be explainable when local Zeus/Qwen is healthy.",
                cost="Accidental hosted routing burns tokens, hides local-runtime failures, and can leak work that should stay local.",
                first="Ship `hermes routing audit/explain` in audit mode before changing runtime behavior.",
                proof="Audit explains every hosted route as explicit, delegation/reviewer, context overflow, health cooldown, or unknown drift.",
                safety="Do not enforce initially. Delegation/reviewer routes stay cloud because the local GPU is single-slot.",
            )
        ]

    def _cron_candidate(self, by_label: dict[str, list[dict[str, Any]]]) -> list[ActionCandidate]:
        rows = _merge_signal_rows(by_label, ["cron_noise", "cron_silent_contract_drift"])
        return [
            self._candidate(
                "quiet-learning-cron-digest",
                "Quiet Learning Cron Digest",
                "CRON",
                rows,
                base_score=45,
                why_now="Cron/watchdog mentions suggest recurring work needs a quiet producer plus digest pattern, not more raw pings.",
                cost="Noisy jobs train Seb to ignore automation; silent failures make useful learning disappear.",
                first="Convert eligible producers to local artifacts and add one digest that reports only meaningful deltas.",
                proof="Scheduler read-back shows producers deliver local; digest returns [SILENT] when no meaningful changes exist.",
                safety="No mutating cron jobs without explicit plan; no recursive cron creation from cron sessions.",
            )
        ]

    def _context_candidate(self, by_label: dict[str, list[dict[str, Any]]]) -> list[ActionCandidate]:
        rows = _merge_signal_rows(by_label, ["wrong_repo"])
        return [
            self._candidate(
                "workspace-context-preflight",
                "Workspace Context Preflight",
                "SKILL_PATCH",
                rows,
                base_score=40,
                why_now="Wrong-repo/workspace corrections indicate the cheapest fix is stronger pre-edit path verification.",
                cost="Wrong checkout edits waste high-context turns and risk polluting unrelated projects.",
                first="Patch the governing coding/project-context skill to require current workspace + git top-level read-back before edits.",
                proof="Future coding receipts cite workspace, git top-level, and local AGENTS.md before patching.",
                safety="Do not store temporary task state in durable memory; keep project facts scoped to project context.",
            )
        ]

    def _tool_error_candidates(self, by_label: dict[str, list[dict[str, Any]]]) -> list[ActionCandidate]:
        rows = by_label.get("tool_error", [])
        return [
            self._candidate(
                "tool-error-root-cause-loop",
                "Tool Error Root-Cause Loop",
                "FIX",
                rows,
                base_score=35,
                why_now="Repeated tool errors/timeouts should become specific precondition checks or tool-contract fixes, not repeated manual retries.",
                cost="Tool failures consume turns and mask whether the bug is user state, environment, or Hermes itself.",
                first="Group top failing tool names/errors and patch the highest-repeat precondition or error message.",
                proof="A fixture reproduces the failure class and now emits the actionable precondition or passes the corrected path.",
                safety="Do not suppress errors; preserve compact failure details in logs/artifacts.",
            )
        ]

    def _candidate(
        self,
        cid: str,
        title: str,
        route: Route,
        rows: list[dict[str, Any]],
        *,
        base_score: float,
        why_now: str,
        cost: str,
        first: str,
        proof: str,
        safety: str,
    ) -> ActionCandidate:
        evidence = _dedupe_refs(rows)
        sources = Counter(row.get("source") or "unknown" for row in rows)
        models = Counter(row.get("model") or "unknown" for row in rows)
        projects = [name for name, _count in Counter(row.get("project") or "unknown" for row in rows).most_common(5)]
        score = base_score + min(len(evidence), 50) + (len(sources) * 1.5)
        confidence: Confidence = "high" if len(evidence) >= 8 else "medium" if len(evidence) >= 3 else "low"
        return ActionCandidate(
            id=cid,
            title=title,
            route=route,
            confidence=confidence,
            score=round(score, 1),
            evidence_count=len(evidence),
            # CandidateStore hashes this bounded window to detect fresh
            # evidence. Keeping the oldest refs would make regression
            # detection stop once a candidate accumulated more than 20.
            evidence_refs=evidence[-20:],
            projects=projects,
            sources=dict(sources.most_common()),
            models=dict(models.most_common(8)),
            why_now=why_now,
            cost_of_ignoring=cost,
            first_move=first,
            proof_gate=proof,
            safety_boundary=safety,
        )

    def _totals(self, sessions: Iterable[dict[str, Any]]) -> dict[str, Any]:
        rows = list(sessions)
        return {
            "sessions": len(rows),
            "messages": sum(int(row.get("message_count") or 0) for row in rows),
            "tool_calls": sum(int(row.get("tool_call_count") or 0) for row in rows),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
            "sources": dict(Counter(row.get("source") or "unknown" for row in rows).most_common()),
            "models": dict(Counter(row.get("model") or "unknown" for row in rows).most_common(10)),
        }


_CANDIDATE_STORE_VERSION = 1
_VALID_CANDIDATE_STATUSES: frozenset[str] = frozenset(
    {"new", "accepted", "deferred", "resolved", "ignored", "regressed"}
)
_REGRESSIBLE_CANDIDATE_STATUSES: frozenset[str] = frozenset(
    {"accepted", "deferred", "resolved"}
)
_VALID_CONFIRMATIONS: frozenset[str] = frozenset(
    {"unconfirmed", "pending", "confirmed", "regressed"}
)
_CANDIDATE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_EVIDENCE_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_REPORT_MAX_AGE_SECONDS = 300.0
_REPORT_FUTURE_SKEW_SECONDS = 0.0
_REPORT_TIMESTAMP_TOLERANCE_SECONDS = 1.0
_REPORT_SPAN_TOLERANCE_SECONDS = 0.001
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class CandidateStoreError(RuntimeError):
    """The candidate store could not be read or durably updated."""


@dataclass
class CandidateRecord:
    """Privacy-minimized, profile-local lifecycle state for one candidate."""

    fingerprint: str
    title: str = ""
    route: str = ""
    status: CandidateStatus = "new"
    first_seen: float = 0.0
    last_seen: float = 0.0
    resolved_at: float | None = None
    last_action_at: float = 0.0
    last_evidence_count: int = 0
    last_score: float = 0.0
    confirmation: str = "unconfirmed"
    evidence_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _store_thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _candidate_store_transaction_lock(path: Path) -> Iterator[None]:
    """Hold one transaction lock across reload, mutation, and atomic replace."""
    thread_lock = _store_thread_lock(path)
    with thread_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.with_name(f".{path.name}.lock").open("a+b")
        except OSError as exc:
            raise CandidateStoreError(
                f"candidate store lock could not be opened: {path}"
            ) from exc

        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        except CandidateStoreError:
            raise
        except OSError as exc:
            raise CandidateStoreError(
                f"candidate store transaction lock failed: {path}"
            ) from exc
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateStoreError(f"corrupt candidate store field: {field_name}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise CandidateStoreError(
            f"corrupt candidate store field: {field_name}"
        ) from exc
    if not math.isfinite(number):
        raise CandidateStoreError(f"corrupt candidate store field: {field_name}")
    return number


def _record_from_json(entry: Any) -> CandidateRecord:
    if not isinstance(entry, dict):
        raise CandidateStoreError("corrupt candidate store record")
    expected_fields = set(CandidateRecord.__dataclass_fields__)
    if set(entry) != expected_fields:
        raise CandidateStoreError("ambiguous candidate store record fields")
    fingerprint = entry.get("fingerprint")
    if not isinstance(fingerprint, str) or not _CANDIDATE_ID_RE.fullmatch(fingerprint):
        raise CandidateStoreError("corrupt candidate fingerprint")
    title = entry.get("title")
    route = entry.get("route")
    status = entry.get("status")
    confirmation = entry.get("confirmation")
    if not isinstance(title, str) or not isinstance(route, str):
        raise CandidateStoreError(f"corrupt candidate metadata: {fingerprint}")
    if route not in {"", "FIX", "CONFIG", "SKILL_PATCH", "CRON", "DECIDE", "IGNORE"}:
        raise CandidateStoreError(f"corrupt candidate route: {fingerprint}")
    if _SECRETISH_RE.search(title) or _PII_RE.search(title):
        raise CandidateStoreError(f"private candidate metadata: {fingerprint}")
    if status not in _VALID_CANDIDATE_STATUSES:
        raise CandidateStoreError(f"corrupt candidate status: {fingerprint}")
    if confirmation not in _VALID_CONFIRMATIONS:
        raise CandidateStoreError(f"corrupt candidate confirmation: {fingerprint}")

    hashes = entry.get("evidence_hashes")
    if not isinstance(hashes, list) or any(
        not isinstance(value, str) or not _EVIDENCE_DIGEST_RE.fullmatch(value)
        for value in hashes
    ):
        raise CandidateStoreError(f"corrupt candidate evidence: {fingerprint}")
    if len(hashes) != len(set(hashes)):
        raise CandidateStoreError(f"duplicate candidate evidence: {fingerprint}")
    if len(hashes) > 256:
        raise CandidateStoreError(f"oversized candidate evidence: {fingerprint}")

    resolved_raw = entry.get("resolved_at")
    resolved_at = (
        None
        if resolved_raw is None
        else _finite_number(resolved_raw, field_name="resolved_at")
    )
    if (status == "resolved") != (resolved_at is not None):
        raise CandidateStoreError(f"ambiguous resolved state: {fingerprint}")
    expected_confirmation = {
        "resolved": {"pending", "confirmed"},
        "regressed": {"regressed"},
    }.get(status, {"unconfirmed"})
    if confirmation not in expected_confirmation:
        raise CandidateStoreError(f"ambiguous confirmation state: {fingerprint}")
    evidence_count = entry.get("last_evidence_count")
    if isinstance(evidence_count, bool) or not isinstance(evidence_count, int) or evidence_count < 0:
        raise CandidateStoreError(
            f"corrupt candidate evidence count: {fingerprint}"
        )
    return CandidateRecord(
        fingerprint=fingerprint,
        title=title,
        route=route,
        status=status,
        first_seen=_finite_number(entry.get("first_seen"), field_name="first_seen"),
        last_seen=_finite_number(entry.get("last_seen"), field_name="last_seen"),
        resolved_at=resolved_at,
        last_action_at=_finite_number(
            entry.get("last_action_at"), field_name="last_action_at"
        ),
        last_evidence_count=evidence_count,
        last_score=_finite_number(entry.get("last_score"), field_name="last_score"),
        confirmation=confirmation,
        evidence_hashes=list(hashes),
    )


def _clone_record(record: CandidateRecord) -> CandidateRecord:
    return replace(record, evidence_hashes=list(record.evidence_hashes))


def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("id") or "").strip().lower()
    if not raw:
        return ""
    if _CANDIDATE_ID_RE.fullmatch(raw):
        return raw
    return "candidate-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _privacy_safe_candidate_title(value: Any) -> str:
    title = " ".join(str(value or "").split())[:160]
    if not title:
        return ""
    if _SECRETISH_RE.search(title) or _PII_RE.search(title):
        return "[redacted candidate]"
    return title


def _candidate_evidence_facts(
    candidate: dict[str, Any],
    fingerprint: str,
    *,
    report_from: float,
    report_to: float,
) -> list[tuple[str, float]]:
    facts: dict[str, float] = {}
    refs = candidate.get("evidence_refs") or []
    if not isinstance(refs, list):
        raise CandidateStoreError("candidate evidence refs must be a list")
    for ref in refs:
        if not isinstance(ref, dict):
            raise CandidateStoreError("candidate evidence ref must be an object")
        raw = "\x1f".join(
            (
                fingerprint,
                str(ref.get("session_id") or ""),
                str(ref.get("message_id") if ref.get("message_id") is not None else ""),
                str(ref.get("signal") or ""),
            )
        )
        if raw.strip("\x1f") != fingerprint:
            observed_raw = ref.get("observed_at")
            if isinstance(observed_raw, bool) or not isinstance(
                observed_raw, (int, float)
            ):
                raise CandidateStoreError(
                    "candidate evidence ref observed_at must be a finite timestamp"
                )
            try:
                observed_at = float(observed_raw)
            except OverflowError as exc:
                raise CandidateStoreError(
                    "candidate evidence ref observed_at must be a finite timestamp"
                ) from exc
            if (
                not math.isfinite(observed_at)
                or observed_at <= 0
                or not report_from <= observed_at <= report_to
            ):
                raise CandidateStoreError(
                    "candidate evidence ref observed_at is outside the report window"
                )
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            facts[digest] = max(facts.get(digest, observed_at), observed_at)
    return list(facts.items())


def _validated_report_envelope(
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], float, float, float]:
    """Validate the facts required for safe regression/absence decisions."""
    if not isinstance(report, dict):
        raise CandidateStoreError("candidate report must be an object")
    required = {
        "generated_at",
        "window",
        "source_filter",
        "candidate_set_complete",
        "candidate_count_before_limit",
        "candidates",
    }
    missing = sorted(required - set(report))
    if missing:
        raise CandidateStoreError(
            f"candidate report missing required field: {missing[0]}"
        )

    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise CandidateStoreError("candidate report candidates must be a list")
    if any(not isinstance(candidate, dict) for candidate in candidates):
        raise CandidateStoreError("candidate report entry must be an object")

    complete = report.get("candidate_set_complete")
    if not isinstance(complete, bool):
        raise CandidateStoreError("candidate report completeness must be a boolean")
    count = report.get("candidate_count_before_limit")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CandidateStoreError("candidate report count must be a non-negative integer")
    if (complete and count != len(candidates)) or (
        not complete and count <= len(candidates)
    ):
        raise CandidateStoreError("candidate report count/completeness is inconsistent")

    source_filter = report.get("source_filter")
    if source_filter is not None and not isinstance(source_filter, str):
        raise CandidateStoreError("candidate report source_filter is invalid")

    window = report.get("window")
    if not isinstance(window, dict):
        raise CandidateStoreError("candidate report window must be an object")
    days = window.get("days")
    try:
        day_count = float(days)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateStoreError(
            "candidate report window days must be positive"
        ) from exc
    if (
        isinstance(days, bool)
        or not isinstance(days, (int, float))
        or not math.isfinite(day_count)
        or day_count <= 0
    ):
        raise CandidateStoreError("candidate report window days must be positive")
    window_from = _finite_number(
        window.get("from_epoch"), field_name="window.from_epoch"
    )
    window_to = _finite_number(window.get("to_epoch"), field_name="window.to_epoch")
    if window_from >= window_to:
        raise CandidateStoreError("candidate report window is invalid")
    expected_span = day_count * 86400
    if not math.isfinite(expected_span) or not math.isclose(
        window_to - window_from,
        expected_span,
        rel_tol=0.0,
        abs_tol=_REPORT_SPAN_TOLERANCE_SECONDS,
    ):
        raise CandidateStoreError("candidate report window span is inconsistent")

    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise CandidateStoreError("candidate report generated_at is invalid")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        generated_epoch = generated.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise CandidateStoreError("candidate report generated_at is invalid") from exc
    if generated.tzinfo is None or not math.isclose(
        generated_epoch,
        window_to,
        rel_tol=0.0,
        abs_tol=_REPORT_TIMESTAMP_TOLERANCE_SECONDS,
    ):
        raise CandidateStoreError("candidate report timestamp/window is inconsistent")
    now = time.time()
    if generated_epoch < now - _REPORT_MAX_AGE_SECONDS:
        raise CandidateStoreError("candidate report is stale")
    if generated_epoch > now + _REPORT_FUTURE_SKEW_SECONDS:
        raise CandidateStoreError("candidate report is from the future")
    return candidates, window_from, window_to, generated_epoch


class CandidateStore:
    """Transaction-safe profile-local candidate lifecycle JSON store."""

    def __init__(self, path: Path | str | None = None):
        if path is None:
            from hermes_constants import get_hermes_home

            path = get_hermes_home() / "radar_candidates.json"
        self._path = Path(path).expanduser().resolve(strict=False)
        self._records: dict[str, CandidateRecord] = {}
        with _candidate_store_transaction_lock(self._path):
            self._records = self._read_records_unlocked()

    @property
    def path(self) -> Path:
        return self._path

    def _read_records_unlocked(self) -> dict[str, CandidateRecord]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CandidateStoreError(
                f"corrupt candidate store JSON: {self._path}"
            ) from exc
        except OSError as exc:
            raise CandidateStoreError(
                f"candidate store could not be read: {self._path}"
            ) from exc

        if not isinstance(raw, dict):
            raise CandidateStoreError("corrupt candidate store root")
        if set(raw) != {"version", "records"}:
            raise CandidateStoreError("ambiguous candidate store root fields")
        if raw.get("version") != _CANDIDATE_STORE_VERSION:
            raise CandidateStoreError("corrupt or unsupported candidate store version")
        entries = raw.get("records")
        if not isinstance(entries, list):
            raise CandidateStoreError("corrupt candidate store records")

        records: dict[str, CandidateRecord] = {}
        for entry in entries:
            record = _record_from_json(entry)
            if record.fingerprint in records:
                raise CandidateStoreError(
                    f"duplicate candidate fingerprint: {record.fingerprint}"
                )
            records[record.fingerprint] = record
        return records

    def _write_records_unlocked(self, records: dict[str, CandidateRecord]) -> None:
        payload = {
            "version": _CANDIDATE_STORE_VERSION,
            "records": [
                records[fingerprint].to_dict()
                for fingerprint in sorted(records)
            ],
        }
        try:
            atomic_json_write(
                self._path,
                payload,
                indent=2,
                sort_keys=True,
                mode=0o600,
            )
        except Exception as exc:
            raise CandidateStoreError(
                f"candidate store write failed: {self._path}"
            ) from exc

    def _reload(self) -> dict[str, CandidateRecord]:
        records = self._read_records_unlocked()
        self._records = records
        return records

    def get(self, fingerprint: str) -> CandidateRecord | None:
        with _candidate_store_transaction_lock(self._path):
            record = self._reload().get(fingerprint)
            return _clone_record(record) if record is not None else None

    def list(self, *, status: str | None = None) -> list[CandidateRecord]:
        if status is not None and status not in _VALID_CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status: {status!r}")
        with _candidate_store_transaction_lock(self._path):
            records = list(self._reload().values())
        if status is not None:
            records = [record for record in records if record.status == status]
        records.sort(key=lambda record: (-record.last_seen, record.fingerprint))
        return [_clone_record(record) for record in records]

    def sync_from_report(self, report: dict[str, Any]) -> list[str]:
        """Merge one report without losing concurrent lifecycle mutations."""
        candidates, report_from, report_to, generated_at = (
            _validated_report_envelope(report)
        )

        with _candidate_store_transaction_lock(self._path):
            records = self._read_records_unlocked()
            before = {
                fingerprint: record.to_dict()
                for fingerprint, record in records.items()
            }
            seen: set[str] = set()
            regressed: list[str] = []
            now = time.time()

            for candidate in candidates:
                candidate_id = candidate.get("id")
                if not isinstance(candidate_id, str) or not candidate_id.strip():
                    raise CandidateStoreError("candidate report id must be a non-empty string")
                fingerprint = _candidate_fingerprint(candidate)
                if fingerprint in seen:
                    raise CandidateStoreError(
                        f"duplicate candidate in report: {fingerprint}"
                    )
                seen.add(fingerprint)
                incoming_evidence = _candidate_evidence_facts(
                    candidate,
                    fingerprint,
                    report_from=report_from,
                    report_to=report_to,
                )
                incoming_hashes = [digest for digest, _observed in incoming_evidence]
                record = records.get(fingerprint)
                if not isinstance(candidate.get("title"), str):
                    raise CandidateStoreError(
                        f"candidate report has invalid title: {fingerprint}"
                    )
                title = _privacy_safe_candidate_title(candidate.get("title"))
                route_raw = str(candidate.get("route") or "").strip().upper()
                if route_raw not in {
                    "FIX", "CONFIG", "SKILL_PATCH", "CRON", "DECIDE", "IGNORE"
                }:
                    raise CandidateStoreError(
                        f"candidate report has invalid route: {fingerprint}"
                    )
                route = route_raw
                evidence_count_raw = candidate.get("evidence_count", 0)
                if (
                    isinstance(evidence_count_raw, bool)
                    or not isinstance(evidence_count_raw, int)
                    or evidence_count_raw < 0
                ):
                    raise CandidateStoreError(
                        f"candidate report has invalid evidence count: {fingerprint}"
                    )
                evidence_count = evidence_count_raw
                if evidence_count and not incoming_hashes:
                    raise CandidateStoreError(
                        f"candidate report lacks hashable evidence: {fingerprint}"
                    )
                score_raw = candidate.get("score", 0.0)
                if (
                    isinstance(score_raw, bool)
                    or not isinstance(score_raw, (int, float))
                    or not math.isfinite(float(score_raw))
                ):
                    raise CandidateStoreError(
                        f"candidate report has invalid score: {fingerprint}"
                    )
                score = float(score_raw)

                if record is None:
                    records[fingerprint] = CandidateRecord(
                        fingerprint=fingerprint,
                        title=title,
                        route=route,
                        status="new",
                        first_seen=now,
                        last_seen=now,
                        last_evidence_count=evidence_count,
                        last_score=score,
                        evidence_hashes=incoming_hashes[-256:],
                    )
                    continue

                old_hashes = set(record.evidence_hashes)
                fresh_hashes = [
                    value for value in incoming_hashes if value not in old_hashes
                ]
                fresh_observed_at = [
                    observed
                    for digest, observed in incoming_evidence
                    if digest not in old_hashes
                ]
                metadata_changed = any(
                    (
                        record.title != title,
                        record.route != route,
                        record.last_evidence_count != evidence_count,
                        record.last_score != score,
                    )
                )
                if fresh_hashes or metadata_changed:
                    record.last_seen = now
                record.title = title
                record.route = route
                record.last_evidence_count = evidence_count
                record.last_score = score
                if fresh_hashes:
                    record.evidence_hashes = list(
                        dict.fromkeys([*record.evidence_hashes, *incoming_hashes])
                    )[-256:]
                    if (
                        record.status in _REGRESSIBLE_CANDIDATE_STATUSES
                        and any(
                            observed > record.last_action_at
                            for observed in fresh_observed_at
                        )
                    ):
                        record.status = "regressed"
                        record.confirmation = "regressed"
                        record.resolved_at = None
                        regressed.append(fingerprint)
                if (
                    record.status == "resolved"
                    and record.confirmation == "confirmed"
                    and record.resolved_at is not None
                    and generated_at > record.resolved_at
                ):
                    record.confirmation = "pending"
                    record.last_seen = now

            if (
                report["candidate_set_complete"] is True
                and report["source_filter"] is None
            ):
                for fingerprint, record in records.items():
                    if (
                        record.status == "resolved"
                        and fingerprint not in seen
                        and record.confirmation != "confirmed"
                        and record.resolved_at is not None
                        and report_from <= record.resolved_at < report_to
                        and generated_at > record.resolved_at
                    ):
                        record.confirmation = "confirmed"

            after = {
                fingerprint: record.to_dict()
                for fingerprint, record in records.items()
            }
            if after != before:
                self._write_records_unlocked(records)
            self._records = records
            return regressed

    def transition(
        self, fingerprint: str, new_status: CandidateStatus
    ) -> CandidateRecord:
        if new_status not in _VALID_CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status: {new_status!r}")
        with _candidate_store_transaction_lock(self._path):
            records = self._read_records_unlocked()
            record = records.get(fingerprint)
            if record is None:
                raise KeyError(f"unknown candidate fingerprint: {fingerprint!r}")
            now = time.time()
            record.status = new_status
            record.last_action_at = now
            if new_status == "resolved":
                record.resolved_at = now
                record.confirmation = "pending"
            else:
                record.resolved_at = None
                record.confirmation = (
                    "regressed" if new_status == "regressed" else "unconfirmed"
                )
            self._write_records_unlocked(records)
            self._records = records
            return _clone_record(record)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Hermes Trajectory Radar",
        "",
        f"Generated: {report.get('generated_at')}",
        f"Window: {report.get('window', {}).get('days')} days",
        "",
        "## Totals",
        "",
    ]
    totals = report.get("totals", {})
    for key in ("sessions", "messages", "tool_calls", "input_tokens", "output_tokens"):
        lines.append(f"- {key.replace('_', ' ').title()}: {totals.get(key, 0):,}")
    lines.extend(["", "## Action Candidates", ""])
    candidates = report.get("candidates") or []
    if not candidates:
        lines.append("No action candidates found for this window.")
        return "\n".join(lines).rstrip() + "\n"
    for idx, candidate in enumerate(candidates, 1):
        lines.extend(
            [
                f"### {idx}. {candidate['title']}",
                "",
                f"- Route: `{candidate['route']}`",
                f"- Confidence: `{candidate['confidence']}`",
                f"- Score: `{candidate['score']}`",
                f"- Evidence refs: `{candidate['evidence_count']}`",
                f"- Projects: {', '.join(candidate.get('projects') or ['unknown'])}",
                f"- Sources: {_fmt_counts(candidate.get('sources') or {})}",
                "",
                f"**Why now:** {candidate['why_now']}",
                "",
                f"**Cost of ignoring:** {candidate['cost_of_ignoring']}",
                "",
                f"**First 30-minute move:** {candidate['first_move']}",
                "",
                f"**Proof it worked:** {candidate['proof_gate']}",
                "",
                f"**Safety boundary:** {candidate['safety_boundary']}",
                "",
                "Evidence refs:",
            ]
        )
        for ref in candidate.get("evidence_refs", [])[:10]:
            msg = f"/m/{ref['message_id']}" if ref.get("message_id") is not None else ""
            lines.append(f"- `{ref['session_id']}`{msg} — {ref.get('signal') or 'signal'}")
            if ref.get("snippet"):
                lines.append(f"  - snippet: {ref['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_candidates_markdown(records: list[CandidateRecord]) -> str:
    if not records:
        return "No active radar candidates.\n"
    lines = [
        "# Radar Candidates",
        "",
        "| Fingerprint | Status | Route | Score | Evidence | Confirmation |",
        "|---|---|---|---:|---:|---|",
    ]
    for record in records:
        title = f" — {record.title}" if record.title else ""
        lines.append(
            f"| `{record.fingerprint}`{title} | {record.status} | "
            f"{record.route or 'unknown'} | {record.last_score:g} | "
            f"{record.last_evidence_count} | {record.confirmation} |"
        )
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], *, fmt: str, out: str | Path | None) -> str:
    if fmt == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    elif fmt == "markdown":
        rendered = render_markdown(report)
    else:
        raise ValueError(f"Unsupported trajectory radar format: {fmt}")
    if out:
        path = Path(out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return str(path)
    return rendered


def _merge_signal_rows(by_label: dict[str, list[dict[str, Any]]], labels: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in labels:
        rows.extend(by_label.get(label, []))
    return rows


def _dedupe_refs(rows: Iterable[dict[str, Any]]) -> list[EvidenceRef]:
    seen: set[tuple[str, int | None, str]] = set()
    refs: list[EvidenceRef] = []
    for row in rows:
        sid = row.get("session_id") or ""
        mid = row.get("message_id")
        label = row.get("label") or ""
        key = (sid, mid, label)
        if not sid or key in seen:
            continue
        seen.add(key)
        refs.append(
            EvidenceRef(
                session_id=sid,
                message_id=mid,
                signal=label,
                observed_at=float(row.get("observed_at") or 0.0),
                snippet=row.get("snippet"),
            )
        )
    return refs


def _project_label(row: dict[str, Any]) -> str:
    root = row.get("git_repo_root") or row.get("cwd") or "unknown"
    if not root:
        return "unknown"
    path = str(root)
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path


def _looks_hosted(session: dict[str, Any]) -> bool:
    provider = (session.get("billing_provider") or "").lower()
    base_url = (session.get("billing_base_url") or "").lower()
    model = (session.get("model") or "").lower()
    if any(local in provider for local in ("zeus", "local", "llama", "vllm")):
        return False
    if "127.0.0.1" in base_url or "localhost" in base_url:
        return False
    return bool(provider or any(name in model for name in ("gpt", "glm", "claude", "kimi", "openrouter")))


def _safe_snippet(content: str, *, limit: int = 180) -> str:
    text = " ".join(str(content).split())
    if _SECRETISH_RE.search(text):
        return "[redacted: secret-like content]"
    if _PII_RE.search(text):
        text = _PII_RE.sub("[redacted]", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in counts.items())
