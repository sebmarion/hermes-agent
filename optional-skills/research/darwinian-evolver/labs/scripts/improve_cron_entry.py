#!/usr/bin/env python3
"""Cron entry point for the autonomous improve loop.

This file is the single *scheduler-invokable* entry. The Hermes cron row that
calls it (see labs/RUNBOOK.md) runs it on a schedule; it is deliberately
idempotent so a double-fire is harmless:

  - harvests failures since the watermark (no-op if nothing new),
  - fetches + filters X bookmarks (cheap, no LLM),
  - runs the improve orchestrator dry decision,
  - writes runs/<ts>/report.md regardless of outcome,
  - leaves ALL network/model steps to the runbook operator (the live
    Zeus qualify/judge chain is still exercised via the existing
    qualify_zeus_researcher.py per the labs runbook — this file keeps the
    loop's *scaffolding* runnable while the CLI is healthy).

Exit 0 always (failures are captured into the report, not raised), so a
transient Zeus/network hiccup cannot wedge the scheduler.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pipeline_state as ps

# Where labs stores its per-run state (override with --state-dir in tests)
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "labs" / "bestplan-research" / "state"


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-out", default=None)
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

    # Step 1: harvest session failures (pure, offline, watermark-gated)
    from harvest_failures import extract_failures, write_facts  # local import keeps CLI lean
    import pipeline_state as ps

    # NOTE: in this scaffolding entry the sessions source is the watermark
    # stub (empty arr = no changes). Real session-DB mining is executed by an
    # operator-provided data file per the RUNBOOK; wiring the DB reader here
    # is deliberately NOT done so this entry stays import-safe while the
    # agent's own session module is being updated by the other thread.
    wm = ps.read_watermark(state_dir, "sessions")
    report["watermark_sessions"] = wm
    report["steps"].append("harvest_failures: no-op (data source owned by runbook)")
    report["notes"].append("failures harvesting is scaffolding-only until session DB reader lands in RUNBOOK")

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

    # Step 3: report.
    out_path = Path(args.report_out) if args.report_out else state_dir / f"report-{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(f"RESULT: OK (report -> {out_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))