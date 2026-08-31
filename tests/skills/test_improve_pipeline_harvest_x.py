"""Tests for harvest_x_bookmarks.py — X-bookmark harvesting + pre-filter.

Deterministic + offline: feed fixture "bookmark" dicts (modeled on xurl
bookmarks output) into the pure core; assert actionability filtering,
credential scrubbing, and that non-actionable posts bucket to a digest NOT the
fixer pipeline. No network is touched.
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

import harvest_x_bookmarks as hx  # noqa: E402


def _bm(bid: int, text: str, url: str = "") -> dict:
    return {"id": f"B{bid}", "full_text": text, "url": url or f"https://x.com/i/status/{bid}"}


# ---------------------------------------------------------------------------
# actionability pre-filter (cheap keyword gate BEFORE any LLM call)
# ---------------------------------------------------------------------------

def test_actionable_tool_repo_kept() -> None:
    bms = [_bm(1, "New CLI tool for managing skills from terminal https://github.com/x/skillcli")]
    kept = hx.filter_actionable(bms)
    assert len(kept) == 1, f"expected actionable bookmark kept, got {kept}"


def test_noise_bucketed_to_digest_not_fixer() -> None:
    noise = [
        _bm(1, "beautiful sunset over the ocean today"),
        _bm(2, "lol this cat video is amazing"),
    ]
    kept, digest = hx.partition(actionable=hx.is_actionable, bookmarks=noise)
    assert kept == [], "non-actionable bookmarks must not enter the fixer pipeline"
    assert len(digest) == 2, "noise should be bucketed to the digest"
    assert all("id" in d and "full_text" in d for d in digest)


def test_dedupe_by_bookmark_id() -> None:
    # two posts with the SAME bookmark id AND actionable text -> only one kept
    dup = [
        _bm(1, "tip about automating hermes skills"),
        {"id": "B1", "full_text": "same id re-saved: hermes skill automation", "url": "https://x.com/i/status/1"},
    ]
    kept = hx.filter_actionable(dup)
    assert len(kept) == 1, f"duplicate bookmark id must be deduped, got {len(kept)}"
    assert kept[0]["id"] == "B1"


# ---------------------------------------------------------------------------
# credential safety in extracted sidecar
# ---------------------------------------------------------------------------

def test_credentials_redacted_in_sidecar(tmp_path: Path) -> None:
    bm = _bm(3, 'use apikey=abcdef0123456789XYZ in your config')
    records = hx.build_sidecar([bm])
    raw = json.dumps(records)
    assert "abcdef0123456789XYZ" not in raw, "credential leaked into bookmark sidecar"
    assert "apikey" in raw or "redacted" in raw.lower()


def test_credentials_redacted_from_bookmark_urls() -> None:
    bm = _bm(6, "Hermes tool", "https://x.example/?apikey=abcdef0123456789XYZ")
    raw = json.dumps(hx.build_sidecar([bm]))
    assert "abcdef0123456789XYZ" not in raw


def test_sidecar_has_extracted_fields() -> None:
    bm = _bm(4, "Hermes plugin idea: add a kanban view to the dashboard")
    rec = hx.build_sidecar([bm])[0]
    for field in ("bookmark_id", "text_snippet", "url", "extracted_idea"):
        assert field in rec, f"missing sidecar field {field} in {rec}"
    assert rec["bookmark_id"] == "B4"


# ---------------------------------------------------------------------------
# CLI wiring: writes a sidecar JSONL without touching ~/.xurl
# ---------------------------------------------------------------------------

def test_write_sidecar_roundtrip(tmp_path: Path) -> None:
    recs = hx.build_sidecar([_bm(5, "tip about prompting")])
    out_path = tmp_path / "bookmarks.jsonl"
    n = hx.write_sidecar(out_path, recs)
    assert n == 1
    lines = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    assert lines[0]["bookmark_id"] == "B5"


def test_bookmark_fetch_failure_is_not_misreported_as_empty(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("xurl")

    monkeypatch.setattr(hx, "run_text_bounded", unavailable)
    with pytest.raises(RuntimeError, match="xurl unavailable"):
        hx.fetch_bookmarks()