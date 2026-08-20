"""Offline contract tests for the live improve-loop chain."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import live_pipeline as lp  # noqa: E402
import run_improve_loop as rl  # noqa: E402


def _failure() -> dict:
    return {
        "task_id": "task_1234abcd",
        "task_title": "bestplan timeout",
        "task_instructions": "gateway timed out while planning",
        "failure_signature": "timeout",
        "before_session_ids": ["session_123456"],
    }


def test_worse_judge_verdict_blocks_apply() -> None:
    verdict = rl.decide(
        {
            "scorecard_ok": True,
            "scorecard_mean": 0.95,
            "threshold": 0.7,
            "replay_passes": True,
            "has_secrets": False,
            "change_class": "skill",
            "verdict": "worse",
        }
    )
    assert verdict["action"] == "block"
    assert "verdict" in verdict["reason"]


def test_judge_response_maps_blind_arm_b_to_candidate_verdict() -> None:
    row = lp.parse_judge_response(
        json.dumps({"verdict": "better", "score": 0.9, "rationale": "baseline wins"}),
        blind_id="B",
        task_id="task_1234abcd",
    )
    assert row["verdict"] == "worse"


def test_materialize_candidate_is_append_only_and_keeps_frontmatter() -> None:
    baseline = "---\nname: bestplan\ndescription: test\n---\n\n# BestPlan\n"
    proposal = "## New timeout rule\n\nRun the bounded retry check before escalation."

    candidate = lp.materialize_candidate(baseline, proposal)

    assert candidate.startswith(baseline)
    assert candidate.rstrip().endswith(proposal)
    assert lp.validate_candidate(candidate, expected_name="bestplan") == []


def test_materialize_candidate_accepts_fenced_unified_diff_additions() -> None:
    baseline = "---\nname: bestplan\ndescription: test\n---\n\n# BestPlan\n"
    proposal = """```diff
--- /tmp/SKILL.md
+++ /tmp/SKILL.md
@@ -1,3 +1,6 @@
 # BestPlan
+
+## Timeout rule
+
+Use bounded recovery.
```"""

    candidate = lp.materialize_candidate(baseline, proposal)

    assert candidate.startswith(baseline)
    assert "## Timeout rule" in candidate


def test_live_chain_scores_and_applies_only_green_candidate(tmp_path: Path) -> None:
    live = tmp_path / "skills"
    target = live / "software-development" / "bestplan" / "SKILL.md"
    target.parent.mkdir(parents=True)
    baseline = "---\nname: bestplan\ndescription: test\n---\n\n# BestPlan\n"
    target.write_text(baseline)

    calls: list[str] = []

    def proposer(_prompt: str) -> str:
        calls.append("proposer")
        return "## New timeout rule\n\nUse the bounded retry check."

    def judge(_prompt: str) -> str:
        calls.append("judge")
        return json.dumps({"verdict": "better", "score": 0.95, "rationale": "clearer"})

    def applier(live_root, target_rel, candidate, state_dir):
        calls.append("apply")
        assert Path(live_root) == live
        assert target_rel == "software-development/bestplan/SKILL.md"
        assert Path(candidate).read_text().startswith(baseline)
        return {"ok": True}

    report = lp.run_live_chain(
        failures=[_failure()],
        state_dir=tmp_path / "state",
        live_skills=live,
        skill_path=target,
        proposer=proposer,
        judge=judge,
        applier=applier,
        run_id="R1",
    )

    assert report["ok"] is True
    assert report["applied"] == ["task_1234abcd"]
    assert calls == ["proposer", "judge", "apply"]
    assert (tmp_path / "runs" / "R1" / "judges.jsonl").is_file()
    assert (tmp_path / "runs" / "R1" / "scorecard.tsv").is_file()


def test_live_chain_rejects_malformed_judge_without_apply(tmp_path: Path) -> None:
    live = tmp_path / "skills"
    target = live / "software-development" / "bestplan" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: bestplan\ndescription: test\n---\n\n# BestPlan\n")
    applied = []

    report = lp.run_live_chain(
        failures=[_failure()],
        state_dir=tmp_path / "state",
        live_skills=live,
        skill_path=target,
        proposer=lambda _prompt: "## Addition\n",
        judge=lambda _prompt: "not-json",
        applier=lambda *args: applied.append(args),
        run_id="R2",
    )

    assert report["ok"] is False
    assert report["halted"] is True
    assert applied == []
    assert any("judge" in note.lower() for note in report["notes"])


def test_live_chain_records_expected_block_without_failing_run(tmp_path: Path) -> None:
    live = tmp_path / "skills"
    target = live / "software-development" / "bestplan" / "SKILL.md"
    target.parent.mkdir(parents=True)
    baseline = "---\nname: bestplan\ndescription: test\n---\n\n# BestPlan\n"
    target.write_text(baseline)
    failures = [{**_failure(), "task_id": "task_blocked"}, {**_failure(), "task_id": "task_applied"}]
    judgments = iter([
        json.dumps({"verdict": "worse", "score": 0.9, "rationale": "regression"}),
        json.dumps({"verdict": "worse", "score": 0.9, "rationale": "improvement"}),
    ])
    applied = []

    report = lp.run_live_chain(
        failures=failures,
        state_dir=tmp_path / "state",
        live_skills=live,
        skill_path=target,
        proposer=lambda _prompt: "## Addition\n\nUse the bounded retry check.",
        judge=lambda _prompt: next(judgments),
        applier=lambda *args: applied.append(args),
        run_id="R3",
    )

    assert report["ok"] is True
    assert report["halted"] is False
    assert report["blocked"] == ["task_blocked"]
    assert report["applied"] == ["task_applied"]
    assert len(applied) == 1
