"""Tests for ledger_rollup.py — net-positive longitudinal reporting.

Deterministic + offline: feed a small fixture change-ledger.jsonl and assert
the weekly rollup computes recurrence deltas / aggregates as documented, and
handles an empty ledger (no fabrication).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import ledger_rollup as lr  # noqa: E402


def _entry(ts, sig, delta=0.3, cls="skill", action="apply") -> dict:
    return {
        "ts": ts,
        "class": cls,
        "target": f"{sig}.md",
        "gates_passed": True,
        "scorecard_mean": 0.9,
        "outcome_delta": delta,
        "rollback_sha": "deadbeef",
        "source_failure_ids": [f"failure_{sig}"],
        "failure_signature": sig,
        "action": action,
    }


# ---------------------------------------------------------------------------
# aggregate over the ledger
# ---------------------------------------------------------------------------

def test_rollup_computes_success_and_deltas() -> None:
    entries = [
        _entry("2026-08-01", "timeout", 0.4),
        _entry("2026-08-02", "timeout", 0.5),
        _entry("2026-08-03", "error"),
    ]
    agg = lr.aggregate(entries)
    assert agg["n_applied"] == 3
    assert len(agg["by_signature"]) == 2
    to = agg["by_signature"]["timeout"]
    assert abs(to["avg_outcome_delta"] - 0.45) < 1e-6
    assert to["n"] == 2


# ---------------------------------------------------------------------------
# empty ledger → no fabricated signal
# ---------------------------------------------------------------------------

def test_empty_ledger_is_noop_not_invented() -> None:
    agg = lr.aggregate([])
    assert agg["n_applied"] == 0
    assert agg["by_signature"] == {}
    md = lr.render_report(agg)
    assert isinstance(md, str) and len(md) > 10


# ---------------------------------------------------------------------------
# render produces usable markdown summary with verdict text
# ---------------------------------------------------------------------------

def test_report_lists_signatures_and_verdict() -> None:
    entries = [_entry("2026-08-01", "timeout", 0.5), _entry("2026-08-02", "error")]
    agg = lr.aggregate(entries)
    md = lr.render_report(agg)
    assert "timeout" in md
    assert "error" in md
    # a positive average delta should surface as a positive net statement
    assert any(kw in md.lower() for kw in ("net-positive", "improved", "positive"))


# ---------------------------------------------------------------------------
# CLI wiring writes report.md
# ---------------------------------------------------------------------------

def test_write_report(tmp_path: Path) -> None:
    entries = [_entry("2026-08-01", "timeout", 0.6)]
    out = tmp_path / "weekly-report.md"
    n = lr.write_weekly_report(out, entries)
    assert n == 1
    assert out.is_file()
    content = out.read_text()
    assert "Weekly net-positive report" in content
    assert "timeout" in content


def test_corrupt_ledger_is_not_treated_as_clean(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"action":"apply"}\nnot-json\n')
    with pytest.raises(ValueError, match="malformed"):
        lr.load_ledger(ledger)