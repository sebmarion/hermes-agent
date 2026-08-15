#!/usr/bin/env python3
"""Mine Hermes session data for failure signatures → structured, sanitized rows.

This is the "harvest" step of the autonomous improve loop. It is PURE in its
core (`extract_failures`): given session rows (id, seq, title, body) and a
watermark seq, it returns only NEW failure records. It never writes raw
session bodies anywhere — only structured fields (task_id, title, a scrubbed
task_instructions snippet, failure_signature, before_session_ids, session_seq).

Failure signatures (heuristic, deliberately conservative):
    error    — exception text / "error" markers (FileNotFoundError, pytest
               failures/exit code 1, "boom", ...)
    retry    — "fell back", "retry", "failed ... retry", "pending retry"
    timeout  — "timed out", "timeout", "aborted ... waiting", "refused"
Anything not matching any signature is not a failure for our purposes.

Credential safety: `write_facts`/`write_failures` apply the same
credential-shaped pattern scan as the dataset validator to the output rows
and redact matches with <redacted:LABEL> before writing, so a credential that
slips into a harvested instruction can never persist to disk.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pipeline_state as ps

# Conservative credential patterns shared with the validator (subset).
CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key id"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"\bxo[a-z]+-[A-Za-z0-9\-]{10,}\b"), "slack token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"), "bearer token"),
]

# very coarse failure signature keyword map (kept disjoint from "success" text)
_SIGNATURES = [
    ("error", re.compile(r"\berror\b|traceback|exception|failed|exit code [1-9]|pytest.*fail", re.I)),
    ("retry", re.compile(r"fell back|retry|pending retry", re.I)),
    ("timeout", re.compile(r"timeout|timed out|aborted.*wait|refused", re.I)),
]

TASK_ID_ALPHABET = "0123456789abcdef"


def _scrub(text: str) -> str:
    """Redact credential-shaped substrings in a body before it can be written."""
    out = text
    for pat, label in CRED_PATTERNS:
        out = pat.sub(f"[redacted:{label}]", out)
    return out


def classify_failure(text: str):
    """Return the first matching failure signature, or None."""
    for sig, pat in _SIGNATURES:
        if pat.search(text):
            return sig
    return None


def _slug_task_id(seed: str) -> str:
    """Deterministic task_xxxx from a content seed (no session ids in key)."""
    import hashlib

    return "task_" + hashlib.sha256(seed.encode()).hexdigest()[:8]


def extract_failures(sessions, watermark_seq: int = 0, researcher_id: str = "autoresearch") -> list[dict]:
    """Return sanitized failure records for sessions with seq > watermark_seq.

    Each record:
        {task_id, task_title, task_instructions, failure_signature,
         before_session_ids, session_seq}
    Crucially: NO 'body' key. Records are scrubbed of credential-shaped values.
    """
    out = []
    for s in sessions:
        if not isinstance(s, dict) or "seq" not in s:
            continue  # skip malformed rows rather than explode (F2)
        try:
            seq = int(s["seq"])
        except (TypeError, ValueError):
            continue
        if seq <= watermark_seq:
            continue
        body = s.get("body", "")
        sig = classify_failure(body)
        if sig is None:
            continue
        sid = str(s.get("id", f"session_{seq}"))
        task_inst = _scrub(body)[:2000]
        out.append(
            {
                "task_id": _slug_task_id(sid + body[:120]),
                "task_title": (s.get("title") or "harvested failure")[:200],
                "task_instructions": task_inst,
                "failure_signature": sig,
                "before_session_ids": [sid],
                "session_seq": seq,
                "researcher_id": researcher_id,
            }
        )
    return out


def write_facts(path: Path, records: list[dict]) -> int:
    """Write records as JSONL after scrubbing every string field again (belt + braces)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as fh:
        for r in records:
            scrubbed = {k: (_scrub(v) if isinstance(v, str) else v) for k, v in r.items()}
            fh.write(json.dumps(scrubbed, sort_keys=True) + "\n")
            n += 1
    return n


# alias so the test name matches the implementation
def write_failures(path, records):
    return write_facts(Path(path), records)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions-json", required=True, help="Path to a JSON array of session rows")
    ap.add_argument("--out", required=True, help="Failures JSONL output path")
    ap.add_argument("--state-dir", required=True, help="Where the session watermark lives")
    ap.add_argument("--watermark-key", default="sessions")
    ap.add_argument("--researcher-id", default="zeusresearch")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    sessions_path, out_path, state_dir = Path(args.sessions_json), Path(args.out), Path(args.state_dir)
    if not sessions_path.is_file():
        print(f"error: sessions file not found: {sessions_path}", file=sys.stderr)
        return 2

    try:
        sessions = json.loads(sessions_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: sessions file corrupt: {exc}", file=sys.stderr)
        return 2
    if not isinstance(sessions, list):
        print("error: sessions file must be a JSON array", file=sys.stderr)
        return 2

    watermark = ps.read_watermark(state_dir, args.watermark_key) or 0
    records = extract_failures(sessions, watermark_seq=watermark, researcher_id=args.researcher_id)
    n = write_facts(out_path, records)
    # advance the watermark only to the max seq we actually consumed
    max_seq = max((int(s["seq"]) for s in sessions if isinstance(s, dict) and "seq" in s), default=watermark)
    ps.write_watermark(state_dir, args.watermark_key, max(max_seq, watermark))
    print(f"RESULT: OK ({n} new failures harvested, watermark -> {max(max_seq, watermark)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))