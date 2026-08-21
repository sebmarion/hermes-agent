"""Tests for fail-closed promotion of accepted skill changes."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import sys

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "optional-skills"
        / "research"
        / "darwinian-evolver"
        / "labs"
        / "scripts"
    ),
)

import promote_skill  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "skills"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Operator")
    _git(repo, "config", "user.email", "operator@example.test")
    (repo / "bestplan.md").write_text("baseline\n")
    (repo / "unrelated.md").write_text("unrelated baseline\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def test_promote_commits_and_pushes_only_allowed_skill_path(tmp_path: Path) -> None:
    repo, remote = _repo(tmp_path)
    (repo / "bestplan.md").write_text("baseline\naccepted improvement\n")
    (repo / "unrelated.md").write_text("local unrelated work\n")

    result = promote_skill.promote(
        repo=repo,
        changed_paths=["bestplan.md"],
        verify=lambda paths: {"status": "passed", "paths": paths},
    )

    assert result["status"] == "pushed"
    assert result["remote_head"] == result["commit"]
    assert _git(repo, "diff", "--name-only") == "unrelated.md"
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "bestplan.md"
    assert _git(repo, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == result["commit"]


def test_promote_requires_verification_before_commit(tmp_path: Path) -> None:
    repo, _remote = _repo(tmp_path)
    (repo / "bestplan.md").write_text("unsafe candidate\n")

    with pytest.raises(promote_skill.PromotionError, match="verification"):
        promote_skill.promote(
            repo=repo,
            changed_paths=["bestplan.md"],
            verify=lambda _paths: {"status": "failed", "reason": "OCR failed"},
        )

    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_promote_rejects_preexisting_staged_work(tmp_path: Path) -> None:
    repo, _remote = _repo(tmp_path)
    (repo / "bestplan.md").write_text("accepted improvement\n")
    (repo / "unrelated.md").write_text("staged unrelated work\n")
    _git(repo, "add", "unrelated.md")

    with pytest.raises(promote_skill.PromotionError, match="staged"):
        promote_skill.promote(
            repo=repo,
            changed_paths=["bestplan.md"],
            verify=lambda _paths: {"status": "passed"},
        )


def test_promote_rejects_remote_advance_without_commit(tmp_path: Path) -> None:
    repo, remote = _repo(tmp_path)
    clone = tmp_path / "other"
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(clone))
    _git(clone, "config", "user.name", "Other Operator")
    _git(clone, "config", "user.email", "other@example.test")
    (clone / "other.md").write_text("remote advance\n")
    _git(clone, "add", "other.md")
    _git(clone, "commit", "-m", "remote advance")
    _git(clone, "push", "origin", "main")

    (repo / "bestplan.md").write_text("accepted improvement\n")
    with pytest.raises(promote_skill.PromotionError, match="remote advanced"):
        promote_skill.promote(
            repo=repo,
            changed_paths=["bestplan.md"],
            verify=lambda _paths: {"status": "passed"},
        )

    assert _git(repo, "rev-parse", "HEAD") != _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "diff", "--cached", "--name-only") == ""
