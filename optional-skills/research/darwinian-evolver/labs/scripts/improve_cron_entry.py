#!/usr/bin/env python3
"""Cron preflight for the improve loop.

This file is the single *scheduler-invokable* entry. The Hermes cron row that
calls it (see labs/RUNBOOK.md) runs it on a schedule; it is deliberately
idempotent so a double-fire is harmless:

  - reads completed sessions from Hermes' canonical state.db,
  - extracts new failures and advances the message watermark only after a
    successful read/write,
  - fetches + filters X bookmarks (cheap, no LLM),
  - writes a report describing the preflight result.

The live Zeus/judge/apply chain is not wired here. After harvesting, the entry
therefore still halts non-zero and never claims a successful improvement run.

Transient network or state failures are captured into the report and surfaced
as a non-zero result.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import harvest_failures as hf
import pipeline_state as ps

# Where labs stores its per-run state (override with --state-dir in tests)
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "labs" / "bestplan-research" / "state"


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-out", default=None)
    ap.add_argument("--db-path", type=Path, default=None)
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report = {
        "run_ts": ts,
        "ok": True,
        "steps": [],
        "watermark_sessions": None,
        "watermark_bookmarks": None,
        "n_failures_new": 0,
        "n_bookmarks_actionable": 0,
        "halted": False,
        "notes": [],
    }

    # Step 1: harvest session failures from the canonical Hermes state DB.
    wm = ps.read_watermark(state_dir, "sessions") or 0
    report["watermark_sessions"] = wm
    try:
        sessions = hf.load_hermes_sessions(args.db_path)
        failures = hf.extract_failures(sessions, watermark_seq=wm)
        failures_path = state_dir / "failures.jsonl"
        hf.write_facts(failures_path, failures)
        max_seq = max((int(row["seq"]) for row in sessions), default=wm)
        ps.write_watermark(state_dir, "sessions", max(max_seq, wm))
        report["watermark_sessions"] = max(max_seq, wm)
        report["n_failures_new"] = len(failures)
        report["steps"].append(
            f"harvest_failures: {len(sessions)} completed sessions, "
            f"{len(failures)} new failures"
        )
    except Exception as exc:  # noqa: BLE001 - state failures must halt closed
        report["steps"].append("harvest_failures: failed")
        report["notes"].append(f"session DB harvest failed: {exc}")
        report["ok"] = False
        report["halted"] = True

    # The live proposal/judge/apply chain remains intentionally fail-closed.
    report["notes"].append("live Zeus/judge/apply chain is not wired; no candidate may be applied")
    report["ok"] = False
    report["halted"] = True

    # Step 2: bookmarks (read-only xurl, cheap filter).
    try:
        import harvest_x_bookmarks as hx

        bms = hx.fetch_bookmarks(10)
        actionable = hx.filter_actionable(bms)
        report["n_bookmarks_actionable"] = len(actionable)
        report["steps"].append(f"harvest_x: {len(bms)} pulled, {len(actionable)} actionable")
    except Exception as exc:  # noqa: BLE001 - network/tool availability must never wedge cron
        report["notes"].append(f"bookmarks harvest skipped: {exc}")
        report["steps"].append("harvest_x: skipped")
        report["ok"] = False
        report["halted"] = True

    # Step 3: report.
    out_path = Path(args.report_out) if args.report_out else state_dir / f"report-{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    if report["ok"]:
        print(f"RESULT: OK (report -> {out_path})")
        return 0
    print(f"RESULT: HALT (report -> {out_path})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))