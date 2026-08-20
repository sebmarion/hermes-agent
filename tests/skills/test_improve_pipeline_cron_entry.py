"""Tests for the scheduler entry's fail-closed behavior."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_x_bookmarks as hx  # noqa: E402
import improve_cron_entry as entry  # noqa: E402
from hermes_state import SessionDB


def test_bookmark_failure_halts_and_reports_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)

    def unavailable(_n):
        raise RuntimeError("xurl unavailable")

    monkeypatch.setattr(hx, "fetch_bookmarks", unavailable)
    assert entry.main(["entry"]) != 0
    reports = list(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["ok"] is False
    assert report["halted"] is True


def test_cron_harvests_real_session_rows_before_halting_live_chain(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0001", source="cli", model="test")
    db.append_message("real-session-0001", role="user", content="repair the skill")
    db.append_message("real-session-0001", role="assistant", content="ERROR: the check failed")
    db.end_session("real-session-0001", "done")
    db.close()

    assert entry.main(["entry", "--db-path", str(db_path)]) != 0
    reports = list(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["n_failures_new"] == 1
    assert isinstance(report["watermark_sessions"], int)
    assert report["watermark_sessions"] > 0
    assert (tmp_path / "failures.jsonl").is_file()
    failure = json.loads((tmp_path / "failures.jsonl").read_text())
    assert failure["before_session_ids"] == ["real-session-0001"]
    assert "session data source is not wired" not in " ".join(report["notes"])