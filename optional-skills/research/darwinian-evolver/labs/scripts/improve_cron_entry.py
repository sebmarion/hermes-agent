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

After harvesting, the bounded Zeus -> independent judge -> score -> apply
chain runs for each new failure. Every candidate is append-only and is
applied atomically with a backup; malformed model output halts that
candidate without mutating the live skill.

Transient network or state failures are captured into the report and surfaced
as a non-zero result.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import harvest_failures as hf
from live_pipeline import run_live_chain
import pipeline_state as ps
import apply_skill_candidate
import propose_zeus_candidate
import promote_skill

# Where labs stores its per-run state (override with --state-dir in tests)
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "labs" / "bestplan-research" / "state"
MAX_CANDIDATES_PER_RUN = 3
_DEFAULT_SKILL_LINK = Path.home() / ".hermes" / "skills" / "software-development" / "bestplan" / "SKILL.md"
DEFAULT_SKILL_PATH = _DEFAULT_SKILL_LINK.resolve()
DEFAULT_LIVE_SKILLS = DEFAULT_SKILL_PATH.parents[2]


def _load_pending(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("pending failure row must be an object")
        rows.append(row)
    return rows


def _merge_failures(pending: list[dict], new: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for row in [*pending, *new]:
        key = str(row.get("task_id") or row.get("session_seq") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _verify_skill_for_promotion(skill_path: Path, session_id: str) -> dict:
    """Run the skill validator and the real OCR gate before Git promotion."""
    validator = skill_path.parent / "scripts" / "validate_bestplan.py"
    if not validator.is_file():
        return {"status": "failed", "reason": f"validator missing: {validator}"}
    completed = subprocess.run(
        [sys.executable, str(validator)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "status": "failed",
            "reason": "BestPlan validator failed",
            "stdout": completed.stdout[-1000:],
            "stderr": completed.stderr[-1000:],
        }

    plugin_path = Path(__file__).resolve().parents[5] / "plugins" / "hermes-bestplan" / "bestplan_ocr.py"
    spec = importlib.util.spec_from_file_location("hermes_bestplan_ocr_promotion", plugin_path)
    if spec is None or spec.loader is None:
        return {"status": "failed", "reason": f"OCR plugin could not load: {plugin_path}"}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = module.verify_ocr_for_turn(session_id=session_id, changed_paths=[str(skill_path)])
    if receipt.get("status") != "passed":
        return {"status": "failed", "reason": "OCR gate failed", "ocr": receipt}
    return {
        "status": "passed",
        "validator": "passed",
        "ocr": receipt,
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-out", default=None)
    ap.add_argument("--db-path", type=Path, default=None)
    ap.add_argument("--live-skills", type=Path, default=DEFAULT_LIVE_SKILLS)
    ap.add_argument(
        "--skill-path",
        type=Path,
        default=DEFAULT_SKILL_PATH,
    )
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
        "n_failures_pending": 0,
        "n_bookmarks_actionable": 0,
        "halted": False,
        "notes": [],
    }
    pending_path = state_dir / "pending_failures.jsonl"
    try:
        pending = _load_pending(pending_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report["notes"].append(f"pending failure queue unreadable: {exc}")
        report["ok"] = False
        report["halted"] = True
        pending = []

    # Step 1: harvest session failures from the canonical Hermes state DB.
    wm = ps.read_watermark(state_dir, "sessions") or 0
    report["watermark_sessions"] = wm
    try:
        sessions = hf.load_hermes_sessions(args.db_path)
        new_failures = hf.extract_failures(sessions, watermark_seq=wm)
        failures = _merge_failures(pending, new_failures)
        failures_path = state_dir / "failures.jsonl"
        hf.write_facts(failures_path, new_failures)
        max_seq = max((int(row["seq"]) for row in sessions), default=wm)
        ps.write_watermark(state_dir, "sessions", max(max_seq, wm))
        report["watermark_sessions"] = max(max_seq, wm)
        report["n_failures_new"] = len(new_failures)
        report["n_failures_pending"] = len(failures)
        report["steps"].append(
            f"harvest_failures: {len(sessions)} completed sessions, "
            f"{len(new_failures)} new failures, {len(failures)} queued"
        )
    except Exception as exc:  # noqa: BLE001 - state failures must halt closed
        report["steps"].append("harvest_failures: failed")
        report["notes"].append(f"session DB harvest failed: {exc}")
        report["ok"] = False
        report["halted"] = True

    # Step 2: bounded live proposal -> independent judge -> score -> apply.
    if report["ok"] and failures:
        try:
            selected_failures = failures[:MAX_CANDIDATES_PER_RUN]
            def proposer(prompt: str) -> str:
                return propose_zeus_candidate.call_zeus(
                    os.environ.get("ZEUS_BASE_URL", "http://100.86.155.23:8080/v1"),
                    os.environ.get("ZEUS_API_KEY", "local-no-auth-needed"),
                    os.environ.get("ZEUS_MODEL", "qwen3.8-27b"),
                    prompt,
                )

            def judge(prompt: str) -> str:
                hermes = shutil.which("hermes")
                if not hermes:
                    fallback = Path.home() / ".local" / "bin" / "hermes"
                    hermes = str(fallback) if fallback.is_file() else None
                if not hermes:
                    raise RuntimeError("Hermes CLI unavailable for non-Zeus judge")
                completed = subprocess.run(
                    [
                        hermes,
                        "-z",
                        prompt,
                        "--provider",
                        "openai-codex",
                        "--model",
                        "gpt-5.6-sol",
                        "--reasoning",
                        "low",
                        "--safe-mode",
                        "--ignore-rules",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if completed.returncode != 0 or not completed.stdout.strip():
                    raise RuntimeError(f"non-Zeus judge failed with exit {completed.returncode}")
                return completed.stdout.strip()

            chain = run_live_chain(
                failures=selected_failures,
                state_dir=state_dir,
                live_skills=args.live_skills,
                skill_path=args.skill_path,
                proposer=proposer,
                judge=judge,
                applier=apply_skill_candidate.apply,
                run_id=ts,
            )
            report["live_chain"] = chain
            report["steps"].append(chain.get("summary", "live_chain: completed"))
            if not chain.get("ok"):
                report["ok"] = False
                report["halted"] = True
                report["notes"].extend(chain.get("notes", []))
            processed_ids = {
                str(task_id)
                for task_id in [*chain.get("applied", []), *chain.get("blocked", [])]
            }
            if chain.get("ok") and not chain.get("blocked"):
                processed_ids.update(str(row.get("task_id")) for row in selected_failures)
            pending = [
                row for row in failures if str(row.get("task_id")) not in processed_ids
            ]
            hf.write_facts(pending_path, pending)
            report["n_failures_pending"] = len(pending)
            if pending:
                report["steps"].append(f"pending_failures: {len(pending)} remain queued")
            if chain.get("applied"):
                try:
                    repo = promote_skill.repository_root(args.skill_path)
                    target_rel = promote_skill.relative_path(repo, args.skill_path)
                    promotion = promote_skill.promote(
                        repo=repo,
                        changed_paths=[target_rel],
                        verify=lambda paths: _verify_skill_for_promotion(
                            args.skill_path, f"improve-promotion-{ts}"
                        ),
                    )
                    report["promotion"] = promotion
                    report["steps"].append(
                        f"promotion: pushed {promotion['commit'][:12]} to "
                        f"{promotion['remote']}/{promotion['branch']}"
                    )
                except Exception as exc:  # noqa: BLE001 - promotion is fail-closed
                    report["notes"].append(f"skill promotion failed: {exc}")
                    report["ok"] = False
                    report["halted"] = True
        except Exception as exc:  # noqa: BLE001 - model/runtime failure is fail-closed
            report["notes"].append(f"live chain failed: {exc}")
            report["ok"] = False
            report["halted"] = True
            try:
                hf.write_facts(pending_path, failures)
                report["n_failures_pending"] = len(failures)
            except (OSError, TypeError, ValueError) as queue_exc:
                report["notes"].append(f"pending failure queue could not be saved: {queue_exc}")

    # Step 3: bookmarks (read-only xurl, cheap filter).
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
    if report["ok"]:
        print(f"RESULT: OK (report -> {out_path})")
        return 0
    print(f"RESULT: HALT (report -> {out_path})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))