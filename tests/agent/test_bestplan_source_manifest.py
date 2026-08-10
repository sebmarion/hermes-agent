from __future__ import annotations

import hashlib
import importlib
import subprocess
import time
from pathlib import Path

import pytest


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


def _write_filter_marker_script(path: Path, marker: Path, *, passthrough: bool) -> None:
    tail = "cat\n" if passthrough else "exit 1\n"
    path.write_text(
        f"#!/bin/sh\n: > {marker}\n" + tail,
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_conflict_index(repo: Path) -> None:
    def write_blob(value: bytes) -> str:
        return subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=value,
            check=True,
            capture_output=True,
        ).stdout.removesuffix(b"\n").decode("ascii")

    base_oid = _git(repo, "rev-parse", "HEAD:tracked.txt").decode("ascii")
    ours_oid = write_blob(b"ours\n")
    link_oid = write_blob(b"link-target")
    zeros = "0" * len(base_oid)
    conflict = (
        f"0 {zeros}\ttracked.txt\n"
        f"100644 {base_oid} 1\ttracked.txt\n"
        f"100755 {ours_oid} 2\ttracked.txt\n"
        f"120000 {link_oid} 3\ttracked.txt\n"
    ).encode("ascii")
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repo,
        input=conflict,
        check=True,
        capture_output=True,
    )


def _set_conflict_stage_assume_valid(repo: Path, target_stage: int) -> None:
    index_path = Path(
        _git(
            repo, "rev-parse", "--path-format=absolute", "--git-path", "index",
        ).decode("utf-8")
    )
    value = bytearray(index_path.read_bytes())
    assert value[:4] == b"DIRC"
    version = int.from_bytes(value[4:8], "big")
    assert version in {2, 3}
    entry_count = int.from_bytes(value[8:12], "big")
    offset = 12
    matched = False
    for _entry_index in range(entry_count):
        entry_start = offset
        flags_offset = entry_start + 60
        flags = int.from_bytes(value[flags_offset:flags_offset + 2], "big")
        name_length = flags & 0x0FFF
        name_offset = flags_offset + 2 + (2 if flags & 0x4000 else 0)
        nul = (
            value.index(b"\0", name_offset)
            if name_length == 0x0FFF
            else name_offset + name_length
        )
        stage = (flags >> 12) & 0x3
        if stage == target_stage:
            value[flags_offset:flags_offset + 2] = (
                flags | 0x8000
            ).to_bytes(2, "big")
            matched = True
        entry_size = nul + 1 - entry_start
        offset = nul + 1 + (-entry_size) % 8
    assert matched
    value[-20:] = hashlib.sha1(value[:-20]).digest()
    index_path.write_bytes(value)


def test_public_strong_capture_ignores_staged_and_unstaged_conversion_drivers(
    tmp_path,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    attribute_rules = (
        "clean.txt filter=clean\n"
        "smudge.txt filter=smudge\n"
        "process.txt filter=process\n"
        "textconv.txt diff=textconv\n"
    )
    for directory in (repo / "staged", repo / "unstaged"):
        directory.mkdir()
        for name in ("clean.txt", "smudge.txt", "process.txt", "textconv.txt"):
            (directory / name).write_bytes(b"committed bytes\n")
    (repo / "unstaged" / ".gitattributes").write_text(
        "# committed inert attributes\n", encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "conversion fixtures")

    markers = {
        name: tmp_path / f"{name}-ran"
        for name in ("clean", "smudge", "process", "textconv")
    }
    scripts = {name: tmp_path / name for name in markers}
    for name, script in scripts.items():
        _write_filter_marker_script(
            script, markers[name], passthrough=name != "process",
        )
    (repo / "staged" / ".gitattributes").write_text(
        attribute_rules, encoding="utf-8",
    )
    _git(repo, "add", "staged/.gitattributes")
    (repo / "unstaged" / ".gitattributes").write_text(
        attribute_rules, encoding="utf-8",
    )
    _git(repo, "config", "filter.clean.clean", str(scripts["clean"]))
    _git(repo, "config", "filter.smudge.smudge", str(scripts["smudge"]))
    _git(repo, "config", "filter.process.process", str(scripts["process"]))
    _git(repo, "config", "diff.textconv.textconv", str(scripts["textconv"]))
    for directory in (repo / "staged", repo / "unstaged"):
        for name in ("clean.txt", "smudge.txt", "process.txt", "textconv.txt"):
            (directory / name).write_bytes(b"dirty worktree bytes\n")

    identity = source.resolve_repo_identity(str(repo))
    snapshot = source.capture_source_snapshot(
        identity, time.monotonic() + 20.0,
    )

    executed = {name for name, marker in markers.items() if marker.exists()}
    assert b"staged/.gitattributes" in snapshot.protected_manifest.protected_paths
    assert b"unstaged/.gitattributes" in snapshot.protected_manifest.protected_paths
    assert b"staged/clean.txt" in snapshot.protected_manifest.protected_paths
    assert b"unstaged/clean.txt" in snapshot.protected_manifest.protected_paths
    assert executed == set()


def test_manifest_streams_canonical_binary_delta_hashes_without_git_diff(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    binary = repo / "binary.dat"
    binary.write_bytes(b"committed\x00bytes")
    _git(repo, "add", "binary.dat")
    _git(repo, "commit", "-qm", "binary base")
    binary.write_bytes(b"staged\x00bytes")
    _git(repo, "add", "binary.dat")
    binary.write_bytes(b"unstaged\x00bytes")
    real_run_git_output = source._run_git_output

    def reject_git_diff(cwd, *args, **kwargs):
        if args and args[0] == "diff":
            raise AssertionError("strong capture invoked Git diff")
        return real_run_git_output(cwd, *args, **kwargs)

    monkeypatch.setattr(source, "_run_git_output", reject_git_diff)
    first = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )
    binary.write_bytes(b"second unstaged\x00bytes")
    second = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )

    assert len(first.staged_diff_sha256) == 64
    assert len(first.unstaged_diff_sha256) == 64
    assert first.staged_diff_sha256 != first.unstaged_diff_sha256
    assert second.staged_diff_sha256 == first.staged_diff_sha256
    assert second.unstaged_diff_sha256 != first.unstaged_diff_sha256


def test_head_read_reuses_the_exact_supported_head_tree(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source._resolve_repo_identity(
        str(repo), time.monotonic() + 10.0,
    )
    real_tree_entries = source._tree_entries
    real_manifest = source._capture_protected_manifest
    tree_reads = []
    manifest_trees = []

    def tracking_tree_entries(captured_repo, treeish, *, deadline):
        entries = real_tree_entries(captured_repo, treeish, deadline=deadline)
        tree_reads.append((treeish, entries))
        return entries

    def tracking_manifest(captured_repo, *, deadline, head_tree_entries=None):
        manifest_trees.append(head_tree_entries)
        return real_manifest(
            captured_repo,
            deadline=deadline,
            head_tree_entries=head_tree_entries,
        )

    monkeypatch.setattr(source, "_tree_entries", tracking_tree_entries)
    monkeypatch.setattr(source, "_capture_protected_manifest", tracking_manifest)

    read = source._head_read(identity, deadline=time.monotonic() + 20.0)

    assert [treeish for treeish, _entries in tree_reads] == [read.tree_oid]
    assert manifest_trees == [tree_reads[0][1]]
    assert manifest_trees[0] is tree_reads[0][1]


def test_manifest_fails_closed_when_canonical_delta_exceeds_bound(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_bytes(b"a substantially larger dirty payload\n")
    monkeypatch.setattr(source, "_MAX_DIFF_BYTES", 8, raising=False)

    with pytest.raises(
        source.UnsupportedRepositoryError, match="delta|diff|limit",
    ) as raised:
        source.capture_protected_manifest(source.resolve_repo_identity(str(repo)))

    assert raised.value.code == "unsupported_repository"


def test_manifest_never_invokes_git_diff(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_bytes(b"changed\n")
    real_run_git_output = source._run_git_output

    def reject_git_diff(cwd, *args, **kwargs):
        if args and args[0] == "diff":
            raise AssertionError("strong capture invoked Git diff")
        return real_run_git_output(cwd, *args, **kwargs)

    monkeypatch.setattr(source, "_run_git_output", reject_git_diff)
    manifest = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )

    assert b"tracked.txt" in manifest.protected_paths


def test_manifest_intent_to_add_is_unstaged_only_even_for_empty_file(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    clean = source.capture_protected_manifest(identity)
    (repo / "intent.txt").write_bytes(b"")
    _git(repo, "add", "-N", "intent.txt")

    intent = source.capture_protected_manifest(identity)

    flag = {entry.path: entry for entry in intent.index_flags}[b"intent.txt"]
    assert flag.intent_to_add is True
    assert intent.staged_diff_sha256 == clean.staged_diff_sha256
    assert intent.unstaged_diff_sha256 != clean.unstaged_diff_sha256
    assert b"intent.txt" in intent.protected_paths


def test_manifest_conflict_stages_preserve_mode_type_and_both_delta_sides(
    tmp_path,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    clean = source.capture_protected_manifest(identity)
    _install_conflict_index(repo)

    manifest = source.capture_protected_manifest(identity)

    conflict_entries = [
        entry for entry in manifest.index_entries if entry.path == b"tracked.txt"
    ]
    assert {(entry.stage, entry.mode) for entry in conflict_entries} == {
        (1, 0o100644),
        (2, 0o100755),
        (3, 0o120000),
    }
    assert manifest.staged_diff_sha256 != clean.staged_diff_sha256
    assert manifest.unstaged_diff_sha256 != clean.unstaged_diff_sha256
    assert b"tracked.txt" in manifest.protected_paths


@pytest.mark.parametrize("stage", [1, 2])
def test_manifest_rejects_nonzero_conflict_stage_index_flags(tmp_path, stage):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _install_conflict_index(repo)
    _set_conflict_stage_assume_valid(repo, stage)

    with pytest.raises(
        source.UnsupportedRepositoryError, match="conflict.*flag|nonzero.*stage",
    ):
        source.capture_protected_manifest(source.resolve_repo_identity(str(repo)))
