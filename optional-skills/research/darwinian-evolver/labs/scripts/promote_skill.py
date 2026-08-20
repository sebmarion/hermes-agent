#!/usr/bin/env python3
"""Fail-closed promotion of verified skill changes to a Git remote."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Sequence


class PromotionError(RuntimeError):
    """Raised when a skill promotion cannot be proven safe."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PromotionError(f"git {' '.join(args)} failed: {detail[:400]}")
    return completed


def repository_root(path: Path) -> Path:
    candidate = Path(path).expanduser().resolve(strict=True)
    directory = candidate if candidate.is_dir() else candidate.parent
    completed = _git(directory, "rev-parse", "--show-toplevel")
    root = Path(completed.stdout.strip()).resolve(strict=True)
    if not (root / ".git").exists():
        raise PromotionError(f"resolved repository has no .git directory: {root}")
    return root


def relative_path(repo: Path, path: Path) -> str:
    try:
        relative = Path(path).expanduser().resolve(strict=True).relative_to(repo.resolve())
    except (OSError, ValueError) as exc:
        raise PromotionError(f"skill path is outside repository: {path}") from exc
    if not relative.parts or ".." in relative.parts:
        raise PromotionError(f"unsafe skill path: {path}")
    return relative.as_posix()


def _status_paths(repo: Path) -> tuple[list[str], list[str], list[str]]:
    completed = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status = line[0], line[1]
        path = line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if index_status != " " and not (index_status == "?" and worktree_status == "?"):
            staged.append(path)
        if worktree_status != " " and not (index_status == "?" and worktree_status == "?"):
            unstaged.append(path)
        if index_status == "?" and worktree_status == "?":
            untracked.append(path)
    return staged, unstaged, untracked


def _names(repo: Path, *diff_args: str) -> list[str]:
    completed = _git(repo, "diff", "--name-only", *diff_args)
    return [line for line in completed.stdout.splitlines() if line]


def promote(
    *,
    repo: Path,
    changed_paths: Sequence[str],
    verify: Callable[[list[str]], dict],
    remote: str = "origin",
    branch: str = "main",
    commit_message: str = "chore(skills): promote verified autonomous improvements",
) -> dict:
    """Verify, commit, push, and read back only the supplied changed paths."""
    repo = repository_root(Path(repo))
    allowed = sorted({str(path).strip() for path in changed_paths if str(path).strip()})
    if not allowed:
        raise PromotionError("no changed skill paths supplied")
    if any(Path(path).is_absolute() or ".." in Path(path).parts for path in allowed):
        raise PromotionError("changed skill paths must be safe repository-relative paths")
    if not commit_message.strip() or "\n" in commit_message:
        raise PromotionError("commit message must be a non-empty single line")

    current_branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if current_branch != branch:
        raise PromotionError(f"promotion requires branch {branch}, found {current_branch or 'detached HEAD'}")

    staged, _unstaged, _untracked = _status_paths(repo)
    if staged:
        raise PromotionError(f"pre-existing staged work must be cleared before promotion: {staged}")

    _git(repo, "fetch", "--quiet", remote, branch)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    remote_head = _git(repo, "rev-parse", f"{remote}/{branch}").stdout.strip()
    if head != remote_head:
        raise PromotionError(
            f"remote advanced or local diverged: local {head[:12]}, remote {remote_head[:12]}"
        )

    modified = set(_names(repo))
    missing = sorted(set(allowed) - modified)
    unexpected_target_state = sorted(modified.intersection(allowed) - set(allowed))
    if missing:
        raise PromotionError(f"expected skill changes are not present: {missing}")
    if unexpected_target_state:
        raise PromotionError(f"unexpected target state: {unexpected_target_state}")

    verification = verify(allowed)
    if not isinstance(verification, dict) or verification.get("status") != "passed":
        raise PromotionError(f"verification did not pass: {verification!r}")

    _git(repo, "add", "--", *allowed)
    staged_after = _names(repo, "--cached")
    if staged_after != allowed:
        _git(repo, "reset", "--", *allowed, check=False)
        raise PromotionError(f"promotion staged unexpected paths: {staged_after}")

    _git(repo, "commit", "-m", commit_message)
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    pushed = _git(repo, "push", remote, f"HEAD:{branch}", check=False)
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout).strip()
        raise PromotionError(f"push failed after local commit {commit[:12]}: {detail[:400]}")

    readback = _git(repo, "ls-remote", remote, f"refs/heads/{branch}")
    remote_after = readback.stdout.split()[0] if readback.stdout.split() else ""
    if remote_after != commit:
        raise PromotionError(
            f"remote read-back mismatch: pushed {commit[:12]}, remote has {remote_after[:12]}"
        )

    _staged, unstaged, untracked = _status_paths(repo)
    return {
        "status": "pushed",
        "repository": str(repo),
        "branch": branch,
        "remote": remote,
        "commit": commit,
        "remote_head": remote_after,
        "changed_paths": allowed,
        "verification": verification,
        "preserved_unstaged_paths": sorted(set(unstaged) | set(untracked)),
    }
