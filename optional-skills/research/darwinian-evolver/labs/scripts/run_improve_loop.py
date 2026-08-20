#!/usr/bin/env python3
"""Autonomous improve-loop orchestrator.

Sequences the improve pipeline deterministically:
    harvest_failures -> validate -> (live) qualify_zeus -> Zeus propose ->
    blind judge -> scorecard -> reality-replay/outcome-eval -> apply decision.

The APPLY-DECISION policy lives in `decide()` — pure and deterministic, so the
rulebook is pinned by tests without any live model or network dependency.

Decision policy (fail-closed, autonomous):
  1. If ANY gate fails  -> action="block"  (nothing applied; reason names blocker).
  2. Else if change_class is a CORE path (config.yaml / provider / cron /
     auth / any non-skill target) -> action="park" (reported to Seb via
     Telegram + PR-style patch; NEVER auto-applied to live).
  3. Else (skill-path + all green) -> action="apply".
Core-path changes are never mutated by this loop — that keeps autonomy while
preserving the floor that only revertible skill edits auto-apply.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

CORE_CLASSES = ("config.yaml", "provider", "providers", "cron", "auth", "memory", "plugin")

DEFAULT_THRESHOLD = 0.7


def _is_core(cls: str) -> bool:
    c = cls.lower()
    return any(cc in c for cc in CORE_CLASSES)


@dataclass
class GateResult:
    scorecard_ok: bool = False
    scorecard_mean: float = 0.0
    threshold: float = DEFAULT_THRESHOLD
    replay_passes: bool = False
    has_secrets: bool = False
    change_class: str = "skill"


def decide(gate: dict) -> dict:
    """Return {action, reason}. action in {'apply','block','park'}."""
    g = GateResult(**{k: v for k, v in gate.items() if k in GateResult.__dataclass_fields__})
    blockers = []
    if not g.scorecard_ok:
        blockers.append("scorecard")
    if not g.replay_passes:
        blockers.append("reality-replay")
    if g.has_secrets:
        blockers.append("secret present in diff")
    if g.scorecard_mean < g.threshold:
        blockers.append(f"score {g.scorecard_mean:.2f} below threshold {g.threshold}")

    if blockers:
        return {"action": "block", "reason": "gate failed: " + ", ".join(blockers)}

    if _is_core(g.change_class):
        return {
            "action": "park",
            "reason": f"core-path class ({g.change_class}) never auto-applied; reported instead",
        }

    return {"action": "apply", "reason": f"all gates passed (mean {g.scorecard_mean:.2f}, class={g.change_class})"}


# ---------------------------------------------------------------------------
# run_once: one idempotent pass over harvested evidence
# ---------------------------------------------------------------------------

def run_once(failures=None, bookmarks=None, gates=None, state_dir=None) -> dict:
    failures = list(failures or [])
    bookmarks = list(bookmarks or [])
    report = {
        "actions": [],
        "applied": [],
        "skipped": [],
        "parked": [],
        "blocked": [],
        "n_failures": len(failures),
        "n_bookmarks": len(bookmarks),
    }
    # For each evidence item with a valid gate result, classify it.
    n_applied = 0
    for failure in failures:
        tid = failure.get("task_id", "?") if isinstance(failure, dict) else "?"
        if gates and tid in gates:
            verdict = decide(gates[tid])
        else:
            verdict = {"action": "skip", "reason": "no gate produced"}
        report["actions"].append({"target": tid, **verdict})
        act = verdict.get("action", "block")
        report[act].append(tid)
        if act == "apply":
            n_applied += 1

    # In test mode we do not actually mutate ~/.hermes; the 'applied' list here
    # means "approved for staging". The apply adapter (B5) performs the real,
    # reversible copy with manifest backup.
    report["summary"] = f"{n_applied} approved-for-staging of {len(report['actions'])} reviewed"
    return report


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--failures-jsonl", default=None, help="Path to harvested failures JSONL")
    ap.add_argument("--report-out", default=None, help="Optional report.json output path")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the decision logic only; never call Zeus/judge or stage anything.",
    )
    args = ap.parse_args(argv[1:] if argv and not argv[0].startswith("-") else argv)

    failures = []
    if args.failures_jsonl:
        p = Path(args.failures_jsonl)
        if not p.is_file():
            print(f"error: failures file not found: {p}", file=sys.stderr)
            return 2
        for line in p.read_text().splitlines():
            if line.strip():
                failures.append(json.loads(line))

    # Without --dry-run we would run the full live chain; for now, in CLI mode
    # we require --dry-run OR a precomputed gates file. Live network wiring is
    # owned by the runbook/operator and guarded by the existing qualifier.
    report = run_once(failures=failures, bookmarks=[], gates=None)
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))