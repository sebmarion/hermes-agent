#!/usr/bin/env python3
"""Harvest Seb's X bookmarks (read-only) → actionable optimization ideas.

The improve loop takes ideas from X bookmarks, but most saved posts are just
reading material and would waste a Zeus call. So this module applies a CHEAP
keyword pre-filter BEFORE any LLM spend; only posts that look Hermes-actionable
(a repo/tool/skill/hook/plugin/fix reference) proceed to the fixer pipeline.
Everything else buckets to a digest sidecar — read later, never sent to the
proposer.

Core is PURE (`filter_actionable`, `is_actionable`, `build_sidecar`) so tests
run offline on fixture dicts shaped like xurl bookmark records:
    {id, full_text, url}

Safety: credential-shaped substrings are redacted before a bookmark can be
written to disk. The CLI runs only `xurl bookmarks` as a read subprocess and
never reads or writes ~/.xurl credentials.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Cheap actionability keywords: present => likely Hermes-actionable.
# Deliberately narrow + lowercase so noise rarely passes the gate.
_ACTION_KEYWORDS = (
    "hermes", "agent", "skill", "cli", "tool", "repo", "github",
    "llm", "prompt", "automation", "workflow", "plugin", "hook",
    "autonomous", "model", "api", "pipeline", "terminal", "n8n",
)

# Credential patterns (subset shared with validator).
CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
]


def _scrub(text: str) -> str:
    out = text
    for pat, label in CRED_PATTERNS:
        out = pat.sub(f"[redacted:{label}]", out)
    return out


def is_actionable(bookmark: dict) -> bool:
    """Cheap keyword gate: does this post look Hermes-actionable?

    Runs on the raw text only, no model needed. Conservative on purpose —
    false negatives go to the digest (harmless); we want few false positives
    (which would burn a Zeus propose call on noise)."""
    if not isinstance(bookmark, dict):
        return False
    text = (bookmark.get("full_text") or "").lower()
    url = (bookmark.get("url") or "").lower()
    haystack = f"{text} {url}"
    return any(kw in haystack for kw in _ACTION_KEYWORDS)


def filter_actionable(bookmarks: list[dict]) -> list[dict]:
    seen = set()

    def keep(b):
        bid = b.get("id")
        if bid in seen:
            return False
        seen.add(bid)
        return is_actionable(b)

    return [b for b in bookmarks if keep(b)]


def partition(actionable, bookmarks):
    """Split into (actionable_kept, digest). Kept entries are deduped;
    digest holds everything (both noise and duplicates-not-yet-processed)."""
    kept, digest, seen = [], [], set()
    for b in bookmarks:
        bid = b.get("id")
        digest.append({"id": bid, "full_text": _scrub(str(b.get("full_text", "")))[:400],
                       "url": b.get("url", "")})
        if bid in seen:
            continue
        seen.add(bid)
        if actionable(b):
            kept.append(b)
    return kept, digest


def build_sidecar(bookmarks: list[dict]) -> list[dict]:
    """Build sanitized sidecar records {bookmark_id, text_snippet, url, extracted_idea}.
    'extracted_idea' is a light one-line classification stub for now; the actual
    idea extraction happens later at the proposer step."""
    out = []
    for b in bookmarks:
        text = _scrub(str(b.get("full_text", "")))
        out.append(
            {
                "bookmark_id": str(b.get("id")),
                "text_snippet": text[:300],
                "url": str(b.get("url", "")),
                "extracted_idea": text[:120],
            }
        )
    return out


def write_sidecar(path: Path, records: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
            n += 1
    return n


def fetch_bookmarks(n: int = 50) -> list[dict]:
    """Read-only fetch via `xurl bookmarks -n N --json`. Never touches ~/.xurl.
    Returns [] on any nonzero/parse failure (fail-closed to no-op, caller logs)."""
    try:
        proc = subprocess.run(
            ["xurl", "bookmarks", "-n", str(n), "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    # some versions nest under .bookmarks / .data
    for key in ("bookmarks", "data"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    return []


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="Sidecar JSONL output path")
    ap.add_argument("--digest-out", default=None, help="Optional digest JSONL output for noise")
    ap.add_argument("-n", type=int, default=50, help="Number of bookmarks to review")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    bms = fetch_bookmarks(args.n)
    if not bms:
        print("RESULT: SKIPPED — no bookmarks fetched (xurl unavailable/offline). No state change.")
        return 0

    kept, digest = partition(actionable=is_actionable, bookmarks=bms)
    recs = build_sidecar(kept)
    n = write_sidecar(Path(args.out), recs)
    if args.digest_out:
        write_sidecar(Path(args.digest_out), digest)

    print(f"RESULT: OK ({len(bms)} reviewed, {n} actionable -> {args.out}, "
          f"{len(digest) - len(kept)} to digest)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))