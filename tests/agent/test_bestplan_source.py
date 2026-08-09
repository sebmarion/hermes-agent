from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent.execution_plan import compile_execution_plan


def _source():
    return importlib.import_module("agent.bestplan_source")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
    ).stdout.rstrip(b"\n")


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_bytes(b"committed\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _snapshot(repo: Path):
    source = _source()
    identity = source.resolve_repo_identity(str(repo))
    return source.capture_source_snapshot(identity, time.monotonic() + 5.0)


def _set_head_to_flat_tree(repo: Path, files: list[tuple[bytes, bytes]]) -> None:
    records: list[bytes] = []
    repo_raw = os.fsencode(repo)
    for path, content in sorted(files):
        oid = subprocess.run(
            [b"git", b"hash-object", b"-w", b"--stdin"],
            cwd=repo_raw,
            input=content,
            check=True,
            capture_output=True,
        ).stdout.removesuffix(b"\n")
        records.append(b"100644 blob " + oid + b"\t" + path + b"\0")
    tree_oid = subprocess.run(
        [b"git", b"mktree", b"-z"],
        cwd=repo_raw,
        input=b"".join(records),
        check=True,
        capture_output=True,
    ).stdout.removesuffix(b"\n")
    commit_oid = subprocess.run(
        [b"git", b"commit-tree", tree_oid],
        cwd=repo_raw,
        input=b"synthetic tree\n",
        check=True,
        capture_output=True,
    ).stdout.removesuffix(b"\n")
    subprocess.run(
        [b"git", b"update-ref", b"HEAD", commit_oid],
        cwd=repo_raw,
        check=True,
        capture_output=True,
    )


def _plan(workspace: str):
    return compile_execution_plan({
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": [{
            "id": "work",
            "kind": "implement",
            "goal": "Implement safely",
            "depends_on": [],
            "capability": "fast_fallback",
            "workspace": workspace,
            "allowed_paths": ["tracked.txt"],
            "read_only": False,
            "expected_artifacts": ["tracked.txt"],
            "acceptance": ["tests pass"],
        }],
        "merge_policy": "Verify before integration.",
        "stop_condition": "Acceptance passes.",
        "escalation_predicates": ["verification_failed"],
    })


def test_source_boundary_module_exposes_the_trusted_primitives():
    source = _source()
    assert {
        "resolve_repo_identity",
        "capture_source_snapshot",
        "capture_protected_manifest",
        "assert_supported_repository",
        "recapture_matches",
        "export_exact_tree",
    } <= set(dir(source))


def test_ignored_runtime_trees_do_not_block_or_get_recursively_scanned(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    ignored_names = [
        ".bytecode-fingerprint",
        "node_modules",
        ".venv",
        "venv",
        ".gitnexus",
    ]
    (repo / ".gitignore").write_text(
        ".bytecode-fingerprint\nnode_modules/\n.venv/\nvenv/\n.gitnexus/\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore runtime trees")
    (repo / ".bytecode-fingerprint").write_bytes(b"runtime-only")
    for name in ignored_names[1:]:
        nested = repo / name / "deep" / "cache"
        nested.mkdir(parents=True)
        (nested / "payload.bin").write_bytes(b"ignored")

    blocked = {os.fsencode(repo / name) for name in ignored_names[1:]}
    real_scandir = source.os.scandir

    def guarded_scandir(path):
        raw = os.fsencode(path)
        if raw in blocked or any(raw.startswith(item + os.sep.encode()) for item in blocked):
            raise AssertionError(f"ignored tree was enumerated: {os.fsdecode(raw)}")
        return real_scandir(path)

    monkeypatch.setattr(source.os, "scandir", guarded_scandir)
    first = source.capture_source_snapshot(
        source.resolve_repo_identity(str(repo)), time.monotonic() + 5.0,
    )
    (repo / ".bytecode-fingerprint").write_bytes(b"changed but ignored")
    second = source.capture_source_snapshot(
        source.resolve_repo_identity(str(repo)), time.monotonic() + 5.0,
    )
    assert second.fingerprint == first.fingerprint


def test_special_file_scan_batches_ignore_queries_by_depth(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    for index in range(32):
        directory = repo / f"package-{index}" / "src"
        directory.mkdir(parents=True)
        (directory / "module.py").write_bytes(b"pass\n")

    calls = 0
    real_run_git = source._run_git

    def counted_run_git(cwd, *args, **kwargs):
        nonlocal calls
        if args and args[0] == "check-ignore":
            calls += 1
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(source, "_run_git", counted_run_git)
    source.capture_protected_manifest(source.resolve_repo_identity(str(repo)))
    assert calls <= 4


def test_snapshot_records_repo_head_ref_common_dir_and_full_oid(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()

    identity = source.resolve_repo_identity(str(nested))
    snapshot = source.capture_source_snapshot(identity, time.monotonic() + 5.0)
    expected = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")

    assert identity.worktree_raw == os.fsencode(repo.resolve())
    assert identity.common_dir_raw == os.fsencode((repo / ".git").resolve())
    assert identity.repository_id
    assert snapshot.head_oid == expected
    assert snapshot.head_ref == b"refs/heads/" + _git(repo, "branch", "--show-current")
    assert snapshot.head_symbolic is True
    assert snapshot.head_raw == b"ref: " + snapshot.head_ref + b"\n"


def test_snapshot_fingerprint_binds_linked_worktree_and_identity_fields(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    first_worktree = tmp_path / "linked-one"
    second_worktree = tmp_path / "linked-two"
    _git(repo, "worktree", "add", "-q", "--detach", str(first_worktree), "HEAD")
    _git(repo, "worktree", "add", "-q", "--detach", str(second_worktree), "HEAD")

    first = _snapshot(first_worktree)
    second = _snapshot(second_worktree)
    assert first.repo.common_dir_raw == second.repo.common_dir_raw
    assert first.repo.git_dir_raw != second.repo.git_dir_raw
    assert first.head_raw == second.head_raw
    assert first.protected_manifest == second.protected_manifest
    assert first.fingerprint != second.fingerprint

    read = source._head_read(first.repo, deadline=time.monotonic() + 5.0)
    baseline = source._snapshot_fingerprint(first.repo, read)
    substitutions = [
        replace(first.repo, worktree_raw=first.repo.worktree_raw + b"-other"),
        replace(first.repo, git_dir_raw=first.repo.git_dir_raw + b"-other"),
        replace(first.repo, common_dir_raw=first.repo.common_dir_raw + b"-other"),
        replace(first.repo, common_dir_device=first.repo.common_dir_device + 1),
        replace(first.repo, common_dir_inode=first.repo.common_dir_inode + 1),
    ]
    assert all(
        source._snapshot_fingerprint(substitute, read) != baseline
        for substitute in substitutions
    )


def test_create_plan_persists_the_exact_captured_head_oid(tmp_path):
    from agent.bestplan_state import BestplanStore

    repo = _init_repo(tmp_path / "repo")
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    plan_id = store.create_plan(
        "do it",
        _plan(str(repo)),
        session_id="s1",
        profile="coder",
        workspace=str(repo),
    )

    assert store.get_plan(plan_id)["baseline_revision"] == _git(
        repo, "rev-parse", "--verify", "HEAD^{commit}",
    ).decode("ascii")


def test_create_plan_distinguishes_trusted_capture_from_legacy_injection(tmp_path):
    source = _source()
    from agent.bestplan_state import BaselineFingerprintError, BestplanStore

    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")

    trusted_id = store.create_plan(
        "trusted",
        _plan(str(repo)),
        session_id="trusted",
        workspace=str(repo),
        baseline_fingerprint=snapshot.fingerprint,
    )
    trusted = store.get_plan(trusted_id)
    assert trusted["baseline_fingerprint"] == snapshot.fingerprint
    assert trusted["baseline_revision"] == snapshot.head_oid

    with pytest.raises(BaselineFingerprintError, match="does not match"):
        store.create_plan(
            "mismatch",
            _plan(str(repo)),
            session_id="mismatch",
            workspace=str(repo),
            baseline_fingerprint="arbitrary-legacy-value",
        )

    missing = tmp_path / "non-git-legacy-fixture"
    legacy_id = store.create_plan(
        "legacy",
        _plan(str(missing)),
        session_id="legacy",
        workspace=str(missing),
        baseline_fingerprint="synthetic-test-baseline",
    )
    legacy = store.get_plan(legacy_id)
    assert legacy["baseline_fingerprint"] == "synthetic-test-baseline"
    assert legacy["baseline_revision"] is None
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_plans WHERE session_id = 'mismatch'"
    ).fetchone()[0] == 0


def test_protected_manifest_is_deterministic_and_binds_dirty_index_and_worktree(
    tmp_path,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "assumed.txt").write_bytes(b"assumed\n")
    (repo / "sparse.txt").write_bytes(b"sparse\n")
    _git(repo, "add", "assumed.txt", "sparse.txt")
    _git(repo, "commit", "-qm", "flagged files")

    (repo / "tracked.txt").write_bytes(b"staged\n")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_bytes(b"unstaged\n")
    (repo / "untracked.txt").write_bytes(b"ambient\n")
    (repo / "ambient-link").symlink_to("untracked.txt")
    _git(repo, "update-index", "--assume-unchanged", "assumed.txt")
    _git(repo, "update-index", "--skip-worktree", "sparse.txt")

    identity = source.resolve_repo_identity(str(repo))
    first = source.capture_protected_manifest(identity)
    second = source.capture_protected_manifest(identity)
    assert first == second

    worktree = {entry.path: entry for entry in first.worktree_entries}
    assert worktree[b"tracked.txt"].kind == "regular"
    assert worktree[b"tracked.txt"].content_sha256 == hashlib.sha256(
        b"unstaged\n"
    ).hexdigest()
    assert worktree[b"ambient-link"].kind == "symlink"
    assert worktree[b"ambient-link"].symlink_target == b"untracked.txt"
    assert worktree[b"untracked.txt"].content_sha256 == hashlib.sha256(
        b"ambient\n"
    ).hexdigest()

    flags = {entry.path: entry for entry in first.index_flags}
    assert flags[b"assumed.txt"].assume_unchanged is True
    assert flags[b"sparse.txt"].skip_worktree is True
    staged = [entry for entry in first.index_entries if entry.path == b"tracked.txt"]
    assert len(staged) == 1
    assert staged[0].oid == _git(repo, "rev-parse", ":tracked.txt").decode("ascii")


def test_protected_manifest_separates_staged_and_unstaged_binary_state(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    binary = repo / "binary.dat"
    binary.write_bytes(b"committed\x00bytes")
    _git(repo, "add", "binary.dat")
    _git(repo, "commit", "-qm", "binary base")
    binary.write_bytes(b"staged\x00bytes")
    _git(repo, "add", "binary.dat")
    binary.write_bytes(b"unstaged\x00bytes")

    identity = source.resolve_repo_identity(str(repo))
    first = source.capture_protected_manifest(identity)
    assert first.protected_paths == tuple(sorted(set(first.protected_paths)))
    assert b"binary.dat" in first.protected_paths
    assert first.staged_diff_sha256 != first.unstaged_diff_sha256
    assert first.digest

    binary.write_bytes(b"second unstaged\x00bytes")
    second = source.capture_protected_manifest(identity)
    assert second.staged_diff_sha256 == first.staged_diff_sha256
    assert second.unstaged_diff_sha256 != first.unstaged_diff_sha256
    assert second.digest != first.digest


def test_protected_paths_only_block_dirty_untracked_and_special_index_state(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    raw_tracked = b"tracked-line\n-tab\t.txt"
    raw_untracked = b"untracked-line\n-tab\t.txt"
    repo_raw = os.fsencode(repo)
    fd = os.open(
        os.path.join(repo_raw, raw_tracked),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        os.write(fd, b"committed\n")
    finally:
        os.close(fd)
    subprocess.run(
        [b"git", b"add", b"--", raw_tracked],
        cwd=repo_raw,
        check=True,
        capture_output=True,
    )
    _git(repo, "commit", "-qm", "raw tracked path")

    identity = source.resolve_repo_identity(str(repo))
    clean = source.capture_protected_manifest(identity)
    assert b"tracked.txt" not in clean.protected_paths
    assert raw_tracked not in clean.protected_paths

    fd = os.open(os.path.join(repo_raw, raw_tracked), os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, b"unstaged\n")
    finally:
        os.close(fd)
    unstaged = source.capture_protected_manifest(identity)
    assert raw_tracked in unstaged.protected_paths

    subprocess.run(
        [b"git", b"add", b"--", raw_tracked],
        cwd=repo_raw,
        check=True,
        capture_output=True,
    )
    staged = source.capture_protected_manifest(identity)
    assert raw_tracked in staged.protected_paths

    fd = os.open(
        os.path.join(repo_raw, raw_untracked),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        os.write(fd, b"ambient\n")
    finally:
        os.close(fd)
    with_untracked = source.capture_protected_manifest(identity)
    assert raw_untracked in with_untracked.protected_paths


def test_manifest_pins_raw_staged_and_unstaged_diff_semantics(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_bytes(b"changed\n")
    calls: list[tuple[str, ...]] = []
    real_run_git = source._run_git

    def recording_run_git(cwd, *args, **kwargs):
        if args and args[0] == "diff":
            calls.append(args)
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(source, "_run_git", recording_run_git)
    source.capture_protected_manifest(source.resolve_repo_identity(str(repo)))

    assert len(calls) >= 4
    proof_calls = [args for args in calls if "--binary" in args]
    name_calls = [args for args in calls if "--name-only" in args]
    assert len(proof_calls) == 2
    assert len(name_calls) == 2
    for args in [*proof_calls, *name_calls]:
        assert "--no-ext-diff" in args
        assert "--no-textconv" in args
        assert "--no-renames" in args
        assert "--no-color" in args
    for args in proof_calls:
        assert "--full-index" in args
    for args in name_calls:
        assert "-z" in args


def test_capture_accepts_a_b_b_as_two_consecutive_identical_reads(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    stable_read = source._head_read(identity, deadline=time.monotonic() + 5.0)
    manifest_a = replace(stable_read.protected_manifest, digest="a" * 64)
    manifest_b = replace(stable_read.protected_manifest, digest="b" * 64)
    reads = iter([
        replace(stable_read, protected_manifest=manifest_a),
        replace(stable_read, protected_manifest=manifest_b),
        replace(stable_read, protected_manifest=manifest_b),
    ])
    calls = 0

    def sequenced_read(_repo, *, deadline):
        nonlocal calls
        calls += 1
        return next(reads)

    monkeypatch.setattr(source, "_head_read", sequenced_read)
    snapshot = source.capture_source_snapshot(identity, time.monotonic() + 1.0)
    assert snapshot.protected_manifest.digest == "b" * 64
    assert calls == 3


def test_capture_churn_has_a_bounded_attempt_count_and_fails_proof_stale(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    stable_read = source._head_read(identity, deadline=time.monotonic() + 5.0)
    calls = 0

    def alternating_read(_repo, *, deadline):
        nonlocal calls
        calls += 1
        manifest = replace(
            stable_read.protected_manifest,
            digest=("0" if calls % 2 else "1") * 64,
        )
        return replace(stable_read, protected_manifest=manifest)

    monkeypatch.setattr(source, "_head_read", alternating_read)
    with pytest.raises(source.ProofStaleError) as raised:
        source.capture_source_snapshot(identity, time.monotonic() + 1.0)
    assert raised.value.code == "proof_stale"
    assert 2 <= calls <= 16


def test_real_index_and_worktree_churn_is_proof_stale_and_persists_no_plan(
    tmp_path, monkeypatch,
):
    source = _source()
    from agent.bestplan_state import BaselineFingerprintError, BestplanStore

    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    real_head_read = source._head_read
    calls = 0

    def alternating_real_read(captured_repo, *, deadline):
        nonlocal calls
        calls += 1
        payload = b"index-a\n" if calls % 2 else b"index-b\n"
        (repo / "tracked.txt").write_bytes(payload)
        _git(repo, "add", "tracked.txt")
        return real_head_read(captured_repo, deadline=deadline)

    monkeypatch.setattr(source, "_MAX_STABILIZATION_READS", 4)
    monkeypatch.setattr(source, "_head_read", alternating_real_read)
    with pytest.raises(source.ProofStaleError) as raised:
        source.capture_source_snapshot(identity, time.monotonic() + 10.0)
    assert raised.value.code == "proof_stale"

    calls = 0
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    with pytest.raises(BaselineFingerprintError, match="proof_stale"):
        store.create_plan(
            "churning",
            _plan(str(repo)),
            session_id="churn",
            workspace=str(repo),
        )
    assert store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_plans"
    ).fetchone()[0] == 0


def test_proof_stale_prevents_plan_persistence(tmp_path, monkeypatch):
    source = _source()
    from agent import bestplan_state

    repo = _init_repo(tmp_path / "repo")
    store = bestplan_state.BestplanStore(db_path=tmp_path / "state" / "state.db")

    def stale(_repo, _deadline):
        raise source.ProofStaleError("proof_stale: churn")

    monkeypatch.setattr(bestplan_state, "capture_source_snapshot", stale)
    with pytest.raises(bestplan_state.BaselineFingerprintError, match="proof_stale"):
        store.create_plan(
            "do it", _plan(str(repo)), session_id="s1", workspace=str(repo),
        )
    count = store._connection().execute(
        "SELECT COUNT(*) FROM bestplan_plans"
    ).fetchone()[0]
    assert count == 0


def test_existing_bestplan_schema_migrates_baseline_revision(tmp_path):
    from agent.bestplan_state import BestplanStore

    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE bestplan_plans ("
        "plan_id TEXT PRIMARY KEY, session_id TEXT, state TEXT)"
    )
    connection.commit()
    connection.close()

    store = BestplanStore(db_path=database)
    columns = {
        row[1] for row in store._connection().execute(
            "PRAGMA table_info(bestplan_plans)"
        )
    }
    assert "baseline_revision" in columns


def test_raw_paths_modes_and_symlink_targets_are_lossless(tmp_path):
    if os.name != "posix":
        pytest.skip("raw POSIX Git path contract")
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    raw_name = b"line\nbreak-\t.txt"
    repo_raw = os.fsencode(repo)
    fd = os.open(os.path.join(repo_raw, raw_name), os.O_WRONLY | os.O_CREAT, 0o700)
    try:
        os.write(fd, b"raw\x00bytes\n")
    finally:
        os.close(fd)
    subprocess.run(
        [b"git", b"add", b"--", raw_name], cwd=repo_raw, check=True,
        capture_output=True,
    )
    _git(repo, "commit", "-qm", "raw path")
    (repo / "ambient-link").symlink_to(os.fsdecode(raw_name))

    manifest = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )
    index = {entry.path: entry for entry in manifest.index_entries}
    worktree = {entry.path: entry for entry in manifest.worktree_entries}
    assert raw_name in index
    assert worktree[raw_name].mode & 0o111
    assert worktree[b"ambient-link"].symlink_target == raw_name


def test_unsupported_repository_shapes_fail_closed(tmp_path):
    source = _source()

    shallow = _init_repo(tmp_path / "shallow")
    (shallow / ".git" / "shallow").write_bytes(_git(shallow, "rev-parse", "HEAD") + b"\n")
    with pytest.raises(source.UnsupportedRepositoryError, match="shallow"):
        source.assert_supported_repository(source.resolve_repo_identity(str(shallow)))

    sparse = _init_repo(tmp_path / "sparse")
    _git(sparse, "config", "core.sparseCheckout", "true")
    with pytest.raises(source.UnsupportedRepositoryError, match="sparse"):
        source.assert_supported_repository(source.resolve_repo_identity(str(sparse)))

    submodule = _init_repo(tmp_path / "submodule")
    head = _git(submodule, "rev-parse", "HEAD").decode("ascii")
    _git(submodule, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/sub")
    _git(submodule, "commit", "-qm", "gitlink")
    with pytest.raises(source.UnsupportedRepositoryError, match="submodule"):
        source.assert_supported_repository(source.resolve_repo_identity(str(submodule)))

    replaced = _init_repo(tmp_path / "replaced")
    old = _git(replaced, "rev-parse", "HEAD").decode("ascii")
    (replaced / "tracked.txt").write_bytes(b"second\n")
    _git(replaced, "commit", "-qam", "second")
    new = _git(replaced, "rev-parse", "HEAD").decode("ascii")
    _git(replaced, "replace", old, new)
    with pytest.raises(source.UnsupportedRepositoryError, match="replace"):
        source.assert_supported_repository(source.resolve_repo_identity(str(replaced)))

    filtered = _init_repo(tmp_path / "filtered")
    _git(filtered, "config", "filter.danger.clean", "cat")
    (filtered / ".gitattributes").write_text(
        "tracked.txt filter=danger\n", encoding="utf-8",
    )
    _git(filtered, "add", ".gitattributes")
    _git(filtered, "commit", "-qm", "filtered source")
    with pytest.raises(source.UnsupportedRepositoryError, match="filter"):
        source.assert_supported_repository(source.resolve_repo_identity(str(filtered)))

    lfs = _init_repo(tmp_path / "lfs")
    (lfs / ".gitattributes").write_text(
        "tracked.txt filter=lfs\n", encoding="utf-8",
    )
    _git(lfs, "add", ".gitattributes")
    _git(lfs, "commit", "-qm", "lfs source")
    _git(lfs, "config", "filter.lfs.process", "git-lfs filter-process")
    with pytest.raises(source.UnsupportedRepositoryError, match="filter"):
        source.assert_supported_repository(source.resolve_repo_identity(str(lfs)))


def test_grafts_alternates_partial_clones_and_nested_repositories_fail_closed(
    tmp_path,
):
    source = _source()

    grafts = _init_repo(tmp_path / "grafts")
    grafts_info = Path(source.resolve_repo_identity(str(grafts)).common_dir) / "info"
    grafts_info.mkdir(exist_ok=True)
    (grafts_info / "grafts").write_bytes(b"legacy graft boundary\n")
    with pytest.raises(source.UnsupportedRepositoryError, match="graft"):
        source.assert_supported_repository(source.resolve_repo_identity(str(grafts)))

    alternate_source = _init_repo(tmp_path / "alternate-source")
    alternates = _init_repo(tmp_path / "alternates")
    objects_info = (
        Path(source.resolve_repo_identity(str(alternates)).common_dir)
        / "objects"
        / "info"
    )
    objects_info.mkdir(parents=True, exist_ok=True)
    (objects_info / "alternates").write_text(
        str((alternate_source / ".git" / "objects").resolve()) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(source.UnsupportedRepositoryError, match="alternate"):
        source.assert_supported_repository(source.resolve_repo_identity(str(alternates)))

    partial_extension = _init_repo(tmp_path / "partial-extension")
    _git(partial_extension, "config", "core.repositoryformatversion", "1")
    _git(partial_extension, "config", "extensions.partialClone", "origin")
    with pytest.raises(source.UnsupportedRepositoryError, match="partial|promisor"):
        source.assert_supported_repository(
            source.resolve_repo_identity(str(partial_extension))
        )

    for name, key, value in [
        ("promisor", "remote.origin.promisor", "true"),
        ("partial-filter", "remote.origin.partialclonefilter", "blob:none"),
    ]:
        partial = _init_repo(tmp_path / name)
        _git(partial, "config", key, value)
        with pytest.raises(source.UnsupportedRepositoryError, match="partial|promisor"):
            source.assert_supported_repository(source.resolve_repo_identity(str(partial)))

    nested = _init_repo(tmp_path / "nested")
    _init_repo(nested / "vendor" / "nested-repository")
    with pytest.raises(source.UnsupportedRepositoryError, match="nested|boundary"):
        source.assert_supported_repository(source.resolve_repo_identity(str(nested)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_nonignored_special_files_fail_closed_but_ignored_specials_do_not(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    os.mkfifo(repo / "unsafe-pipe")
    with pytest.raises(source.UnsupportedRepositoryError, match="special"):
        _snapshot(repo)

    (repo / ".gitignore").write_text("unsafe-pipe\nignored-cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore runtime specials")
    ignored = repo / "ignored-cache"
    ignored.mkdir()
    os.mkfifo(ignored / "pipe")
    _snapshot(repo)


def test_recapture_detects_protected_changes_but_ignores_ignored_state(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore cache")
    expected = _snapshot(repo)

    (repo / "cache").mkdir()
    (repo / "cache" / "runtime.bin").write_bytes(b"ignored")
    assert source.recapture_matches(expected) is True
    (repo / "tracked.txt").write_bytes(b"dirty\n")
    assert source.recapture_matches(expected) is False


def test_export_exact_tree_uses_committed_blobs_and_excludes_ambient_bytes(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    script = repo / "script.sh"
    script.write_bytes(b"#!/bin/sh\nprintf committed\n")
    script.chmod(0o755)
    (repo / "committed-link").symlink_to("tracked.txt")
    (repo / "archive-secret.txt").write_bytes(b"must remain in exact tree\n")
    (repo / "archive-template.txt").write_bytes(b"$Format:%H$\n")
    (repo / ".gitattributes").write_text(
        "archive-secret.txt export-ignore\narchive-template.txt export-subst\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(
        repo,
        "add",
        "script.sh",
        "committed-link",
        "archive-secret.txt",
        "archive-template.txt",
        ".gitattributes",
        ".gitignore",
    )
    _git(repo, "commit", "-qm", "tree shapes")

    (repo / "tracked.txt").write_bytes(b"ambient tracked\n")
    script.write_bytes(b"staged ambient\n")
    _git(repo, "add", "script.sh")
    script.write_bytes(b"unstaged ambient\n")
    (repo / "untracked.txt").write_bytes(b"ambient untracked\n")
    (repo / "cache").mkdir()
    (repo / "cache" / "ignored.bin").write_bytes(b"ambient ignored\n")

    snapshot = _snapshot(repo)
    destination = tmp_path / "exported"
    source.export_exact_tree(snapshot, destination)

    assert (destination / "tracked.txt").read_bytes() == b"committed\n"
    assert (destination / "script.sh").read_bytes() == b"#!/bin/sh\nprintf committed\n"
    assert stat.S_IMODE((destination / "script.sh").stat().st_mode) == 0o755
    assert (destination / "committed-link").is_symlink()
    assert os.fsencode(os.readlink(destination / "committed-link")) == b"tracked.txt"
    assert (destination / "archive-secret.txt").read_bytes() == b"must remain in exact tree\n"
    assert (destination / "archive-template.txt").read_bytes() == b"$Format:%H$\n"
    assert not (destination / "untracked.txt").exists()
    assert not (destination / "cache").exists()


@pytest.mark.parametrize(
    "aliased_paths",
    [
        [(b"Case.txt", b"one"), (b"case.txt", b"two")],
        [("caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt".encode(), b"one"),
         ("cafe\N{COMBINING ACUTE ACCENT}.txt".encode(), b"two")],
    ],
)
def test_export_rejects_casefold_and_unicode_normalization_aliases(
    tmp_path, aliased_paths,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _set_head_to_flat_tree(repo, aliased_paths)
    snapshot = _snapshot(repo)
    destination = tmp_path / "collision-export"

    with pytest.raises(source.UnsupportedRepositoryError, match="alias|collision"):
        source.export_exact_tree(snapshot, destination)
    assert not destination.exists()


def test_export_refuses_preexisting_symlinked_and_source_owned_destinations(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(source.SourceBoundaryError, match="exist|destination"):
        source.export_exact_tree(snapshot, existing)

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_destination = tmp_path / "destination-link"
    symlink_destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(source.SourceBoundaryError, match="symlink|exist|destination"):
        source.export_exact_tree(snapshot, symlink_destination)

    with pytest.raises(source.SourceBoundaryError, match="source|Git|repository"):
        source.export_exact_tree(snapshot, repo / "unsafe-export")

    alias_parent = tmp_path / "source-alias"
    alias_parent.symlink_to(repo, target_is_directory=True)
    with pytest.raises(source.SourceBoundaryError, match="source|Git|repository"):
        source.export_exact_tree(snapshot, alias_parent / "unsafe-export")


def test_export_cat_file_timeout_is_coded_proof_stale(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    real_run = source.subprocess.run

    def timeout_cat_file(command, *args, **kwargs):
        if command[:3] == ["git", "cat-file", "--batch"]:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(source.subprocess, "run", timeout_cat_file)
    with pytest.raises(source.ProofStaleError) as raised:
        source.export_exact_tree(snapshot, tmp_path / "timed-out-export")
    assert raised.value.code == "proof_stale"


def test_batch_blob_request_checks_deadline_before_spawning_git(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    oid = _git(repo, "rev-parse", "HEAD:tracked.txt").decode("ascii")
    entries = tuple(
        source._TreeEntry(
            path=f"path-{index}".encode(),
            mode=0o100644,
            object_type=b"blob",
            oid=oid,
        )
        for index in range(2)
    )
    checks = 0

    def expiring_remaining(_deadline):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise source.ProofStaleError("proof_stale: synthetic deadline")
        return 1.0

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("cat-file spawned after the absolute deadline")

    monkeypatch.setattr(source, "_remaining", expiring_remaining)
    monkeypatch.setattr(source, "_run_git", unexpected_git)
    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source._batch_blobs(identity, entries, deadline=time.monotonic() + 1.0)
