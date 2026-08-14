from __future__ import annotations

import hashlib
import errno
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent.bestplan_checks import (
    CheckReceipt,
    CheckSetReceipt,
    _command_digest,
    _domain_digest,
)
from agent.bestplan_contract import BoundCommand
from agent.bestplan_promotion import FrozenIntegration
from agent.bestplan_source import capture_source_snapshot, resolve_repo_identity


@pytest.fixture(autouse=True)
def _issue_test_landing_authority(monkeypatch):
    """Keep low-level Git tests focused behind the reviewed landing gate."""

    from agent import bestplan_local_git
    from agent.review_engine import _issue_landing_authorization

    real_land = bestplan_local_git.land_checked_integration

    def authorized_land(*, snapshot, integration, checks, authorization=None, **kwargs):
        if authorization is None:
            lock_path = str(
                Path(snapshot.repo.common_dir) / "test-bestplan-landing.lock"
            )
            lock_handle = open(lock_path, "a+b")
            authorization = _issue_landing_authorization(
                lock_handle=lock_handle,
                plan_id="local-git-test",
                review_job_id="local-git-review-test",
                target_digest="7" * 64,
                integration_oid=integration.integration_oid,
                check_receipt_digest=checks.receipt_digest,
                fencing_token=1,
                owner_pid=os.getpid(),
                owner_process_start_id="test-process-start",
                repository_id=snapshot.repo.repository_id,
                repository_effect_lock_path=lock_path,
            )
        return real_land(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            authorization=authorization,
            **kwargs,
        )

    monkeypatch.setattr(
        bestplan_local_git, "land_checked_integration", authorized_land,
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=None if env is None else {**os.environ, **env},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "BestPlan Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _integration_commit(repo: Path, *, path: str, content: bytes) -> tuple[str, str]:
    target = _git(repo, "rev-parse", "HEAD")
    index = repo.parent / f"index-{hashlib.sha256(path.encode()).hexdigest()[:8]}"
    env = {"GIT_INDEX_FILE": str(index)}
    _git(repo, "read-tree", target, env=env)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=content,
        check=True,
        capture_output=True,
    ).stdout.strip().decode("ascii")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},{path}",
        env=env,
    )
    tree = _git(repo, "write-tree", env=env)
    integration = subprocess.run(
        ["git", "commit-tree", tree, "-p", target],
        cwd=repo,
        input=b"BestPlan checked integration\n",
        check=True,
        capture_output=True,
    ).stdout.strip().decode("ascii")
    _git(
        repo,
        "update-ref",
        "refs/hermes-bestplan/integrations/local-test",
        integration,
    )
    index.unlink(missing_ok=True)
    return integration, tree


def _frozen(
    snapshot, integration_oid: str, tree_oid: str,
) -> tuple[FrozenIntegration, CheckSetReceipt, tuple[BoundCommand, ...]]:
    contract_digest = "a" * 64
    frozen = FrozenIntegration(
        plan_id="bp-local",
        approval_digest="b" * 64,
        contract_digest=contract_digest,
        source_snapshot_digest="c" * 64,
        target_ref="refs/heads/main",
        target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        tree_oid=tree_oid,
        ref_name="refs/hermes-bestplan/integrations/local-test",
        candidates=(),
        receipt_digest="d" * 64,
    )
    command = BoundCommand(
        identifier="pytest",
        executable="/usr/bin/true",
        executable_sha256="1" * 64,
        argv=(),
        logical_cwd="integration",
        env=(),
        inputs=(),
        cache=(),
        timeout_seconds=60,
        network_allowlist=(),
    )
    command_digest = _command_digest(command)
    empty_sha = hashlib.sha256(b"").hexdigest()
    output_body = {
        "integration_oid": integration_oid,
        "command_digest": command_digest,
        "exit_code": 0,
        "stdout_sha256": empty_sha,
        "stderr_sha256": empty_sha,
        "stdout_size": 0,
        "stderr_size": 0,
    }
    output_framed_sha256 = _domain_digest(
        b"hermes.bestplan.check-output.v1", output_body,
    )
    receipt_body = {
        **output_body,
        "command_id": command.identifier,
        "policy_digest": "2" * 64,
        "output_framed_sha256": output_framed_sha256,
        "pre_tree_digest": "3" * 64,
        "post_tree_digest": "4" * 64,
    }
    receipt = CheckReceipt(
        integration_oid=integration_oid,
        command_id=command.identifier,
        command_digest=command_digest,
        policy_digest="2" * 64,
        exit_code=0,
        stdout_sha256=empty_sha,
        stderr_sha256=empty_sha,
        stdout_size=0,
        stderr_size=0,
        output_framed_sha256=output_framed_sha256,
        pre_tree_digest="3" * 64,
        post_tree_digest="4" * 64,
        receipt_digest=_domain_digest(
            b"hermes.bestplan.check-receipt.v1", receipt_body,
        ),
    )
    set_body = {
        "integration_oid": integration_oid,
        "contract_digest": contract_digest,
        "ordered_receipts": [receipt.receipt_digest],
    }
    checks = CheckSetReceipt(
        integration_oid=integration_oid,
        contract_digest=contract_digest,
        ordered_receipts=(receipt,),
        receipt_digest=_domain_digest(
            b"hermes.bestplan.check-set.v1", set_body,
        ),
    )
    return frozen, checks, (command,)


def _snapshot(repo: Path):
    identity = resolve_repo_identity(str(repo))
    return capture_source_snapshot(identity, time.monotonic() + 20.0)


def _configure_local_remote(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "sebmarion", str(remote))
    _git(repo, "config", "branch.main.remote", "sebmarion")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")
    _git(repo, "push", "sebmarion", "HEAD:refs/heads/main")
    return remote


def _seed_bare_remote(repo: Path, tmp_path: Path, name: str) -> Path:
    remote = tmp_path / name
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repo, "push", str(remote), "HEAD:refs/heads/main")
    return remote


def _configure_split_remote(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    fetch_remote = _seed_bare_remote(repo, tmp_path, "fetch.git")
    push_remote = _seed_bare_remote(repo, tmp_path, "push.git")
    _git(repo, "remote", "add", "sebmarion", str(fetch_remote))
    _git(repo, "config", "remote.sebmarion.pushurl", str(push_remote))
    _git(repo, "config", "branch.main.remote", "sebmarion")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")
    return fetch_remote, push_remote


def _exact_push_arguments(remote: Path, integration_oid: str) -> tuple[str, ...]:
    return (
        "-c",
        "push.followTags=false",
        "-c",
        "push.recurseSubmodules=no",
        "-c",
        "push.gpgSign=false",
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--recurse-submodules=no",
        "--no-signed",
        "--no-push-option",
        str(remote),
        f"{integration_oid}:refs/heads/main",
    )


def test_local_main_fast_forward_uses_checked_commit_and_preserves_disjoint_dirty(tmp_path):
    from agent.bestplan_local_git import land_checked_integration

    repo = _repo(tmp_path)
    (repo / "notes.txt").write_bytes(b"keep me exact\n")
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"VALUE = 1\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    before_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    result = land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    assert result.old_oid == snapshot.head_oid
    assert result.new_oid == integration_oid
    assert result.check_receipt_digest == checks.receipt_digest
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(repo, "rev-parse", "HEAD") == integration_oid
    assert _git(repo, "rev-parse", "refs/heads/main") == integration_oid
    assert (repo / "src" / "feature.py").read_bytes() == b"VALUE = 1\n"
    assert (repo / "notes.txt").read_bytes() == b"keep me exact\n"
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    parents = _git(repo, "show", "-s", "--format=%P", integration_oid).split()
    assert parents == [snapshot.head_oid]


def test_local_main_fast_forward_preserves_disjoint_staged_and_unstaged_bytes(tmp_path):
    from agent.bestplan_local_git import land_checked_integration

    repo = _repo(tmp_path)
    (repo / "staged.txt").write_bytes(b"staged\n")
    _git(repo, "add", "staged.txt")
    (repo / "base.txt").write_bytes(b"base plus unstaged\n")
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"VALUE = 2\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    staged_blob = _git(repo, "rev-parse", ":staged.txt")

    land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    assert _git(repo, "rev-parse", ":staged.txt") == staged_blob
    assert (repo / "staged.txt").read_bytes() == b"staged\n"
    assert (repo / "base.txt").read_bytes() == b"base plus unstaged\n"
    assert _git(repo, "diff", "--cached", "--name-only") == "staged.txt"
    assert _git(repo, "diff", "--name-only") == "base.txt"


@pytest.mark.parametrize(
    ("dirty_path", "incoming_path"),
    [
        ("src/feature.py", "src/feature.py"),
        ("src", "src/feature.py"),
        ("src/feature.py/child", "src/feature.py"),
        ("SRC/other.py", "src/feature.py"),
        ("cafe\u0301/notes.py", "caf\u00e9/result.py"),
    ],
)
def test_local_main_fast_forward_rejects_dirty_exact_ancestor_and_alias_overlap(
    tmp_path, dirty_path, incoming_path,
):
    from agent.bestplan_local_git import LocalMainConflict, land_checked_integration

    repo = _repo(tmp_path)
    dirty = repo / dirty_path
    dirty.parent.mkdir(parents=True, exist_ok=True)
    if dirty_path == "src":
        dirty.mkdir()
        (dirty / "local.txt").write_text("dirty\n", encoding="utf-8")
    else:
        dirty.write_text("dirty\n", encoding="utf-8")
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path=incoming_path, content=b"incoming\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)

    with pytest.raises(LocalMainConflict, match="overlap"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )

    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid
    assert dirty.exists()


def test_local_main_fast_forward_rejects_check_or_target_drift_before_effect(tmp_path):
    from agent.bestplan_local_git import LocalMainProofStale, land_checked_integration

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"VALUE = 3\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    wrong_checks = CheckSetReceipt(
        integration_oid="f" * 40,
        contract_digest=checks.contract_digest,
        ordered_receipts=(),
        receipt_digest=checks.receipt_digest,
    )

    with pytest.raises(LocalMainProofStale, match="check"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=wrong_checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )
    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid

    (repo / "unrelated.txt").write_text("new head\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-m", "advance main")
    with pytest.raises(LocalMainProofStale, match="target"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )


def test_local_main_fast_forward_never_overwrites_ignored_incoming_path(tmp_path):
    from agent.bestplan_local_git import LocalMainConflict, land_checked_integration

    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("src/feature.py\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore local generated file")
    ignored = repo / "src" / "feature.py"
    ignored.parent.mkdir()
    ignored.write_bytes(b"local ignored bytes\n")
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked incoming bytes\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)

    with pytest.raises(LocalMainConflict, match="overlap"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )

    assert ignored.read_bytes() == b"local ignored bytes\n"
    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid


def test_local_main_rejects_fabricated_or_empty_check_set_before_effect(tmp_path):
    from agent.bestplan_local_git import LocalMainProofStale, land_checked_integration

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    fabricated = CheckSetReceipt(
        integration_oid=checks.integration_oid,
        contract_digest=checks.contract_digest,
        ordered_receipts=(),
        receipt_digest="f" * 64,
    )

    with pytest.raises(LocalMainProofStale, match="check"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=fabricated,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )

    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid


def test_local_main_rejects_newly_activated_git_filter_before_effect(tmp_path):
    from agent.bestplan_local_git import LocalMainProofStale, land_checked_integration

    repo = _repo(tmp_path)
    marker = tmp_path / "filter-ran"
    driver = tmp_path / "smudge-filter.sh"
    driver.write_text(
        "#!/bin/sh\nprintf ran > " + str(marker) + "\ncat\n",
        encoding="utf-8",
    )
    driver.chmod(0o700)
    (repo / ".gitattributes").write_text(
        "*.txt filter=bestplan-test\n", encoding="utf-8",
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "add local filter attributes")
    snapshot = _snapshot(repo)
    _git(repo, "config", "filter.bestplan-test.clean", "cat")
    _git(repo, "config", "filter.bestplan-test.smudge", str(driver))
    integration_oid, tree_oid = _integration_commit(
        repo, path="incoming.txt", content=b"candidate bytes\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)

    with pytest.raises(LocalMainProofStale, match="protected state"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )

    assert not marker.exists()
    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid


def test_local_main_never_invokes_repository_fsmonitor(tmp_path):
    from agent.bestplan_local_git import land_checked_integration

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    marker = tmp_path / "fsmonitor-ran"
    driver = tmp_path / "fsmonitor.sh"
    driver.write_text(
        "#!/bin/sh\nprintf ran > " + str(marker) + "\nprintf '\\n'\n",
        encoding="utf-8",
    )
    driver.chmod(0o700)
    _git(repo, "config", "core.fsmonitor", str(driver))
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    marker.unlink(missing_ok=True)

    land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    assert not marker.exists()
    assert _git(repo, "rev-parse", "HEAD") == integration_oid


def test_local_main_rejects_ignore_rule_change_before_reclassifying_dirty_file(
    tmp_path,
):
    from agent.bestplan_local_git import LocalMainConflict, land_checked_integration

    repo = _repo(tmp_path)
    local = repo / "notes.log"
    local.write_bytes(b"keep local log\n")
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path=".gitignore", content=b"*.log\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)

    with pytest.raises(LocalMainConflict, match="ignore"):
        land_checked_integration(
            snapshot=snapshot,
            integration=integration,
            checks=checks,
            commands=commands,
            deadline=time.monotonic() + 20.0,
        )

    assert local.read_bytes() == b"keep local log\n"
    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid


def test_local_main_effect_is_not_run_through_the_deadline_killing_git_helper(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    real_run_git = local_git._run_git
    merge_calls: list[tuple[str, ...]] = []

    def observed_run_git(repo_identity, *args, **kwargs):
        if "merge" in args:
            merge_calls.append(tuple(args))
        return real_run_git(repo_identity, *args, **kwargs)

    monkeypatch.setattr(local_git, "_run_git", observed_run_git)
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    assert merge_calls == []
    assert _git(repo, "rev-parse", "HEAD") == integration_oid


def test_reaped_local_main_effect_never_signals_numeric_process_group(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    wait_calls: list[int] = []
    signal_calls: list[tuple[object, ...]] = []

    class ReapedProcess:
        pid = 424_242

        def wait(self):
            wait_calls.append(self.pid)
            return 0

    process = ReapedProcess()

    def fake_popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        assert "core.hooksPath=/dev/null" in command
        assert "filter.lfs.required=false" in command
        return process

    monkeypatch.setattr(local_git.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        local_git.os,
        "killpg",
        lambda pid, value: signal_calls.append(("killpg", pid, value)),
    )
    monkeypatch.setattr(
        local_git,
        "terminate_process_group",
        lambda value, **kwargs: signal_calls.append(
            ("terminate_process_group", value, kwargs),
        ),
        raising=False,
    )

    result = local_git._run_uninterruptible_git_effect(
        snapshot,
        ("merge", "--ff-only", "deadbeef"),
    )

    assert result.returncode == 0
    assert wait_calls == [process.pid]
    assert signal_calls == []


def test_prelanding_push_target_is_the_only_observer_and_binds_remote_identity(
    tmp_path,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    remote = _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )

    assert not hasattr(local_git, "observe_local_main_push_target")
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )

    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid
    assert target.remote_name == "sebmarion"
    assert target.remote_ref == "refs/heads/main"
    assert target.observed_remote_oid == snapshot.head_oid
    assert target.integration_oid == integration_oid
    assert target.display_url == str(remote)
    assert str(remote) not in target.remote_identity_sha256


def test_prelanding_push_target_is_bound_before_local_main_moves(tmp_path):
    from agent.bestplan_local_git import (
        classify_local_main_for_push,
        classify_local_push_remote,
        observe_prelanding_local_main_push_target,
    )

    repo = _repo(tmp_path)
    _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )

    target = observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )

    assert _git(repo, "rev-parse", "HEAD") == snapshot.head_oid
    assert target.integration_oid == integration_oid
    assert target.observed_remote_oid == snapshot.head_oid
    assert classify_local_main_for_push(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    ) == "expected"
    assert classify_local_push_remote(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    ) == "observed"


def test_push_recovery_classifiers_distinguish_effect_and_drift(tmp_path):
    from agent.bestplan_local_git import (
        classify_local_main_for_push,
        classify_local_push_remote,
        land_checked_integration,
        observe_prelanding_local_main_push_target,
        push_exact_local_main,
    )

    repo = _repo(tmp_path)
    remote = _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    target = observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    assert classify_local_main_for_push(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    ) == "integration"
    push_exact_local_main(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    )
    assert classify_local_push_remote(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    ) == "integration"

    unrelated_oid, _unrelated_tree = _integration_commit(
        repo, path="src/remote-only.py", content=b"drift\n",
    )
    _git(repo, "push", "sebmarion", f"{unrelated_oid}:refs/heads/main")
    assert _git(remote, "rev-parse", "refs/heads/main") == unrelated_oid
    assert classify_local_push_remote(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    ) == "other"


def test_remote_observation_forwards_only_ssh_credential_context(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/test-agent.sock")
    monkeypatch.setenv("GIT_ASKPASS", "/private/tmp/secret-askpass")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-forward")
    real_credential_environment = local_git._remote_credential_environment
    observed: list[dict[str, str]] = []

    def checked_credential_environment():
        extra = real_credential_environment()
        observed.append(extra)
        assert extra.get("SSH_AUTH_SOCK") == "/private/tmp/test-agent.sock"
        assert "GIT_ASKPASS" not in extra
        assert "AWS_SECRET_ACCESS_KEY" not in extra
        return extra

    monkeypatch.setattr(
        local_git,
        "_remote_credential_environment",
        checked_credential_environment,
    )
    local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )

    assert len(observed) == 1


def test_distinct_fetch_and_push_urls_use_push_endpoint_for_read_effect_and_readback(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    fetch_remote, push_remote = _configure_split_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    _git(
        repo,
        "push",
        str(fetch_remote),
        f"{integration_oid}:refs/heads/main",
    )
    integration, checks, commands = _frozen(
        snapshot, integration_oid, tree_oid,
    )
    real_remote_command = local_git._run_bounded_remote_command
    observed_reads: list[str] = []

    def observed_remote_command(command, **kwargs):
        if "ls-remote" in command:
            observed_reads.append(str(command[-2]))
        return real_remote_command(command, **kwargs)

    monkeypatch.setattr(
        local_git, "_run_bounded_remote_command", observed_remote_command,
    )
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )
    real_effect = local_git._run_push_effect
    observed_effects: list[tuple[str, ...]] = []

    def observed_effect(snapshot_value, arguments, **kwargs):
        observed_effects.append(tuple(arguments))
        return real_effect(snapshot_value, arguments, **kwargs)

    monkeypatch.setattr(local_git, "_run_push_effect", observed_effect)
    receipt = local_git.push_exact_local_main(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    )

    assert target.observed_remote_oid == snapshot.head_oid
    assert receipt.remote_oid == integration_oid
    assert observed_reads == [str(push_remote)] * 3
    assert observed_effects == [_exact_push_arguments(push_remote, integration_oid)]
    assert _git(push_remote, "rev-parse", "refs/heads/main") == integration_oid


def test_prelanding_push_target_rejects_multiple_push_urls_before_observation(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    primary = _configure_local_remote(repo, tmp_path)
    second = _seed_bare_remote(repo, tmp_path, "second-push.git")
    _git(repo, "config", "--add", "remote.sebmarion.pushurl", str(primary))
    _git(repo, "config", "--add", "remote.sebmarion.pushurl", str(second))
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    observed_reads: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        local_git,
        "_read_remote_oid",
        lambda *args, **kwargs: observed_reads.append((args, kwargs))
        or snapshot.head_oid,
    )

    with pytest.raises(local_git.LocalPushStale, match="URL|exactly one|single"):
        local_git.observe_prelanding_local_main_push_target(
            snapshot=snapshot,
            expected_target_oid=snapshot.head_oid,
            integration_oid=integration_oid,
            deadline=time.monotonic() + 20.0,
        )

    assert observed_reads == []
    assert _git(primary, "rev-parse", "refs/heads/main") == snapshot.head_oid
    assert _git(second, "rev-parse", "refs/heads/main") == snapshot.head_oid


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "https://user:secret@example.invalid/project.git",
        "https://token@example.invalid/project.git",
        "https://example.invalid/project.git?access_token=secret",
        "https://example.invalid/project.git#secret",
    ),
)
def test_prelanding_push_target_rejects_credential_bearing_url_before_observation(
    tmp_path,
    monkeypatch,
    unsafe_url,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    _configure_local_remote(repo, tmp_path)
    _git(repo, "config", "remote.sebmarion.pushurl", unsafe_url)
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    observed_reads: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        local_git,
        "_read_remote_oid",
        lambda *args, **kwargs: observed_reads.append((args, kwargs))
        or snapshot.head_oid,
    )

    with pytest.raises(local_git.LocalPushStale) as captured:
        local_git.observe_prelanding_local_main_push_target(
            snapshot=snapshot,
            expected_target_oid=snapshot.head_oid,
            integration_oid=integration_oid,
            deadline=time.monotonic() + 20.0,
        )

    assert "secret" not in str(captured.value)
    assert "token" not in str(captured.value)
    assert observed_reads == []


def test_remote_read_ignores_repo_local_url_rewrites_after_endpoint_capture(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    approved_remote = _configure_local_remote(repo, tmp_path)
    replacement_remote = _seed_bare_remote(
        repo, tmp_path, "read-replacement.git",
    )
    snapshot = _snapshot(repo)
    integration_oid, _tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    _git(
        repo,
        "push",
        str(replacement_remote),
        f"{integration_oid}:refs/heads/main",
    )
    real_target_config = local_git._remote_target_config

    def captured_then_rewritten(snapshot_value, **kwargs):
        configured = real_target_config(snapshot_value, **kwargs)
        _git(
            repo,
            "config",
            f"url.{replacement_remote}.insteadOf",
            str(approved_remote),
        )
        _git(
            repo,
            "config",
            f"url.{replacement_remote}.pushInsteadOf",
            str(approved_remote),
        )
        return configured

    monkeypatch.setattr(
        local_git, "_remote_target_config", captured_then_rewritten,
    )
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )

    assert target.observed_remote_oid == snapshot.head_oid
    assert _git(approved_remote, "rev-parse", "refs/heads/main") == snapshot.head_oid
    assert (
        _git(replacement_remote, "rev-parse", "refs/heads/main")
        == integration_oid
    )


def test_exact_local_main_push_is_normal_nonforce_and_verified(tmp_path, monkeypatch):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    remote = _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )
    real_effect = local_git._run_push_effect
    observed: list[tuple[str, ...]] = []

    def observed_effect(snapshot_value, arguments, **kwargs):
        observed.append(tuple(arguments))
        return real_effect(snapshot_value, arguments, **kwargs)

    monkeypatch.setattr(local_git, "_run_push_effect", observed_effect)
    receipt = local_git.push_exact_local_main(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    )

    assert receipt.integration_oid == integration_oid
    assert receipt.remote_oid == integration_oid
    assert len(observed) == 1
    assert observed[0] == _exact_push_arguments(remote, integration_oid)
    assert all("--force" not in item and not item.startswith("+") for item in observed[0])
    assert _git(remote, "rev-parse", "refs/heads/main") == integration_oid


def test_exact_push_isolated_from_repo_local_rewrites_and_push_expansion(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    approved_remote = _configure_local_remote(repo, tmp_path)
    replacement_remote = _seed_bare_remote(
        repo, tmp_path, "push-replacement.git",
    )
    push_option_marker = tmp_path / "push-option-used"
    _git(
        approved_remote,
        "config",
        "receive.advertisePushOptions",
        "true",
    )
    hook = approved_remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "if test \"${GIT_PUSH_OPTION_COUNT:-0}\" != 0; then\n"
        f"  : > {push_option_marker}\n"
        "fi\n"
        "cat >/dev/null\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    _git(repo, "tag", "-a", "unapproved-tag", integration_oid, "-m", "tag")
    integration, checks, commands = _frozen(
        snapshot, integration_oid, tree_oid,
    )
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )
    real_effect = local_git._run_push_effect
    observed_effects: list[tuple[str, ...]] = []

    def configure_then_run(snapshot_value, arguments, **kwargs):
        observed_effects.append(tuple(arguments))
        _git(
            repo,
            "config",
            f"url.{replacement_remote}.pushInsteadOf",
            str(approved_remote),
        )
        _git(repo, "config", "push.followTags", "true")
        _git(repo, "config", "push.recurseSubmodules", "only")
        _git(repo, "config", "push.gpgSign", "true")
        _git(repo, "config", "push.pushOption", "unapproved")
        return real_effect(snapshot_value, arguments, **kwargs)

    monkeypatch.setattr(local_git, "_run_push_effect", configure_then_run)
    receipt = local_git.push_exact_local_main(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    )

    assert receipt.remote_oid == integration_oid
    assert len(observed_effects) == 1
    arguments = observed_effects[0]
    assert "--no-follow-tags" in arguments
    assert "--recurse-submodules=no" in arguments
    assert "--no-signed" in arguments
    assert "--no-push-option" in arguments
    assert _git(approved_remote, "rev-parse", "refs/heads/main") == integration_oid
    assert (
        _git(replacement_remote, "rev-parse", "refs/heads/main")
        == snapshot.head_oid
    )
    assert _git(
        approved_remote,
        "for-each-ref",
        "--format=%(refname)",
        "refs/tags",
    ) == ""
    assert not push_option_marker.exists()


def test_exact_push_uses_captured_endpoint_after_remote_name_swap(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    approved_remote = _configure_local_remote(repo, tmp_path)
    replacement_remote = _seed_bare_remote(
        repo, tmp_path, "replacement.git",
    )
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(
        snapshot, integration_oid, tree_oid,
    )
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )
    real_effect = local_git._run_push_effect
    observed_effects: list[tuple[str, ...]] = []

    def swap_then_run(snapshot_value, arguments, **kwargs):
        observed_effects.append(tuple(arguments))
        _git(repo, "config", "remote.sebmarion.url", str(replacement_remote))
        _git(
            repo,
            "config",
            "remote.sebmarion.pushurl",
            str(replacement_remote),
        )
        return real_effect(snapshot_value, arguments, **kwargs)

    monkeypatch.setattr(local_git, "_run_push_effect", swap_then_run)
    receipt = local_git.push_exact_local_main(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    )

    assert receipt.remote_oid == integration_oid
    assert observed_effects == [
        _exact_push_arguments(approved_remote, integration_oid)
    ]
    assert (
        _git(approved_remote, "rev-parse", "refs/heads/main")
        == integration_oid
    )
    assert (
        _git(replacement_remote, "rev-parse", "refs/heads/main")
        == snapshot.head_oid
    )


def test_exact_local_main_push_bounds_stuck_effect_and_leaves_reconcilable_state(
    tmp_path, monkeypatch,
):
    from agent import bestplan_local_git as local_git

    repo = _repo(tmp_path)
    _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    target = local_git.observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    local_git.land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )

    real_popen = local_git.subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []
    started = threading.Event()
    rescue_used = threading.Event()
    helper_ready = tmp_path / "stuck-push-ready"

    def stuck_push_popen(command, *args, **kwargs):
        if "push" not in command:
            return real_popen(command, *args, **kwargs)
        helper = (
            "import os,signal,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as stream:\n"
            "    stream.write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )
        process = real_popen(
            [sys.executable, "-c", helper, str(helper_ready)],
            *args,
            **kwargs,
        )
        spawned.append(process)
        started.set()
        return process

    monkeypatch.setattr(local_git.subprocess, "Popen", stuck_push_popen)
    call_started = time.monotonic()
    deadline = call_started + 2.0

    def rescue_stuck_process() -> None:
        if not started.wait(timeout=2.0):
            return
        while time.monotonic() < deadline + 0.5:
            time.sleep(0.01)
        process = spawned[0]
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        rescue_used.set()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    rescue = threading.Thread(target=rescue_stuck_process, daemon=True)
    rescue.start()
    effect_finished = math.inf
    try:
        with pytest.raises(
            local_git.LocalPushEffectUnknown, match="outcome|deadline|unknown",
        ):
            local_git.push_exact_local_main(
                snapshot=snapshot,
                target=target,
                deadline=deadline,
            )
        effect_finished = time.monotonic()
    finally:
        rescue.join(timeout=3.0)
        if rescue.is_alive() and spawned:
            try:
                os.killpg(spawned[0].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert started.is_set()
    assert helper_ready.is_file()
    assert effect_finished <= deadline + 0.25
    assert not rescue_used.is_set()
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(spawned[0].pid, 0)

    monkeypatch.setattr(local_git.subprocess, "Popen", real_popen)
    assert local_git.classify_local_push_remote(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    ) == "observed"
    _git(repo, "push", "sebmarion", f"{integration_oid}:refs/heads/main")
    assert local_git.classify_local_push_remote(
        snapshot=snapshot,
        target=target,
        deadline=time.monotonic() + 20.0,
    ) == "integration"


def test_unproved_push_extinction_retains_private_control_and_closes_fds(
    tmp_path,
    monkeypatch,
):
    from agent import bestplan_local_git as local_git
    from agent import bestplan_source as source_boundary

    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    real_mkdtemp = local_git.tempfile.mkdtemp
    real_create = local_git._create_push_control_repo
    roots: list[Path] = []
    controls = []

    def private_mkdtemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        root = Path(real_mkdtemp(*args, **kwargs))
        roots.append(root)
        return str(root)

    def captured_create(*args, **kwargs):
        control = real_create(*args, **kwargs)
        controls.append(control)
        return control

    class CompletedLeader:
        pid = 987_654
        returncode = 0

        def poll(self):
            return self.returncode

    def fake_popen(*args, **kwargs):
        return CompletedLeader()

    def unknown_process_group(*args, **kwargs):
        raise OSError(errno.EIO, "process-group state is unavailable")

    monkeypatch.setattr(local_git.tempfile, "mkdtemp", private_mkdtemp)
    monkeypatch.setattr(local_git, "_create_push_control_repo", captured_create)
    monkeypatch.setattr(local_git.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(local_git.os, "killpg", unknown_process_group)

    with pytest.raises(
        local_git.LocalPushEffectUnknown, match="extinction|unknown",
    ):
        local_git._run_push_effect(
            snapshot,
            _exact_push_arguments(tmp_path / "unused.git", "a" * 40),
            deadline=time.monotonic() + 20.0,
        )

    assert len(roots) == 1
    assert len(controls) == 1
    root = roots[0]
    control = controls[0]
    try:
        assert root.is_dir()
        assert root.stat().st_mode & 0o777 == 0o700
        for descriptor in (
            control.source_objects_fd,
            control.root_fd,
            control.parent_fd,
        ):
            with pytest.raises(OSError) as captured:
                os.fstat(descriptor)
            assert captured.value.errno == errno.EBADF
    finally:
        if root.is_dir():
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                source_boundary._remove_owned_tree_contents(
                    descriptor, deadline=time.monotonic() + 20.0,
                )
            finally:
                os.close(descriptor)
            root.rmdir()


def test_exact_local_main_push_rejects_remote_drift_before_effect(tmp_path):
    from agent.bestplan_local_git import (
        LocalPushStale,
        land_checked_integration,
        observe_prelanding_local_main_push_target,
        push_exact_local_main,
    )

    repo = _repo(tmp_path)
    remote = _configure_local_remote(repo, tmp_path)
    snapshot = _snapshot(repo)
    integration_oid, tree_oid = _integration_commit(
        repo, path="src/feature.py", content=b"checked\n",
    )
    integration, checks, commands = _frozen(snapshot, integration_oid, tree_oid)
    target = observe_prelanding_local_main_push_target(
        snapshot=snapshot,
        expected_target_oid=snapshot.head_oid,
        integration_oid=integration_oid,
        deadline=time.monotonic() + 20.0,
    )
    land_checked_integration(
        snapshot=snapshot,
        integration=integration,
        checks=checks,
        commands=commands,
        deadline=time.monotonic() + 20.0,
    )
    advanced_oid, _advanced_tree = _integration_commit(
        repo, path="src/remote.py", content=b"advanced\n",
    )
    _git(repo, "push", "sebmarion", f"{advanced_oid}:refs/heads/main")

    with pytest.raises(LocalPushStale, match="remote"):
        push_exact_local_main(
            snapshot=snapshot,
            target=target,
            deadline=time.monotonic() + 20.0,
        )

    assert _git(remote, "rev-parse", "refs/heads/main") == advanced_oid
