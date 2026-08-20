#!/usr/bin/env python3
"""Deterministic blind-A/B scorecard for the Hermes-skill research pilot.

Usage:
    python score_hermes_skill_run.py --judge-file judges.jsonl --out scorecard.tsv

Rules (fail-closed, never invents a verdict):
  - Zero judge rows           -> exit 1 ("no evidence").
  - ANY malformed row         -> exit 1 (whole run aborted; no partial scoring).
  - Out-of-enum verdict / out-of-range score / bad blind_candidate / wrong
    schema_version            -> each rejected with its line number, then abort.

The pilot treats the judge file as untrusted input: if even one row is
malformed we refuse to emit a scorecard rather than silently scoring a
truncated sample (a partially-poisoned judge file must not look like a clean
result).

Output is byte-deterministic: sorted by task_id then blind_candidate, fixed
column order, fixed float formatting.

Verdicts allowed: better / equal / worse. 'equal' is a real outcome. The script
reports what the judges said; it does not decide promotion — that's the runbook.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ALLOWED_VERDICTS = ("better", "equal", "worse")
SCHEMA_VERSION = 1


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--judge-file", required=True, help="Path to judges.jsonl")
    ap.add_argument("--out", required=True, help="Output TSV path")
    return ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)


def load_judges(path: Path):
    rows, problems = [], []
    text = path.read_text()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append((lineno, f"invalid JSON: {exc}"))
            continue
        if row.get("schema_version") != SCHEMA_VERSION:
            problems.append((lineno, "schema_version must be 1"))
            continue
        verdict = row.get("verdict")
        if verdict not in ALLOWED_VERDICTS:
            problems.append((lineno, f"verdict {verdict!r} outside {ALLOWED_VERDICTS}; rejected (not coerced)"))
            continue
        blind = row.get("blind_candidate")
        if blind not in ("A", "B"):
            problems.append((lineno, f"blind_candidate {blind!r} must be A or B"))
            continue
        score = row.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not (0 <= score <= 1):
            problems.append((lineno, "score must be a number in [0,1]"))
            continue
        rows.append(row)
    return rows, problems


def aggregate(rows):
    """Per blind arm: count + mean score, plus overall verdict tally."""
    by_arm = defaultdict(lambda: {"n": 0, "sum": 0.0, "better": 0, "equal": 0, "worse": 0})
    for r in rows:
        arm = r["blind_candidate"]
        by_arm[arm]["n"] += 1
        by_arm[arm]["sum"] += float(r["score"])
        by_arm[arm][r["verdict"]] += 1
    return by_arm


def render(by_arm):
    out_lines = ["arm\tn\tmean_score\tbetter\tequal\tworse"]
    for arm in sorted(by_arm.keys()):
        d = by_arm[arm]
        mean = (d["sum"] / d["n"]) if d["n"] else 0.0
        out_lines.append(
            f"{arm}\t{d['n']}\t{mean:.4f}\t{d['better']}\t{d['equal']}\t{d['worse']}"
        )
    return "\n".join(out_lines) + "\n"


def main(argv):
    args = parse_args(argv)
    judge_path = Path(args.judge_file)
    out_path = Path(args.out)

    if not judge_path.is_file():
        print(f"error: judge file not found: {judge_path}", file=sys.stderr)
        return 1

    rows, problems = load_judges(judge_path)
    if problems:
        # Fail-closed: any malformed row aborts the whole run. We never emit a
        # scorecard from a truncated sample — a partially-poisoned judge file
        # must not look like a clean result.
        for lineno, msg in problems:
            print(f"[line {lineno}] rejected: {msg}", file=sys.stderr)
        print(
            f"RESULT: ABORT — {len(problems)} malformed judge row(s); "
            "refusing to score a truncated sample.",
            file=sys.stderr,
        )
        return 1

    if not rows:
        print("RESULT: ABORT — no valid judge rows; refusing to fabricate a verdict.", file=sys.stderr)
        return 1

    by_arm = aggregate(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(by_arm))
    print(f"RESULT: OK ({len(rows)} judge rows scored -> {out_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
