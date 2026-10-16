"""Tests for run_improve_loop.py — the fail-closed apply-decision core.

The decision function is PURE and deterministic so we can pin the policy
without any Zeus/judge/network. The live network steps (qualify, propose,
judge) are stubbed here; the real wiring lives in __main__ / runbook.
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import run_improve_loop as rl  # noqa: E402


def _gate(mean=0.9, replay=True, secrets=False, cls="skill") -> dict:
    return {
        "scorecard_ok": True,
        "scorecard_mean": mean,
        "threshold": 0.7,
        "replay_passes": replay,
        "has_secrets": secrets,
        "change_class": cls,
    }


# ---------------------------------------------------------------------------
# happy path applies
# ---------------------------------------------------------------------------

def test_green_skill_change_applies() -> None:
    gate = _gate()
    verdict = rl.decide(gate)
    assert verdict["action"] == "apply", f"expected apply, got {verdict}"


# ---------------------------------------------------------------------------
# each failed gate must block the apply (fail-closed)
# ---------------------------------------------------------------------------

def test_failing_replay_blocks_apply() -> None:
    verdict = rl.decide(_gate(replay=False))
    assert verdict["action"] != "apply", f"replay failure must block apply: {verdict}"
    assert "replay" in verdict["reason"], f"reason should name the blocker: {verdict}"


def test_low_score_blocks_apply() -> None:
    verdict = rl.decide(_gate(mean=0.5))
    assert verdict["action"] != "apply"
    assert "score" in verdict["reason"].lower()


def test_secret_presence_blocks_apply() -> None:
    verdict = rl.decide(_gate(secrets=True))
    assert verdict["action"] != "apply"
    assert "secret" in verdict["reason"].lower()


def test_missing_scorecard_blocks_apply() -> None:
    g = _gate()
    g["scorecard_ok"] = False
    verdict = rl.decide(g)
    assert verdict["action"] != "apply"


# ---------------------------------------------------------------------------
# core-path changes are NEVER auto-applied (parked/reported, not mutated)
# ---------------------------------------------------------------------------

def test_core_path_class_parked_not_applied() -> None:
    g = _gate(cls="config.yaml")
    verdict = rl.decide(g)
    assert verdict["action"] == "park", f"core-path change must be parked: {verdict}"
    assert "core" in verdict["reason"].lower()


def test_auth_class_parked_not_applied() -> None:
    verdict = rl.decide(_gate(cls="auth"))
    assert verdict["action"] == "park"


# ---------------------------------------------------------------------------
# empty input => no-op (idempotency foundation)
# ---------------------------------------------------------------------------

def test_no_failures_is_noop() -> None:
    outcome = rl.run_once(failures=[], bookmarks=[], gates=None)
    # returns a report dict with zero actions; nothing applied
    assert outcome["actions"] == []
    assert outcome["applied"] == []
    assert outcome["skipped"] == []


def test_cli_help_starts_without_runtime_name_error() -> None:
    script = Path(rl.__file__).resolve()
    proc = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()


def test_cli_refuses_unwired_live_mode(tmp_path: Path) -> None:
    failures = tmp_path / "failures.jsonl"
    failures.write_text('{"task_id":"task_1234"}\n')
    proc = subprocess.run(
        [sys.executable, str(Path(rl.__file__).resolve()), "--failures-jsonl", str(failures)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "dry-run" in proc.stderr.lower()