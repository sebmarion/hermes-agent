#!/usr/bin/env python3
"""Longitudinal net-positive reporting from the change ledger.

Every applied improvement appends a row to state/change-ledger.jsonl:
    {ts, class, target, gates_passed, scorecard_mean, outcome_delta,
     rollback_sha, source_failure_ids, failure_signature, action}

This module aggregates that ledger into a weekly "is it actually helping?"
report: for each failure signature we show how many changes were applied and
the average measured outcome delta (how much recurrence dropped). A positive
mean across signatures is the auditable net-positive signal — distinct from a
one-off rubric score because it's accumulated over real, time-stamped results.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_ledger(path: Path):
    """Yield JSON rows from the ledger file; skip-but-count corrupt lines."""
    entries = []
    if not path.is_file():
        return entries
    bad = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            entries.append(row)
        else:
            bad += 1
    if bad:
        raise ValueError(f"{bad} malformed ledger line(s); refusing to aggregate partial evidence")
    return entries


def aggregate(entries) -> dict:
    """Summarize by failure_signature; empty input -> clean empty summary."""
    total = 0
    by_sig = defaultdict(list)
    for e in entries:
        sig = e.get("failure_signature") or e.get("class") or "unlabeled"
        by_sig[sig].append(e)
        if e.get("action", "apply") == "apply":
            total += 1

    out_sigs = {}
    for sig, es in sorted(by_sig.items()):
        n = len(es)
        deltas = [float(x["outcome_delta"]) for x in es if isinstance(x.get("outcome_delta"), (int, float))]
        mean = (sum(deltas) / len(deltas)) if deltas else 0.0
        out_sigs[sig] = {"n": n, "avg_outcome_delta": round(mean, 4), "deltas_count": len(deltas)}
    return {"n_applied": total, "by_signature": out_sigs}


def render_report(agg) -> str:
    """Render the aggregator into markdown suitable for report.md + Telegram."""
    lines = ["# Weekly net-positive report\n"]
    if agg["n_applied"] == 0 and not agg["by_signature"]:
        lines.append(
            "_No changes were auto-applied this week._\n\nThe system ran autonomously "
            "with nothing judged worth staging; this is a valid outcome, not an error.\n"
        )
        return "\n".join(lines)

    means = [v["avg_outcome_delta"] for v in agg["by_signature"].values()]
    overall = (sum(means) / len(means)) if means else 0.0
    verdict = "net-positive" if overall > 0 else ("flat" if abs(overall) < 1e-9 else "net-negative")
    lines.append(f"**Verdict: {verdict}** (mean outcome delta = {overall:.3f})\n")
    lines.append(f"Changes applied this period: **{agg['n_applied']}**\n")
    lines.append("| failure_signature | changes | avg_outcome_delta |")
    lines.append("|---|---|---|")
    for sig, info in agg["by_signature"].items():
        lines.append(f"| {sig} | {info['n']} | {info['avg_outcome_delta']:.3f} |")
    lines.append("\n_A positive outcome delta means the failure signature recurred less after the fix._")
    return "\n".join(lines)


def write_weekly_report(out_path: Path, entries) -> int:
    agg = aggregate(entries)
    md = render_report(agg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    return agg["n_applied"]


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", required=True, help="Path to state/change-ledger.jsonl")
    ap.add_argument("--out", required=True, help="Weekly report markdown output path")
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    try:
        entries = load_ledger(Path(args.ledger))
        n = write_weekly_report(Path(args.out), entries)
    except (OSError, ValueError) as exc:
        print(f"RESULT: HALT — {exc}", file=sys.stderr)
        return 1
    print(f"RESULT: OK ({n} applied changes aggregated -> {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))