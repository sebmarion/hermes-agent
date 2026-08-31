"""Tests for harvest_failures.py — session-failure mining for the improve loop.

Deterministic + offline. Feed fixture "sessions" (list of dicts modeled on
Hermes session rows) into the pure core; assert watermarking, signature
detection, and that NO raw session body ever reaches disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import harvest_failures as hf  # noqa: E402
import pipeline_state as ps  # noqa: E402


def _session(sid: int, text: str, title: str = "task") -> dict:
    return {"id": f"session_{sid:04d}", "seq": sid, "title": title, "body": text}


# ---------------------------------------------------------------------------
# watermark gating
# ---------------------------------------------------------------------------

def test_only_sessions_after_watermark_harvested(tmp_path: Path) -> None:
    sessions = [
        _session(1, "all good here"),
        _session(2, "ERROR: boom TaskException crashed"),
        _session(3, "another fine run"),
        _session(4, "blocked: gateway refused connection"),
    ]
    ps.write_watermark(tmp_path, "sessions", 2)
    issues = hf.extract_failures(sessions, watermark_seq=2)
    ids = [i["session_seq"] for i in issues]
    # seq 2 is the boundary: watermark 2 means "already processed up to 2",
    # so only strictly-later sessions are candidates
    assert ids == [4], f"expected only seq 4, got {ids}"


def test_failure_signatures_detected(tmp_path: Path) -> None:
    sessions = [
        _session(1, "we did a thing, finished ok"),
        _session(2, "FileNotFoundError: no such file or directory"),
        _session(3, "task fell back to remote because local died"),
        _session(4, "exit code 1: pytest failed 3 tests"),
        _session(5, "aborted: approval gate timed out waiting on user"),
    ]
    out = hf.extract_failures(sessions, watermark_seq=0)
    sigs = {i["failure_signature"] for i in out}
    assert "error" in sigs, f"missing error signature in {sigs}"
    assert "retry" in sigs, f"missing retry signature in {sigs}"
    assert "timeout" in sigs, f"missing timeout signature in {sigs}"
    assert len(out) == 3, f"expected 3 failures (2,3,4,5-ish), got {len(out)}"


def test_non_failure_sessions_skipped(tmp_path: Path) -> None:
    sessions = [_session(1, "everything normal, no errors, clean exit")]
    assert hf.extract_failures(sessions, watermark_seq=0) == []


# ---------------------------------------------------------------------------
# structured-only output (never raw bodies)
# ---------------------------------------------------------------------------

def test_no_raw_body_written_to_disk(tmp_path: Path) -> None:
    secret_text = "sk-super-secret-token-value-abcdef1234567890"
    sessions = [_session(1, f"oops {secret_text} crashed badly")]
    issues = hf.extract_failures(sessions, watermark_seq=0)
    out_path = tmp_path / "failures.jsonl"
    hf.write_failures(out_path, issues)
    raw = out_path.read_text()
    assert secret_text not in raw, "raw session body leaked to disk"
    for row in json.loads("[" + raw.strip().replace("\n", ",") + "]"):
        assert "body" not in row, "structured rows must not carry a raw body field"
        assert "task_instructions" in row


def test_failure_row_has_required_fields(tmp_path: Path) -> None:
    sessions = [_session(7, "gateway refused: timeout")]
    issues = hf.extract_failures(sessions, watermark_seq=0)
    row = issues[0]
    for field in (
        "task_id", "task_title", "task_instructions", "failure_signature",
        "before_session_ids", "session_seq",
    ):
        assert field in row, f"missing field {field} in {row}"
    assert row["task_id"].startswith("task_"), f"task_id must be task_*: {row['task_id']}"


# ---------------------------------------------------------------------------
# credential shapes must be scrubbed before they reach the file
# ---------------------------------------------------------------------------

def test_credentials_redacted_from_instructions(tmp_path: Path) -> None:
    sessions = [_session(1, "auth failed: apikey=abcdef0123456789XYZ crashed")]
    issues = hf.extract_failures(sessions, watermark_seq=0)
    out_path = tmp_path / "failures.jsonl"
    hf.write_facts(out_path, issues)
    raw = out_path.read_text()
    assert "abcdef0123456789XYZ" not in raw, "credential leaked into failure file"
    assert "apikey" in raw or "redacted" in raw.lower()


def test_load_hermes_sessions_projects_completed_db_rows() -> None:
    class FakeDB:
        def __init__(self) -> None:
            self.closed = False

        def search_sessions(self, **kwargs):
            assert kwargs == {"limit": -1, "offset": 0}
            return [
                {"id": "real-session-0001", "title": "finished task", "ended_at": 20},
                {"id": "active-session-0002", "title": "still running", "ended_at": None},
                {
                    "id": "autoresearch-session-0003",
                    "title": "BestPlan editor",
                    "ended_at": 30,
                    "source": "desktop",
                },
            ]

        def get_messages(self, session_id, include_compacted):
            assert include_compacted is True
            if session_id == "real-session-0001":
                return [
                    {"id": 41, "role": "user", "content": "repair the skill"},
                    {"id": 42, "role": "assistant", "content": "ERROR: the check failed"},
                ]
            if session_id == "autoresearch-session-0003":
                return [
                    {"id": 52, "role": "assistant", "content": "ERROR: proposer failed"}
                ]
            raise AssertionError("active sessions must not be read")

        def close(self):
            self.closed = True

    db = FakeDB()
    rows = hf.load_hermes_sessions(
        db_factory=lambda **_: db,
        ignored_session_ids={"autoresearch-session-0003"},
    )
    assert rows == [
        {
            "id": "real-session-0001",
            "seq": 42,
            "title": "finished task",
            "body": "user: repair the skill\nassistant: ERROR: the check failed",
        },
        {
            "id": "autoresearch-session-0003",
            "seq": 52,
            "title": "BestPlan editor",
            "body": "",
            "ignored": True,
        },
    ]
    assert [row["before_session_ids"] for row in hf.extract_failures(rows)] == [
        ["real-session-0001"]
    ]
    assert db.closed is True


def test_cli_harvests_fixture_and_advances_watermark(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps([_session(7, "ERROR: bounded failure", "BestPlan repair")])
    )
    out_path = tmp_path / "failures.jsonl"
    state_dir = tmp_path / "state"

    assert hf.main(
        [
            "harvest_failures.py",
            "--sessions-json",
            str(sessions_path),
            "--out",
            str(out_path),
            "--state-dir",
            str(state_dir),
        ]
    ) == 0

    assert len(out_path.read_text().splitlines()) == 1
    assert ps.read_watermark(state_dir, "sessions") == 7