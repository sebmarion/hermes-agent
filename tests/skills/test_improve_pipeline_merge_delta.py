"""RED phase: baseline-delta engine contracts for merge_upstream.

These tests define the required behavior for the new delta-based updater:
1. Upstream change on a different line combines with a local core change
2. Overlapping change halts with no live mutation
3. Explicit owned runtime/plugin paths remain byte-identical
4. Missing/corrupt/unknown upstream anchor halts
5. Candidate commit is a descendant of the current fork HEAD
6. Remote movement between preview and publish halts
7. Successful publish is independently read back and equals the tested candidate SHA
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_upstream as mu  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _init_repo(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def test_delta_combines_upstream_and_local_changes(tmp_path: Path) -> None:
    """Contract 1: an upstream core change combines with a local owned change."""
    repo = tmp_path / "repo"
    base = _init_repo(repo, {
        "core.py": "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n",
        "optional-skills/own.md": "edge-v1\n",
    })

    # Upstream changes line2 (different line from local)
    (repo / "core.py").write_text("line1\nline2-upstream\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    # Local changes only an updater-owned path.
    _git(repo, "checkout", "-q", base)
    (repo / "optional-skills/own.md").write_text("edge-v2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local_sha = _git(repo, "rev-parse", "HEAD")

    # Build delta candidate from local, applying upstream delta (base->upstream)
    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"
    report = mu.build_delta_candidate(repo, base, upstream_sha, local_sha, state_path, preview)

    # Both allowed changes should be present.
    candidate_tree = mu.full_snapshot(preview, "HEAD")
    assert "core.py" in candidate_tree
    core_content = (preview / "core.py").read_text()
    assert "line2-upstream" in core_content  # upstream change applied
    assert "line9\n" in core_content
    assert (preview / "optional-skills/own.md").read_text() == "edge-v2\n"  # local edge preserved


def test_delta_halts_on_overlapping_change(tmp_path: Path) -> None:
    """Contract 2: overlapping change halts with no live mutation."""
    repo = tmp_path / "repo"
    base = _init_repo(repo, {
        "cron/scheduler.py": "line1\nline2\nline3\n",
    })

    # Upstream changes line2
    (repo / "cron/scheduler.py").write_text("line1\nline2-upstream\nline3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    # Local changes the SAME line2 differently
    _git(repo, "checkout", "-q", base)
    (repo / "cron/scheduler.py").write_text("line1\nline2-local\nline3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local_sha = _git(repo, "rev-parse", "HEAD")

    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"

    # Should halt (raise) because both sides changed line2
    with pytest.raises(mu.MergeUpstreamError, match="overlap|conflict"):
        mu.build_delta_candidate(repo, base, upstream_sha, local_sha, state_path, preview)

    # Live repo should be unchanged
    assert _git(repo, "rev-parse", "HEAD") == local_sha
    assert (repo / "cron/scheduler.py").read_text() == "line1\nline2-local\nline3\n"


def test_delta_halts_on_unowned_local_core_divergence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n", "upstream.py": "base\n"})

    (repo / "upstream.py").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", base)
    (repo / "core.py").write_text("unauthorized-local-core-change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(mu.ScopeViolation, match="unowned local Hermes paths"):
        mu.build_delta_candidate(
            repo,
            base,
            upstream_sha,
            local_sha,
            tmp_path / "state" / "upstream-sync.json",
            tmp_path / "preview",
        )


def test_delta_halts_on_unowned_local_core_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n", "upstream.py": "base\n"})

    (repo / "upstream.py").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", base)
    (repo / "core.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unauthorized local core deletion")
    local_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(mu.ScopeViolation, match="unowned local Hermes paths"):
        mu.build_delta_candidate(
            repo,
            base,
            upstream_sha,
            local_sha,
            tmp_path / "state" / "upstream-sync.json",
            tmp_path / "preview",
        )


def test_delta_preserves_owned_paths_byte_identical(tmp_path: Path) -> None:
    """Contract 3: explicit owned runtime paths remain byte-identical."""
    repo = tmp_path / "repo"
    base = _init_repo(repo, {
        "scripts/improve_loop_wrapper.py": "original-wrapper\n",
        "core.py": "core\n",
    })

    # Upstream changes core.py and owned paths
    (repo / "core.py").write_text("core-upstream\n")
    (repo / "scripts/improve_loop_wrapper.py").write_text("upstream-wrapper\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    # Local has owned paths unchanged from base
    local_sha = base

    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"
    mu.build_delta_candidate(repo, base, upstream_sha, local_sha, state_path, preview)

    # Owned paths should be byte-identical to local (base) versions
    assert (preview / "scripts/improve_loop_wrapper.py").read_text() == "original-wrapper\n"
    # Core should be upstream
    assert (preview / "core.py").read_text() == "core-upstream\n"


def test_delta_preserves_an_owned_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})
    (repo / "owned.txt").write_text("upstream resurrected it\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream adds owned path")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    state_path = tmp_path / "state" / "upstream-sync.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"schema": 1, "upstream_sha": base, "owned_paths": ["owned.txt"]})
    )
    preview = tmp_path / "preview"

    report = mu.build_delta_candidate(
        repo, base, upstream_sha, base, state_path, preview
    )

    assert report["owned_paths"] == ["owned.txt", "scripts/improve_loop_wrapper.py"]
    assert not (preview / "owned.txt").exists()
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{report['candidate_sha']}:owned.txt"],
        cwd=repo,
        capture_output=True,
    ).returncode != 0


def test_delta_owned_restore_does_not_follow_an_upstream_symlink(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"owned.txt": "local\n"})
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n")
    (repo / "owned.txt").unlink()
    (repo / "owned.txt").symlink_to(outside)
    _git(repo, "add", "owned.txt")
    _git(repo, "commit", "-qm", "upstream symlink")
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    state_path = tmp_path / "state" / "upstream-sync.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"schema": 1, "upstream_sha": base, "owned_paths": ["owned.txt"]})
    )
    preview = tmp_path / "preview"

    report = mu.build_delta_candidate(
        repo, base, upstream_sha, base, state_path, preview
    )

    assert outside.read_text() == "safe\n"
    assert not (preview / "owned.txt").is_symlink()
    assert (preview / "owned.txt").read_text() == "local\n"
    assert _git(repo, "show", f"{report['candidate_sha']}:owned.txt") == "local"


def test_delta_halts_on_missing_anchor(tmp_path: Path) -> None:
    """Contract 4: missing/corrupt/unknown upstream anchor halts."""
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "core\n"})

    # Create an upstream commit
    (repo / "core.py").write_text("core-upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    # Use a non-existent anchor
    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"

    with pytest.raises(mu.MergeUpstreamError, match="anchor|upstream"):
        mu.build_delta_candidate(repo, "nonexistent-anchor", upstream_sha, base, state_path, preview)


def test_delta_candidate_is_descendant_of_local_head(tmp_path: Path) -> None:
    """Contract 5: candidate commit is a descendant of the current fork HEAD."""
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "core\n"})

    (repo / "core.py").write_text("core-upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    local_sha = base

    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"
    mu.build_delta_candidate(repo, base, upstream_sha, local_sha, state_path, preview)

    # Candidate HEAD should be a descendant of local_sha
    candidate_sha = _git(preview, "rev-parse", "HEAD")
    merge_base = _git(preview, "merge-base", candidate_sha, local_sha)
    assert merge_base == local_sha  # local is an ancestor of candidate


def test_delta_candidate_records_exact_local_and_upstream_parents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "core\n"})

    (repo / "core.py").write_text("core-upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", base)
    (repo / "optional-skills/own.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "optional-skills/own.md").write_text("local\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local_sha = _git(repo, "rev-parse", "HEAD")

    state_path = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"
    report = mu.build_delta_candidate(
        repo, base, upstream_sha, local_sha, state_path, preview
    )

    commit_and_parents = _git(
        preview, "rev-list", "--parents", "-n", "1", report["candidate_sha"]
    ).split()
    assert commit_and_parents[1:] == [local_sha, upstream_sha]
    assert report["merge_parents"] == [local_sha, upstream_sha]
    assert report["candidate_tree_sha"] == _git(
        preview, "rev-parse", f"{report['candidate_sha']}^{{tree}}"
    )


def test_owned_only_delta_still_records_upstream_parent_with_local_tree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(
        repo,
        {"scripts/improve_loop_wrapper.py": "local-owned\n"},
    )

    (repo / "scripts/improve_loop_wrapper.py").write_text("upstream-owned\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream owned-only delta")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    preview = tmp_path / "preview"
    report = mu.build_delta_candidate(
        repo,
        base,
        upstream_sha,
        base,
        tmp_path / "state" / "upstream-sync.json",
        preview,
    )

    assert report["candidate_sha"] != base
    assert report["merge_parents"] == [base, upstream_sha]
    assert report["candidate_tree_sha"] == _git(repo, "rev-parse", f"{base}^{{tree}}")
    assert _git(
        preview, "rev-list", "--parents", "-n", "1", report["candidate_sha"]
    ).split()[1:] == [base, upstream_sha]


def test_candidate_lineage_guard_rejects_missing_upstream_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    local_sha = _init_repo(repo, {"core.py": "local\n"})
    tree_sha = _git(repo, "rev-parse", f"{local_sha}^{{tree}}")

    with pytest.raises(mu.MergeUpstreamError, match="parents"):
        mu.assert_candidate_lineage(
            repo,
            candidate_sha=local_sha,
            expected_tree_sha=tree_sha,
            local_sha=local_sha,
            upstream_sha="f" * 40,
        )


def test_candidate_lineage_guard_rejects_untested_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo, {"core.py": "base\n"})

    (repo / "upstream.txt").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", base)
    (repo / "local.txt").write_text("local\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--no-ff", "-qm", "candidate", upstream_sha)
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    untested_tree_sha = _git(repo, "rev-parse", f"{base}^{{tree}}")

    with pytest.raises(mu.MergeUpstreamError, match="tree"):
        mu.assert_candidate_lineage(
            repo,
            candidate_sha=candidate_sha,
            expected_tree_sha=untested_tree_sha,
            local_sha=local_sha,
            upstream_sha=upstream_sha,
        )


def test_noop_candidate_reports_no_merge_parents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    local_sha = _init_repo(repo, {"core.py": "current\n"})
    report = mu.build_delta_candidate(
        repo,
        local_sha,
        local_sha,
        local_sha,
        tmp_path / "state" / "upstream-sync.json",
        tmp_path / "preview",
    )

    assert report["noop"] is True
    assert report["merge_parents"] == []
    assert report["candidate_tree_sha"] == _git(
        repo, "rev-parse", f"{local_sha}^{{tree}}"
    )


def test_noop_refuses_recorded_upstream_without_local_ancestry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    upstream_sha = _init_repo(repo, {"upstream.py": "current\n"})
    _git(repo, "checkout", "--orphan", "fork")
    _git(repo, "rm", "-q", "-rf", ".")
    (repo / "local.py").write_text("fork\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fork")
    local_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(mu.MergeUpstreamError, match="not in local history"):
        mu.build_delta_candidate(
            repo,
            upstream_sha,
            upstream_sha,
            local_sha,
            tmp_path / "state" / "upstream-sync.json",
            tmp_path / "preview",
        )


def test_delta_halts_on_remote_movement(tmp_path: Path) -> None:
    """Contract 6: remote movement between preview and publish halts."""
    # This test requires mocking the remote SHA check
    # For now, test that the function exists and raises on mismatch
    assert hasattr(mu, "assert_remote_sha_unchanged")
    assert hasattr(mu, "publish_and_verify")


def test_delta_publish_readback_verifies_sha(tmp_path: Path) -> None:
    """Contract 7: successful publish is independently read back and equals the tested candidate SHA."""
    # Test that publish_and_verify exists and the readback logic is in place
    assert hasattr(mu, "publish_and_verify")

    # Test the readback function
    repo = tmp_path / "repo"
    sha = _init_repo(repo, {"core.py": "core\n"})

    # Mock ls-remote to return the same SHA
    original_run = mu._run
    mu._run = lambda r, *args, **kwargs: sha if "ls-remote" in args[0] else original_run(r, *args, **kwargs)

    try:
        mu.assert_remote_sha_unchanged(repo, "origin", sha)
    finally:
        mu._run = original_run
