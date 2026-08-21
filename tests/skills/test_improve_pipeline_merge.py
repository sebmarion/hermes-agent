"""TDD contract for the daily upstream merge planner.

The updater must preserve only the edge surfaces owned by this system and must
fail closed when the local line contains unknown/core changes. These tests use
small temporary git repositories and inject the command runner where practical;
no remote or live checkout is touched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_upstream as mu  # noqa: E402


def test_edge_path_allowlist_is_narrow() -> None:
    assert mu.is_edge_path("optional-skills/research/darwinian-evolver/SKILL.md")
    assert mu.is_edge_path("optional-skills/research/darwinian-evolver/labs/scripts/merge_upstream.py")
    assert mu.is_edge_path("tests/skills/test_improve_pipeline_merge.py")
    assert mu.is_edge_path("cron/scheduler.py")
    assert mu.is_edge_path("tests/cron/test_cron_script.py")
    assert mu.is_edge_path("plugins/hermes-bestplan/bestplan_ocr.py")
    assert mu.is_edge_path("plugins/hermes-bestplan/future_runtime.py")
    assert mu.is_edge_path("scripts/improve_loop_wrapper.py")
    assert not mu.is_edge_path("agent/run_agent.py")
    assert not mu.is_edge_path("hermes_cli/main.py")
    assert not mu.is_edge_path("package-lock.json")


def test_core_changes_are_rejected() -> None:
    with pytest.raises(mu.ScopeViolation, match="hermes_cli/main.py"):
        mu.assert_edge_only(["optional-skills/foo/SKILL.md", "hermes_cli/main.py"])


def test_required_bestplan_runtime_paths_are_fail_closed() -> None:
    with pytest.raises(mu.ConservationError, match="bestplan_ocr.py"):
        mu.assert_required_runtime_paths({"scripts/improve_loop_wrapper.py": "sha256:abc"})


def test_diff_status_preserves_add_modify_delete_operations() -> None:
    raw = "A\toptional-skills/a/SKILL.md\nM\ttests/skills/test_a.py\nD\toptional-skills/b/SKILL.md\n"
    assert mu.parse_name_status(raw) == [
        ("A", "optional-skills/a/SKILL.md"),
        ("M", "tests/skills/test_a.py"),
        ("D", "optional-skills/b/SKILL.md"),
    ]


def test_overlay_operations_are_edge_only() -> None:
    ops = mu.overlay_operations(
        [("A", "optional-skills/a/SKILL.md"), ("D", "optional-skills/b/SKILL.md")]
    )
    assert ops == [("copy", "optional-skills/a/SKILL.md"), ("delete", "optional-skills/b/SKILL.md")]


def test_conservation_detects_missing_edge_content() -> None:
    with pytest.raises(mu.ConservationError, match="optional-skills/a/SKILL.md"):
        mu.assert_conserved(
            expected={"optional-skills/a/SKILL.md": "sha256:abc"},
            actual={"optional-skills/a/SKILL.md": "sha256:def"},
        )


def test_conservation_accepts_exact_edge_content() -> None:
    expected = {"optional-skills/a/SKILL.md": "sha256:abc"}
    assert mu.assert_conserved(expected=expected, actual=dict(expected)) is None


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_temp_repo_overlay_carries_edge_delta_and_not_core(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "agent").mkdir()
    (repo / "agent/core.py").write_text("upstream-core")
    (repo / "optional-skills").mkdir()
    (repo / "optional-skills/own.md").write_text("edge-v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "optional-skills/own.md").write_text("edge-v2")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "edge change")
    head = _git(repo, "rev-parse", "HEAD")
    assert mu.changed_name_status(repo, base, head) == [("M", "optional-skills/own.md")]


def test_remote_url_guard_rejects_unknown_push_target() -> None:
    with pytest.raises(mu.RemoteGuardError):
        mu.assert_fork_remote("https://github.com/NousResearch/hermes-agent.git")
    with pytest.raises(mu.RemoteGuardError):
        mu.assert_fork_remote("https://evil.example/github.com/sebmarion/hermes-agent")
    with pytest.raises(mu.RemoteGuardError):
        mu.assert_fork_remote("git@github.com:sebmarion/hermes-agent.evil")
    mu.assert_fork_remote("git@github.com:sebmarion/hermes-agent.git")
    mu.assert_fork_remote("https://github.com/sebmarion/hermes-agent.git")


def test_apply_rejects_clean_checkout_head_changed_after_preview(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    with pytest.raises(mu.MergeUpstreamError, match="changed after preview"):
        mu.apply_candidate(repo, "not-a-real-candidate", expected_head="different-head")


def test_apply_candidate_uses_fast_forward_only_and_never_reset(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(mu, "_clean_checkout", lambda *_args, **_kwargs: None)

    def record(_repo, *args, **_kwargs):
        calls.append(tuple(args))
        return ""

    monkeypatch.setattr(mu, "_run", record)
    mu.apply_candidate(Path("/tmp/repo"), "candidate-sha", expected_head="source-sha")

    assert ("merge", "--ff-only", "candidate-sha") in calls
    assert not any(call and call[0] == "reset" for call in calls)


def test_apply_candidate_fast_forwards_detached_clone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "file.txt").write_text("candidate\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "--detach", base)

    mu.apply_candidate(repo, candidate, expected_head=base)

    assert _git(repo, "rev-parse", "HEAD") == candidate
    assert _git(repo, "status", "--porcelain") == ""


def test_publish_and_verify_uses_normal_push_without_force(monkeypatch) -> None:
    expected = "a" * 40
    candidate = "b" * 40
    calls: list[tuple[str, ...]] = []
    remote_reads = iter((expected, candidate))

    monkeypatch.setattr(
        mu,
        "_ssh_push_url",
        lambda *_args: "git@github.com:sebmarion/hermes-agent.git",
    )

    def record(_repo, *args, **_kwargs):
        calls.append(tuple(args))
        if args and args[0] == "ls-remote":
            return next(remote_reads)
        return ""

    monkeypatch.setattr(mu, "_run", record)
    assert mu.publish_and_verify(Path("/tmp/repo"), "fork", candidate, expected) == candidate

    push = next(call for call in calls if call and call[0] == "push")
    assert not any(part.startswith("--force") for part in push)
    assert push[-1] == f"{candidate}:main"


def test_https_fork_remote_is_converted_to_ssh_for_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "sebmarion", "https://github.com/sebmarion/hermes-agent.git")
    assert mu._ssh_push_url(repo, "sebmarion") == "git@github.com:sebmarion/hermes-agent.git"


def test_build_preview_starts_from_upstream_and_overlays_owned_edge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "agent").mkdir()
    (repo / "cron").mkdir()
    (repo / "tests/cron").mkdir(parents=True)
    (repo / "plugins/hermes-bestplan").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "optional-skills").mkdir()
    (repo / "agent/core.py").write_text("base-core")
    (repo / "cron/scheduler.py").write_text("base-scheduler")
    (repo / "tests/cron/test_cron_script.py").write_text("base-scheduler-test")
    (repo / "plugins/hermes-bestplan/bestplan_ocr.py").write_text("base-ocr")
    (repo / "scripts/improve_loop_wrapper.py").write_text("base-wrapper")
    (repo / "optional-skills/own.md").write_text("local-edge")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    # Upstream advances only its core-independent edge files.
    (repo / "agent/core.py").write_text("base-core")
    (repo / "cron/scheduler.py").write_text("upstream-scheduler")
    (repo / "tests/cron/test_cron_script.py").write_text("upstream-scheduler-test")
    (repo / "plugins/hermes-bestplan/bestplan_ocr.py").write_text("upstream-ocr")
    (repo / "scripts/improve_loop_wrapper.py").write_text("upstream-wrapper")
    (repo / "optional-skills/own.md").write_text("upstream-edge")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream = _git(repo, "rev-parse", "HEAD")

    # Local line diverges from the same base with an owned edge change.
    _git(repo, "checkout", "-q", base)
    (repo / "optional-skills/own.md").write_text("local-owned-edge")
    (repo / "cron/scheduler.py").write_text("local-scheduler-fix")
    (repo / "tests/cron/test_cron_script.py").write_text("local-scheduler-test")
    (repo / "plugins/hermes-bestplan/bestplan_ocr.py").write_text("local-ocr-fix")
    (repo / "scripts/improve_loop_wrapper.py").write_text("local-wrapper-fix")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local edge")
    local = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state" / "upstream-sync.json"
    preview = tmp_path / "preview"

    report = mu.build_preview(repo, upstream, local, state, preview)
    assert (preview / "agent/core.py").read_text() == "base-core"
    assert (preview / "optional-skills/own.md").read_text() == "local-owned-edge"
    assert (preview / "cron/scheduler.py").read_text() == "local-scheduler-fix"
    assert (preview / "tests/cron/test_cron_script.py").read_text() == "local-scheduler-test"
    assert (preview / "plugins/hermes-bestplan/bestplan_ocr.py").read_text() == "local-ocr-fix"
    assert (preview / "scripts/improve_loop_wrapper.py").read_text() == "local-wrapper-fix"
    assert "optional-skills/own.md" in report["owned_paths"]
    assert "plugins/hermes-bestplan/bestplan_ocr.py" in report["owned_paths"]
    assert "scripts/improve_loop_wrapper.py" in report["owned_paths"]
    _git(repo, "worktree", "remove", "--force", str(preview))


def test_build_preview_rejects_unowned_core_replacement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "agent").mkdir()
    (repo / "plugins/hermes-bestplan").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "agent/core.py").write_text("local-core")
    (repo / "plugins/hermes-bestplan/bestplan_ocr.py").write_text("local-ocr")
    (repo / "scripts/improve_loop_wrapper.py").write_text("local-wrapper")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local")
    local = _git(repo, "rev-parse", "HEAD")
    (repo / "agent/core.py").write_text("upstream-core")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "upstream")
    upstream = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(mu.ScopeViolation, match="agent/core.py"):
        mu.build_preview(repo, upstream, local, tmp_path / "state" / "sync.json", tmp_path / "preview")
