"""Tests for propose_zeus_candidate.py — the live-proposal shim.

The PURE core (prompt construction, staging layout, schema-row mapping,
blind_id + hash bookkeeping) is pinned offline here. The network step
(`call_zeus`) is injected/stubbed in tests; a smoke hit against the real Zeus
endpoint is exercised separately once the live CLI is healthy.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import propose_zeus_candidate as pzc  # noqa: E402


def _failure(task_id="task_a1b2", sig="timeout", inst="gateway refused the connection repeatedly") -> dict:
    return {
        "task_id": task_id,
        "task_title": "x",
        "task_instructions": inst,
        "failure_signature": sig,
        "before_session_ids": ["session_fixture_123"],
    }


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


def test_build_prompt_lists_existing_level_two_headings(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# BestPlan\n\n## Procedure\nsteps\n\n## Pitfalls\nrisks\n")

    prompt = pzc.build_proposer_prompt(_failure(), skill_path=str(skill))

    assert "EXISTING LEVEL-2 HEADINGS" in prompt
    assert "## Procedure" in prompt
    assert "## Pitfalls" in prompt
    assert "extend an existing section" in prompt


def test_build_prompt_requests_markdown_not_patch_syntax() -> None:
    prompt = pzc.build_proposer_prompt(_failure(), skill_path="skills/x/SKILL.md")

    assert "raw Markdown content" in prompt
    assert "patch syntax" in prompt
    assert "diff-style" not in prompt


def test_build_prompt_expands_tilde_skill_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / "skill.md"
    skill.write_text("# BestPlan\n\n## Existing\n")

    prompt = pzc.build_proposer_prompt(_failure(), skill_path="~/skill.md")

    assert "## Existing" in prompt




def test_main_fails_closed_when_skill_path_is_missing(tmp_path: Path) -> None:
    failures = tmp_path / "failures.jsonl"
    failures.write_text(json.dumps(_failure()) + "\n")

    result = pzc.main([
        "entry", "--run-dir", str(tmp_path / "run"),
        "--failures-jsonl", str(failures),
        "--skill-path", str(tmp_path / "missing"), "--dry-run",
    ])

    assert result != 0
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


def test_dry_run_uses_linux_absolute_skill_baseline(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("REAL BASELINE CONTENT")
    failures = tmp_path / "failures.jsonl"
    failures.write_text(
        json.dumps({
            "task_id": "task_1234",
            "task_title": "failure",
            "task_instructions": "reproduce this failure",
            "failure_signature": "error",
            "before_session_ids": ["session_123456"],
            "skill_path": str(skill),
        }) + "\n"
    )
    run_dir = tmp_path / "run"
    assert pzc.main([
        "propose_zeus_candidate.py",
        "--run-dir", str(run_dir),
        "--failures-jsonl", str(failures),
        "--skill-path", str(skill),
        "--dry-run",
    ]) == 0
    assert (run_dir / "baseline" / "SKILL.md").read_text() == "REAL BASELINE CONTENT"


def test_stage_rejects_missing_real_session_evidence(tmp_path: Path) -> None:
    task = _failure()
    task.pop("before_session_ids")
    with pytest.raises(ValueError, match="real session"):
        pzc.stage_run(
            run_dir=tmp_path / "run",
            task=task,
            baseline_text="baseline",
            candidate_text="candidate",
            researcher_id="qwen-zeus",
            judge_model="deepseek/model",
            judge_prompt_hash="sha256:" + "a" * 64,
        )


def test_call_zeus_rejects_plain_http_public_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        pzc._validate_base_url("http://evil.example/v1")


def test_call_zeus_rejects_content_above_candidate_budget(monkeypatch) -> None:
    body = json.dumps(
        {"choices": [{"message": {"content": "x" * (pzc.MAX_LUNA_OUTPUT_CHARS + 1)}}]}
    ).encode()

    class Response:
        status = 200

        def read(self, limit: int) -> bytes:
            assert limit == pzc.MAX_LUNA_STDOUT_BYTES + 1
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(pzc.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="output limit"):
        pzc.call_zeus("http://127.0.0.1:8080/v1", "local", "model", "prompt")


def test_call_luna_uses_existing_openai_codex_route(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="## Candidate\n", stderr="")

    monkeypatch.setattr(pzc.shutil, "which", lambda _name: "hermes")
    monkeypatch.setattr(pzc, "run_text_bounded", fake_run)

    result = pzc.call_luna("repair the concrete failure")

    assert result == "## Candidate"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        "hermes",
        "-z",
        "repair the concrete failure",
        "--provider",
        "openai-codex",
        "--model",
        "gpt-5.6-luna",
        "--reasoning",
        "low",
        "--toolsets",
        "search",
        "--safe-mode",
        "--ignore-rules",
        "--in",
        "/tmp",
    ]
    assert kwargs["timeout"] == 180
    assert kwargs["max_stdout_bytes"] == pzc.MAX_LUNA_STDOUT_BYTES


def test_call_luna_rejects_output_above_candidate_budget(monkeypatch) -> None:
    monkeypatch.setattr(pzc.shutil, "which", lambda _name: "hermes")
    monkeypatch.setattr(
        pzc,
        "run_text_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="x" * (pzc.MAX_LUNA_OUTPUT_CHARS + 1), stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="output limit"):
        pzc.call_luna("repair the concrete failure")


def test_call_luna_fails_closed_on_empty_or_failed_output(monkeypatch) -> None:
    monkeypatch.setattr(pzc.shutil, "which", lambda _name: "hermes")
    monkeypatch.setattr(
        pzc,
        "run_text_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="Luna proposal failed"):
        pzc.call_luna("repair the concrete failure")


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