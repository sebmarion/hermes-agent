#!/usr/bin/env python3
"""Bounded live proposal -> blind judge -> score -> apply orchestration.

This module is deliberately small and dependency-injected. The cron entry uses
real Zeus and non-Zeus one-shot adapters; tests use offline functions. Candidate
changes are append-only to the existing SKILL.md, which makes the apply step
reversible and prevents a proposer from replacing the operator's baseline.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import propose_zeus_candidate as pzc
import run_improve_loop as rl
import score_hermes_skill_run as score

_ALLOWED_VERDICTS = {"better", "equal", "worse"}
_CREDENTIALS = tuple(pattern for pattern, _label in pzc.CRED_PATTERNS)


def materialize_candidate(baseline: str, proposal: str) -> str:
    """Turn a proposer addition into a complete, append-only skill file."""
    if not isinstance(baseline, str) or not baseline.strip():
        raise ValueError("baseline skill must be non-empty")
    if not isinstance(proposal, str) or not proposal.strip():
        raise ValueError("proposal must be non-empty")
    addition = proposal.strip()
    diff_lines = addition.splitlines()
    is_diff = addition.startswith("```diff") or any(
        line.startswith(("--- ", "+++ ", "@@")) for line in diff_lines
    )
    if is_diff:
        headers = [line[4:].strip() for line in diff_lines if line.startswith(("--- ", "+++ "))]
        if len(headers) == 2 and Path(headers[0]).name != Path(headers[1]).name:
            raise ValueError("proposal changes the target filename")
        additions = []
        for line in diff_lines:
            if line.startswith("```") or line.startswith(("--- ", "+++ ", "@@")):
                continue
            if line.startswith("-"):
                raise ValueError("proposal contains a deletion; only additions are allowed")
            if line.startswith("+"):
                additions.append(line[1:])
        addition = "\n".join(additions).strip()
        if not addition:
            raise ValueError("diff proposal contains no additions")
    if addition.startswith("diff --git"):
        raise ValueError("proposal must be an append-only Markdown addition, not a diff")
    if any(pattern.search(addition) for pattern in _CREDENTIALS):
        raise ValueError("proposal contains a credential-shaped value")
    return baseline.rstrip() + "\n\n" + addition + "\n"


def validate_candidate(candidate: str, expected_name: str = "bestplan") -> list[str]:
    """Return structural/credential errors; an empty list means replay-safe."""
    errors: list[str] = []
    if not isinstance(candidate, str) or not candidate.strip():
        return ["candidate is empty"]
    if not candidate.startswith("---\n"):
        errors.append("candidate is missing YAML frontmatter")
    match = re.search(r"(?ms)^---\n(.*?)\n---\n", candidate)
    if not match:
        errors.append("candidate frontmatter is malformed")
    elif not re.search(rf"(?m)^name:\s*{re.escape(expected_name)}\s*$", match.group(1)):
        errors.append(f"candidate frontmatter name is not {expected_name}")
    if f"#{expected_name.title()}" not in candidate and "# BestPlan" not in candidate:
        errors.append("candidate has no skill heading")
    for pattern in _CREDENTIALS:
        if pattern.search(candidate):
            errors.append("candidate contains a credential-shaped value")
            break
    return errors


def build_judge_prompt(baseline: str, candidate: str, task_title: str, blind_id: str) -> str:
    """Create a frozen, explicit A/B prompt with no tool-use requirement."""
    if blind_id not in {"A", "B"}:
        raise ValueError("blind_id must be A or B")
    arm_a, arm_b = (candidate, baseline) if blind_id == "A" else (baseline, candidate)
    return (
        "You are an independent, non-Zeus blind reviewer. Compare two versions "
        "of one Hermes SKILL.md against the concrete failure title below. "
        "Do not infer which arm is newer. Return ONLY one JSON object with "
        "exactly these fields: verdict (better, equal, or worse, meaning ARM A "
        "versus ARM B), score (number from 0 to 1), rationale (short string). "
        "Do not use tools and do not "
        "include markdown fences or secrets.\n\n"
        f"FAILURE TITLE: {task_title}\n"
        "ARM A:\n<ARM_A>\n"
        f"{arm_a}\n</ARM_A>\n\n"
        "ARM B:\n<ARM_B>\n"
        f"{arm_b}\n</ARM_B>\n"
    )


def parse_judge_response(raw: str, blind_id: str, task_id: str) -> dict:
    """Parse the strict judge contract; reject prose, fences, and bad scores."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("judge returned empty output")
    try:
        row = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("judge output was not JSON") from exc
    if not isinstance(row, dict):
        raise ValueError("judge output must be a JSON object")
    verdict = row.get("verdict")
    score_value = row.get("score")
    rationale = row.get("rationale")
    if verdict not in _ALLOWED_VERDICTS:
        raise ValueError("judge verdict is outside the allowed enum")
    if isinstance(score_value, bool) or not isinstance(score_value, (int, float)) or not 0 <= score_value <= 1:
        raise ValueError("judge score must be a number in [0,1]")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
        raise ValueError("judge rationale must be a short non-empty string")
    if blind_id == "B":
        verdict = {"better": "worse", "equal": "equal", "worse": "better"}[verdict]
    return {
        "schema_version": 1,
        "task_id": task_id,
        "blind_candidate": blind_id,
        "verdict": verdict,
        "score": float(score_value),
        "rationale": rationale.strip(),
    }


def _write_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _target_relative(skill_path: Path, live_skills: Path) -> str:
    try:
        return skill_path.resolve().relative_to(live_skills.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("skill path must be inside the live skills directory") from exc


def run_live_chain(
    failures: list[dict],
    state_dir: Path,
    live_skills: Path,
    skill_path: Path,
    proposer: Callable[[str], str],
    judge: Callable[[str], str],
    applier: Callable[[Path, str, Path, Path], dict],
    run_id: str = "live-run",
    threshold: float = 0.7,
) -> dict:
    """Run every harvested failure, applying only independently green candidates."""
    state_dir = Path(state_dir)
    live_skills = Path(live_skills)
    skill_path = Path(skill_path)
    run_dir = state_dir.parent / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report = {"ok": True, "halted": False, "run_dir": str(run_dir), "applied": [], "blocked": [], "notes": []}
    if not failures:
        report["summary"] = "no failures to improve"
        return report
    target_rel = _target_relative(skill_path, live_skills)
    judge_path = run_dir / "judges.jsonl"
    score_path = run_dir / "scorecard.tsv"

    for index, failure in enumerate(failures):
        task_id = str(failure.get("task_id") or "")
        try:
            baseline = skill_path.read_text()
            prompt = pzc.build_proposer_prompt(failure, str(skill_path))
            proposal = proposer(prompt)
            candidate = materialize_candidate(baseline, proposal)
            candidate_errors = validate_candidate(candidate)
            if candidate_errors:
                raise ValueError("candidate replay validation failed: " + "; ".join(candidate_errors))

            blind_id = "A" if index % 2 == 0 else "B"
            judge_prompt = build_judge_prompt(
                baseline, candidate, str(failure.get("task_title", "")), blind_id
            )
            row = pzc.stage_run(
                run_dir=run_dir,
                task={**failure, "skill_path": str(skill_path)},
                baseline_text=baseline,
                candidate_text=candidate,
                researcher_id="qwen-zeus",
                judge_model="gpt-5.6-sol",
                judge_prompt_hash="sha256:" + hashlib.sha256(judge_prompt.encode()).hexdigest(),
                seed_blind=blind_id,
            )
            judge_row = parse_judge_response(
                judge(judge_prompt),
                row["blind_id"],
                task_id,
            )
            _write_jsonl(judge_path, judge_row)
            rows, problems = score.load_judges(judge_path)
            if problems or not rows:
                raise ValueError("scorecard input rejected")
            score_path.write_text(score.render(score.aggregate(rows)))
            verdict = rl.decide(
                {
                    "scorecard_ok": True,
                    "scorecard_mean": judge_row["score"],
                    "threshold": threshold,
                    "replay_passes": True,
                    "has_secrets": False,
                    "change_class": "skill",
                    "verdict": judge_row["verdict"],
                }
            )
            if verdict["action"] != "apply":
                report["blocked"].append(task_id)
                report["notes"].append(f"{task_id}: {verdict['reason']}")
                report["ok"] = False
                report["halted"] = True
                continue
            applier(live_skills, target_rel, run_dir / "candidates" / "SKILL.md.candidate", state_dir)
            report["applied"].append(task_id)
        except Exception as exc:  # noqa: BLE001 - one bad candidate must fail closed
            report["blocked"].append(task_id)
            report["notes"].append(f"{task_id or 'unknown task'}: {exc}")
            report["ok"] = False
            report["halted"] = True

    report["summary"] = f"{len(report['applied'])} applied, {len(report['blocked'])} blocked"
    return report
