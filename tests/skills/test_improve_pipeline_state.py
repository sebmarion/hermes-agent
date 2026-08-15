"""Tests for pipeline_state.py — watermark/manifest persistence for the improve loop.

Deterministic + offline. Uses tmp_path exclusively; never touches the live
~/.hermes state dir.
"""
from __future__ import annotations

import json

import pytest

from pathlib import Path

import sys

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "optional-skills" / "research" / "darwinian-evolver" / "labs" / "scripts"),
)

import pipeline_state as ps  # noqa: E402


# ---------------------------------------------------------------------------
# watermarks
# ---------------------------------------------------------------------------

def test_watermark_missing_returns_none(tmp_path: Path) -> None:
    assert ps.read_watermark(tmp_path, "sessions") is None


def test_watermark_roundtrip(tmp_path: Path) -> None:
    ps.write_watermark(tmp_path, "sessions", 12345)
    assert ps.read_watermark(tmp_path, "sessions") == 12345


def test_watermark_tampered_json_fails_closed(tmp_path: Path) -> None:
    wf = tmp_path / "watermark.sessions.json"
    wf.write_text("{not json!!")
    with pytest.raises(ValueError):
        ps.read_watermark(tmp_path, "sessions")


def test_watermark_update_overwrites(tmp_path: Path) -> None:
    ps.write_watermark(tmp_path, "sessions", 1)
    ps.write_watermark(tmp_path, "sessions", 2)
    assert ps.read_watermark(tmp_path, "sessions") == 2


def test_watermark_key_sanitized(tmp_path: Path) -> None:
    # keys with weird chars must be rejected fail-closed (raise), never used
    # to escape the state dir
    with pytest.raises(ValueError):
        ps.write_watermark(tmp_path, "../evil", 5)
    assert not (tmp_path / ".." / "evil.json").exists()
    with pytest.raises(ValueError):
        ps.read_watermark(tmp_path, "../evil")


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------

def test_manifest_missing_returns_none(tmp_path: Path) -> None:
    assert ps.read_manifest(tmp_path) is None


def test_manifest_roundtrip(tmp_path: Path) -> None:
    m = {"run_id": "r1", "files": {"a.txt": "abc123"}, "applied": True}
    ps.write_manifest(tmp_path, m)
    assert ps.read_manifest(tmp_path) == m


def test_manifest_tampered_fails_closed(tmp_path: Path) -> None:
    mf = tmp_path / "manifest.json"
    mf.write_text("[1,2,")
    with pytest.raises(ValueError):
        ps.read_manifest(tmp_path)


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------

def test_write_is_atomic_no_partial_file(tmp_path: Path) -> None:
    # the repo-wide autouse conftest fixture creates hermes_test/ inside
    # tmp_path (redirected HERMES_HOME); assert only the atomicity property:
    # no .tmp residue and a single well-formed watermark file for the key
    ps.write_watermark(tmp_path, "sessions", 42)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"leftover temp files: {leftovers}"
    wf = tmp_path / "watermark.sessions.json"
    assert wf.is_file()
    assert json.loads(wf.read_text()) == {"value": 42}