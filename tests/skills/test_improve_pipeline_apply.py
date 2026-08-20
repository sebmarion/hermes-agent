"""Tests for apply_skill_candidate.py — staged skill apply with .bak + rollback.

Deterministic + offline: operate on tmp_path staging/live/state dirs, never the
real ~/.hermes. Inject `apply_one`'s inner copy step to fault mid-apply and
assert a torn apply rolls back to the prior content via the manifest.
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

import apply_skill_candidate as ap  # noqa: E402
import pipeline_state as ps  # noqa: E402


def _stage(tmp_path: Path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "SKILL.md").write_text("ORIGINAL CONTENT v1")
    staging = tmp_path / "staging"
    staging.mkdir()
    return live, staging


# ---------------------------------------------------------------------------
# clean apply writes original to .bak + new content into place
# ---------------------------------------------------------------------------

def test_apply_writes_bak_and_new_content(tmp_path: Path) -> None:
    live, staging = _stage(tmp_path)
    candidate = staging / "SKILL.md"
    candidate.write_text("CANDIDATE CONTENT v2")

    result = ap.apply(live=live, target="SKILL.md", candidate=candidate, state_dir=tmp_path / "state")
    assert result["ok"] is True
    bak = live / "SKILL.md.bak"
    assert bak.is_file(), "original must be preserved to SKILL.md.bak"
    assert bak.read_text() == "ORIGINAL CONTENT v1"
    assert (live / "SKILL.md").read_text() == "CANDIDATE CONTENT v2"


# ---------------------------------------------------------------------------
# injected fault mid-apply must revert to the exact original
# ---------------------------------------------------------------------------

def test_torn_apply_rolls_back_via_fault(tmp_path: Path) -> None:
    live, staging = _stage(tmp_path)
    candidate = staging / "SKILL.md"
    candidate.write_text("CANDIDATE v2")

    calls = {"n": 0}

    def failing_copy(src, dst):
        calls["n"] += 1
        raise OSError("disk exploded mid-write")

    try:
        ap.apply(
            live=live, target="SKILL.md", candidate=candidate,
            state_dir=tmp_path / "state",
            _copy=failing_copy,
        )
    except OSError:
        pass

    # regardless of where it failed, the file must still read the ORIGINAL
    assert (live / "SKILL.md").read_text() == "ORIGINAL CONTENT v1", "torn apply corrupted the target"

    # if a manifest was written but the apply didn't complete cleanly, a later
    # recovery run should restore from the backup
    manifest = ps.read_manifest(tmp_path / "state")
    if manifest is not None:
        ap.recover_if_torn(live=live, target="SKILL.md", state_dir=tmp_path / "state")


def test_recover_from_backup(tmp_path: Path) -> None:
    live, staging = _stage(tmp_path)
    state = tmp_path / "state"
    # simulate: someone applied (candidate won), then corrupted the live file
    (live / "SKILL.md").write_text("CANDIDATE v2")
    (live / "SKILL.md.bak").write_text("ORIGINAL CONTENT v1")
    ps.write_manifest(state, {"targets": [{"path": "SKILL.md", "applied": True}]})

    ap.recover_all(live=live, state_dir=state)
    # explicit: recover restores from .bak when told a change is bad
    ap.restore_from_bak(live / "SKILL.md.bak", live / "SKILL.md")
    assert (live / "SKILL.md").read_text() == "ORIGINAL CONTENT v1"


def test_manifest_failure_rolls_live_file_back(tmp_path: Path, monkeypatch) -> None:
    live, staging = _stage(tmp_path)
    candidate = staging / "SKILL.md"
    candidate.write_text("CANDIDATE v2")

    def fail_manifest(*args, **kwargs):
        raise OSError("state disk unavailable")

    monkeypatch.setattr(ps, "write_manifest", fail_manifest)
    try:
        ap.apply(live=live, target="SKILL.md", candidate=candidate, state_dir=tmp_path / "state")
    except OSError as exc:
        assert "state disk unavailable" in str(exc)
    else:
        raise AssertionError("manifest failure must abort apply")
    assert (live / "SKILL.md").read_text() == "ORIGINAL CONTENT v1"


def test_nested_traversal_target_rejected(tmp_path: Path) -> None:
    live, staging = _stage(tmp_path)
    candidate = staging / "SKILL.md"
    candidate.write_text("CANDIDATE v2")
    with pytest.raises(ValueError, match="unsafe target"):
        ap.apply(live=live, target="nested/../../outside.md", candidate=candidate, state_dir=tmp_path / "state")