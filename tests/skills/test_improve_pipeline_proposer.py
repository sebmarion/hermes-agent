"""Tests for propose_zeus_candidate.py — the live-proposal shim.

The PURE core (prompt construction, staging layout, schema-row mapping,
blind_id + hash bookkeeping) is pinned offline here. The network step
(`call_zeus`) is injected/stubbed in tests; a smoke hit against the real Zeus
endpoint is exercised separately once the live CLI is healthy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import propose_zeus_candidate as pzc  # noqa: E402


def _failure(task_id="task_a1b2", sig="timeout", inst="gateway refused the connection repeatedly") -> dict:
    return {"task_id": task_id, "task_title": "x", "task_instructions": inst, "failure_signature": sig}


# ---------------------------------------------------------------------------
# prompt construction (offline, deterministic)
# ---------------------------------------------------------------------------

def test_build_prompt_includes_failure() -> None:
    prompt = pzc.build_proposer_prompt(_failure(), skill_path="skills/research/bestplan/SKILL.md")
    assert isinstance(prompt, str) and len(prompt) > 100
    assert _failure()["task_instructions"] in prompt
    assert "SKILL.md" in prompt or "skill" in prompt.lower()


def test_build_prompt_is_stable_for_same_input() -> None:
    f = _failure()
    p1 = pzc.build_proposer_prompt(f, skill_path="skills/x/SKILL.md")
    p2 = pzc.build_proposer_prompt(f, skill_path="skills/x/SKILL.md")
    assert p1 == p2, "prompt must be byte-stable (caching / reproducibility)"
    sha = hashlib.sha256(p1.encode()).hexdigest()
    assert len(sha) == 64


# ---------------------------------------------------------------------------
# staging layout + schema mapping
# ---------------------------------------------------------------------------

def test_stage_writes_baseline_candidate_dataset(tmp_path: Path) -> None:
    base_text = "# Baseline skill\n\n## Pitfalls\n1. old rule"
    cand_text = "# Baseline skill\n\n## Pitfalls\n1. old rule\n2. NEW anti-timeout check: retry-with-backoff\n"

    run_dir = tmp_path / "runs" / "R1"
    pzc.stage_run(
        run_dir=run_dir,
        task=_failure(),
        baseline_text=base_text,
        candidate_text=cand_text,
        researcher_id="qwen-zeus",
        judge_model="deepseek/deepseek-v4-flash-0731",
        judge_prompt_hash="sha256:" + "a" * 64,
        seed_blind="A",
    )

    assert (run_dir / "baseline").is_dir()
    assert (run_dir / "candidates").is_dir()
    assert (run_dir / "baseline" / "SKILL.md").exists()
    assert (run_dir / "candidates" / "SKILL.md.candidate").exists()

    ds_rows = [json.loads(l) for l in (run_dir / "dataset.jsonl").read_text().splitlines()]
    assert len(ds_rows) == 1
    row = ds_rows[0]
    assert row["researcher_id"] == "qwen-zeus"
    assert row["task_id"] == "task_a1b2"
    assert row["blind_id"] == "A"
    assert row["schema_version"] == 1
    assert row["candidate_patch_sha256"]
    assert row["before_session_ids"]


def test_stage_assigns_blind_b_when_seed_A_used_first(tmp_path: Path) -> None:
    # two tasks => one gets A, next gets B (blind labels assigned deterministically)
    run = tmp_path / "runs" / "R2"
    f1, f2 = _failure("task_c3d4"), _failure("task_e5f6")
    pzc.stage_run(run_dir=run, task=f1, baseline_text="b", candidate_text="c",
                  researcher_id="z", judge_model="m/x", judge_prompt_hash="sha256:" + "b" * 64, seed_blind="B")
    pzc.stage_run(run_dir=run, task=f2, baseline_text="b", candidate_text="c",
                  researcher_id="z", judge_model="m/x", judge_prompt_hash="sha256:" + "b" * 64, seed_blind=None)
    rows = [json.loads(l) for l in (run / "dataset.jsonl").read_text().splitlines()]
    assert {r["task_id"]: r["blind_id"] for r in rows} == {"task_c3d4": "B", "task_e5f6": "A"}


# ---------------------------------------------------------------------------
# fail-closed: bad input never fabricates a run dir
# ---------------------------------------------------------------------------

def test_missing_baseline_or_candidate_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises((ValueError, FileNotFoundError)):
        pzc.stage_run(run_dir=tmp_path / "R", task=_failure(), baseline_text="",
                      candidate_text="nonempty", researcher_id="z",
                      judge_model="m/x", judge_prompt_hash="sha256:" + "c" * 64)


# ---------------------------------------------------------------------------
# decision integration: green proposal advances to apply-decision
# ---------------------------------------------------------------------------

def test_produce_decision_from_green_shim_state(tmp_path: Path) -> None:
    # simulate a fully-green shim outcome (scorecard ok + replay pass + no secrets)
    gate = {"scorecard_ok": True, "scorecard_mean": 0.95, "threshold": 0.7,
            "replay_passes": True, "has_secrets": False, "change_class": "skill"}
    import run_improve_loop as rl

    verdict = rl.decide(gate)
    assert verdict["action"] == "apply"