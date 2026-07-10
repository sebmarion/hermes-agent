"""Local session-to-action radar for Hermes.

This module reads the local Hermes ``state.db`` and turns repeated session
friction into privacy-preserving action candidates.  It intentionally emits
session/message references and short machine labels by default, not raw
transcript excerpts.

A lightweight local candidate lifecycle is layered on top so the radar is
not a passive report: candidates can be accepted, deferred, resolved,
ignored, and automatically regressed when fresh evidence re-surfaces.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

Route = Literal["FIX", "CONFIG", "SKILL_PATCH", "CRON", "DECIDE", "IGNORE"]
Confidence = Literal["high", "medium", "low"]
CandidateStatus = Literal["new", "accepted", "deferred", "resolved", "ignored", "regressed"]

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
        cutoff = time.time() - (days * 86400)
        sessions = self._sessions(cutoff, source)
        signals = self._signals(cutoff, source, include_snippets=include_snippets)
        candidates = self._build_candidates(sessions, signals)
        candidates.sort(key=lambda item: (-item.score, item.title))
        total_candidate_count = len(candidates)
        if limit > 0:
            candidates = candidates[:limit]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window": {"days": days, "from_epoch": cutoff, "to_epoch": time.time()},
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

    def _sessions(self, cutoff: float, source: str | None) -> list[dict[str, Any]]:
        cols = (
            "id, source, model, started_at, ended_at, message_count, tool_call_count, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cwd, "
            "git_repo_root, billing_provider, billing_base_url, title"
        )
        if source:
            cursor = self._conn.execute(
                f"SELECT {cols} FROM sessions WHERE started_at >= ? AND source = ?",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                f"SELECT {cols} FROM sessions WHERE started_at >= ?",
                (cutoff,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def _signals(
        self,
        cutoff: float,
        source: str | None,
        *,
        include_snippets: bool,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [cutoff]
        source_clause = ""
        if source:
            source_clause = " AND s.source = ?"
            params.append(source)
        cursor = self._conn.execute(
            f"""
            SELECT m.id AS message_id, m.session_id, m.role, m.content,
                   m.tool_name, m.tool_calls, s.source, s.model, s.cwd, s.git_repo_root,
                   s.billing_provider
              FROM messages m
              JOIN sessions s ON s.id = m.session_id
             WHERE s.started_at >= ?{source_clause}
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
            # evidence. Keeping the oldest 20 would make lifecycle regression
            # stop working once a candidate accumulated more than 20 refs.
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


# ---------------------------------------------------------------------------
# Candidate lifecycle — local, profile-aware, no schema migrations
# ---------------------------------------------------------------------------

_VALID_STATUS: tuple[str, ...] = ("new", "accepted", "deferred", "resolved", "ignored", "regressed")
# When a candidate is in one of these statuses and fresh radar evidence for the
# same fingerprint re-surfaces, it regresses to ``regressed`` (or stays there).
_REGRESSIBLE_STATUS: tuple[str, ...] = ("accepted", "deferred", "resolved")


@dataclass
class CandidateRecord:
    """A persisted candidate lifecycle record.

    The fingerprint is the radar candidate ``id`` (a stable slug like
    ``done-means-proven-gatekeeper``).  This means a candidate is tracked
    across runs without coupling to a specific evidence row; only its
    *fingerprint* matters.
    """

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
    note: str = ""
    confirmation: str = "unconfirmed"
    evidence_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateStore:
    """Profile-aware local JSON store for candidate lifecycle state.

    Lives at ``<hermes_home>/radar_candidates.json``.  It is deliberately
    separate from ``state.db`` so it requires **no schema migration** and
    carries no PII — only fingerprints, statuses, and timestamps.
    """

    def __init__(self, path: Path | str | None = None):
        if path is not None:
            self._path = Path(path)
        else:
            from hermes_constants import get_hermes_home

            self._path = get_hermes_home() / "radar_candidates.json"
        self._records: dict[str, CandidateRecord] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            self._records = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._records = {}
            return
        records: dict[str, CandidateRecord] = {}
        for entry in raw if isinstance(raw, list) else []:
            try:
                records[entry["fingerprint"]] = CandidateRecord(
                    fingerprint=entry["fingerprint"],
                    title=entry.get("title", ""),
                    route=entry.get("route", ""),
                    status=entry.get("status", "new"),
                    first_seen=entry.get("first_seen", 0.0),
                    last_seen=entry.get("last_seen", 0.0),
                    resolved_at=entry.get("resolved_at"),
                    last_action_at=entry.get("last_action_at", 0.0),
                    last_evidence_count=entry.get("last_evidence_count", 0),
                    last_score=entry.get("last_score", 0.0),
                    note=entry.get("note", ""),
                    confirmation=entry.get("confirmation", "unconfirmed"),
                    evidence_hashes=[str(value) for value in entry.get("evidence_hashes", []) if value],
                )
            except (KeyError, TypeError):
                continue
        self._records = records

    def save(self) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [rec.to_dict() for rec in sorted(self._records.values(), key=lambda r: r.fingerprint)]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self._path)
        return self._path

    # -- queries ------------------------------------------------------------

    def get(self, fingerprint: str) -> CandidateRecord | None:
        return self._records.get(fingerprint)

    def list(
        self,
        *,
        status: str | None = None,
    ) -> list[CandidateRecord]:
        records = list(self._records.values())
        if status:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: (-r.last_seen, r.fingerprint))
        return records

    # -- mutations ----------------------------------------------------------

    def _touch(self, fingerprint: str, candidate: dict[str, Any] | None = None) -> CandidateRecord:
        now = time.time()
        rec = self._records.get(fingerprint)
        if rec is None:
            rec = CandidateRecord(
                fingerprint=fingerprint,
                title=(candidate or {}).get("title", ""),
                route=(candidate or {}).get("route", ""),
                status="new",
                first_seen=now,
                last_seen=now,
                last_action_at=now,
                last_evidence_count=(candidate or {}).get("evidence_count", 0),
                last_score=(candidate or {}).get("score", 0.0),
                evidence_hashes=_candidate_evidence_hashes(candidate or {}),
            )
            self._records[fingerprint] = rec
        else:
            rec.last_seen = now
            if candidate:
                rec.title = candidate.get("title", rec.title)
                rec.route = candidate.get("route", rec.route)
                rec.last_evidence_count = candidate.get("evidence_count", rec.last_evidence_count)
                rec.last_score = candidate.get("score", rec.last_score)
        return rec

    def sync_from_report(self, report: dict[str, Any]) -> list[str]:
        """Sync candidates and return fingerprints with genuinely new evidence.

        Evidence refs are hashed before persistence. Re-running an unchanged
        report cannot regress a candidate; a new session/message/signal tuple
        can. Absence confirms a resolution only for an unfiltered, untruncated
        report, avoiding false confirmation from partial views.
        """
        regressed: list[str] = []
        seen_fps: set[str] = set()
        for candidate in report.get("candidates", []):
            fp = candidate.get("id") or ""
            if not fp:
                continue
            seen_fps.add(fp)
            previous = self._records.get(fp)
            previous_hashes = set(previous.evidence_hashes) if previous else set()
            incoming_hashes = _candidate_evidence_hashes(candidate)
            fresh_hashes = set(incoming_hashes) - previous_hashes
            rec = self._touch(fp, candidate)
            if previous is not None and rec.status in _REGRESSIBLE_STATUS and fresh_hashes:
                rec.status = "regressed"
                rec.confirmation = "regressed"
                rec.resolved_at = None
                regressed.append(fp)
            rec.evidence_hashes = list(
                dict.fromkeys([*rec.evidence_hashes, *incoming_hashes])
            )[-256:]

        if report.get("candidate_set_complete") and not report.get("source_filter"):
            for fp, rec in self._records.items():
                if rec.status == "resolved" and fp not in seen_fps:
                    rec.confirmation = "confirmed"
        self.save()
        return regressed

    def transition(
        self,
        fingerprint: str,
        new_status: CandidateStatus,
        *,
        note: str = "",
    ) -> CandidateRecord:
        if new_status not in _VALID_STATUS:
            raise ValueError(f"invalid candidate status: {new_status!r}")
        rec = self._records.get(fingerprint)
        if rec is None:
            raise KeyError(f"unknown candidate fingerprint: {fingerprint!r}")
        now = time.time()
        rec.status = new_status
        rec.last_action_at = now
        if note:
            rec.note = note
        if new_status == "resolved":
            rec.resolved_at = now
            rec.confirmation = "pending"
        else:
            rec.resolved_at = None
            rec.confirmation = "unconfirmed"
        self.save()
        return rec


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


def render_candidates_markdown(records: list[CandidateRecord], *, show_resolved: bool = False) -> str:
    """Render candidate lifecycle records as a compact markdown table/list."""
    active = [r for r in records if r.status != "ignored"]
    if not show_resolved:
        active = [r for r in active if r.status != "resolved"]
    if not active:
        return "No active radar candidates.\n"
    lines = ["# Radar Candidates", ""]
    lines.append("| # | Fingerprint | Status | Route | Score | Evidence | Last Seen |")
    lines.append("|---|-------------|--------|-------|-------|----------|-----------|")
    for idx, rec in enumerate(active, 1):
        title = rec.title or rec.fingerprint
        when = (
            datetime.fromtimestamp(rec.last_seen, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if rec.last_seen
            else "—"
        )
        lines.append(
            f"| {idx} | `{rec.fingerprint}` ({title}) | {rec.status} | {rec.route} | "
            f"{rec.last_score} | {rec.last_evidence_count} | {when} |"
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
                snippet=row.get("snippet"),
            )
        )
    return refs


def _candidate_evidence_hashes(candidate: dict[str, Any]) -> list[str]:
    hashes: list[str] = []
    for ref in candidate.get("evidence_refs") or []:
        raw = "\x1f".join(
            (
                str(ref.get("session_id") or ""),
                str(ref.get("message_id") if ref.get("message_id") is not None else ""),
                str(ref.get("signal") or ""),
            )
        )
        if raw.strip("\x1f"):
            hashes.append(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24])
    return list(dict.fromkeys(hashes))


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
