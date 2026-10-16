"""Regression tests for concise upstream-merge failure reports."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_upstream as mu  # noqa: E402


def test_summarize_apply_failure_names_conflicts_without_patch_noise() -> None:
    stdout = "\n".join(
        [
            "Applied patch to 'hermes_cli/profiles.py' cleanly.",
            "Falling back to direct application...",
            "Applied patch to 'tools/file_tools.py' cleanly.",
            "U apps/desktop/src/contrib/runtime-loader.test.ts",
            "U apps/desktop/src/contrib/runtime-loader.ts",
        ]
    )
    stderr = "warning: 2 lines add whitespace errors."

    detail = mu.summarize_apply_failure(stdout, stderr)

    assert detail == (
        "conflicting paths: apps/desktop/src/contrib/runtime-loader.test.ts, "
        "apps/desktop/src/contrib/runtime-loader.ts; warning: 2 lines add whitespace errors."
    )
    assert "Applied patch" not in detail
    assert "Falling back" not in detail


def test_summarize_apply_failure_preserves_non_conflict_error() -> None:
    detail = mu.summarize_apply_failure("", "error: patch failed: core.py:12")

    assert detail == "error: patch failed: core.py:12"
