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


def test_cron_hands_harvested_failures_to_live_chain(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0002", source="cli", model="test")
    db.append_message("real-session-0002", role="user", content="repair the skill")
    db.append_message("real-session-0002", role="assistant", content="ERROR: the check failed")
    db.end_session("real-session-0002", "done")
    db.close()

    seen = {}

    def fake_chain(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "halted": False, "applied": ["task_x"], "blocked": [], "notes": []}

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: {"status": "pushed", "commit": "test", "remote_head": "test", "remote": "origin", "branch": "main"},
    )
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert len(seen["failures"]) == 1
    assert seen["failures"][0]["before_session_ids"] == ["real-session-0002"]


def test_cron_caps_live_candidates_and_persists_remaining_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(entry, "MAX_CANDIDATES_PER_RUN", 1)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    for index in (1, 2):
        sid = f"real-session-00{index:02d}"
        db.create_session(sid, source="cli", model="test")
        db.append_message(sid, role="user", content="repair the skill")
        db.append_message(sid, role="assistant", content=f"ERROR: check {index} failed")
        db.end_session(sid, "done")
    db.close()

    seen = {}

    def fake_chain(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "halted": False,
            "applied": [kwargs["failures"][0]["task_id"]],
            "blocked": [],
            "notes": [],
        }

    monkeypatch.setattr(entry, "run_live_chain", fake_chain)
    monkeypatch.setattr(
        entry.promote_skill,
        "promote",
        lambda **_kwargs: {"status": "pushed", "commit": "test", "remote_head": "test", "remote": "origin", "branch": "main"},
    )
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert len(seen["failures"]) == 1
    pending = [json.loads(line) for line in (tmp_path / "pending_failures.jsonl").read_text().splitlines()]
    assert len(pending) == 1


def test_cron_promotes_accepted_skill_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(entry, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(hx, "fetch_bookmarks", lambda _n: [])

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("real-session-0003", source="cli", model="test")
    db.append_message("real-session-0003", role="user", content="repair the skill")
    db.append_message("real-session-0003", role="assistant", content="ERROR: check failed")
    db.end_session("real-session-0003", "done")
    db.close()

    monkeypatch.setattr(
        entry,
        "run_live_chain",
        lambda **_kwargs: {
            "ok": True,
            "halted": False,
            "applied": ["task_accepted"],
            "blocked": [],
            "notes": [],
            "summary": "1 applied, 0 blocked",
        },
    )
    seen = {}

    def fake_promote(**kwargs):
        seen.update(kwargs)
        return {
            "status": "pushed",
            "commit": "abc123",
            "remote_head": "abc123",
            "remote": "origin",
            "branch": "main",
            "changed_paths": ["software-development/bestplan/SKILL.md"],
        }

    monkeypatch.setattr(entry.promote_skill, "promote", fake_promote)
    assert entry.main(["entry", "--db-path", str(db_path)]) == 0
    assert seen["changed_paths"] == ["software-development/bestplan/SKILL.md"]
    report = json.loads(next(tmp_path.glob("report-*.json")).read_text())
    assert report["promotion"]["status"] == "pushed"
    assert report["promotion"]["commit"] == "abc123"
