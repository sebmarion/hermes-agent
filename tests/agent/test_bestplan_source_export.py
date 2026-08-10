from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.agent import test_bestplan_source as base


def test_export_exact_tree_uses_committed_blobs_and_excludes_ambient_bytes(tmp_path):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
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
    base._git(
        repo,
        "add",
        "script.sh",
        "committed-link",
        "archive-secret.txt",
        "archive-template.txt",
        ".gitattributes",
        ".gitignore",
    )
    base._git(repo, "commit", "-qm", "tree shapes")

    (repo / "tracked.txt").write_bytes(b"ambient tracked\n")
    script.write_bytes(b"staged ambient\n")
    base._git(repo, "add", "script.sh")
    script.write_bytes(b"unstaged ambient\n")
    (repo / "untracked.txt").write_bytes(b"ambient untracked\n")
    (repo / "cache").mkdir()
    (repo / "cache" / "ignored.bin").write_bytes(b"ambient ignored\n")

    snapshot = base._snapshot(repo)
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


def test_export_recapture_and_materialization_have_distinct_bounded_budgets(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)

    class MaterializationReached(RuntimeError):
        pass

    def slow_recapture(_snapshot, *, deadline):
        assert deadline > time.monotonic()
        time.sleep(0.35)
        return True

    def require_fresh_materialization_budget(_repo, _tree_oid, *, deadline):
        assert deadline - time.monotonic() > 0.2
        raise MaterializationReached

    monkeypatch.setattr(source, "_DEFAULT_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(source, "recapture_matches", slow_recapture)
    monkeypatch.setattr(source, "_tree_entries", require_fresh_materialization_budget)

    with pytest.raises(MaterializationReached):
        source.export_exact_tree(snapshot, tmp_path / "exported")


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_NOFILE is POSIX-only")
def test_exact_tree_export_keeps_directory_fds_below_low_process_limit(tmp_path):
    repo = base._init_repo(tmp_path / "repo")
    for index in range(320):
        directory = repo / f"directory-{index:04d}"
        directory.mkdir()
        (directory / "payload.txt").write_bytes(f"payload-{index}\n".encode())
    base._git(repo, "add", ".")
    base._git(repo, "commit", "-qm", "wide committed tree")
    destination = tmp_path / "exported-low-fd"
    script = """
import os
import resource
import sys
import time
from agent import bestplan_source as source

repo_path, destination = sys.argv[1:3]
identity = source.resolve_repo_identity(repo_path)
snapshot = source.capture_source_snapshot(identity, time.monotonic() + 20.0)
_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (96, hard))
source.export_exact_tree(snapshot, destination)
assert os.path.isfile(os.path.join(destination, "directory-0319", "payload.txt"))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(destination)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=90.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "directory-0000" / "payload.txt").is_file()
    assert (destination / "directory-0319" / "payload.txt").is_file()


def test_export_fails_closed_when_tree_entry_limit_is_exceeded(tmp_path, monkeypatch):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    (repo / "second.txt").write_bytes(b"second\n")
    base._git(repo, "add", "second.txt")
    base._git(repo, "commit", "-qm", "second file")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "entry-limited-export"
    monkeypatch.setattr(source, "_MAX_TREE_ENTRIES", 1, raising=False)

    with pytest.raises(source.UnsupportedRepositoryError, match="tree|metadata|limit"):
        source.export_exact_tree(snapshot, destination)

    assert not destination.exists()


def test_export_streams_blobs_and_fails_closed_at_blob_limit(tmp_path, monkeypatch):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "blob-limited-export"
    monkeypatch.setattr(source, "_MAX_BLOB_BYTES", 4, raising=False)

    with pytest.raises(source.UnsupportedRepositoryError, match="blob|limit"):
        source.export_exact_tree(snapshot, destination)

    assert not destination.exists()


def test_export_fails_closed_before_capture_on_non_posix_host(tmp_path, monkeypatch):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "unsupported-host-export"
    monkeypatch.setattr(
        source,
        "recapture_matches",
        lambda *_args, **_kwargs: pytest.fail("unsupported host recaptured source"),
    )

    with monkeypatch.context() as context:
        context.setattr(source.os, "name", "nt")
        with pytest.raises(source.UnsupportedRepositoryError, match="POSIX|host"):
            source.export_exact_tree(snapshot, destination)

    assert not destination.exists()


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
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    base._set_head_to_flat_tree(repo, aliased_paths)
    snapshot = base._snapshot(repo)
    destination = tmp_path / "collision-export"

    with pytest.raises(source.UnsupportedRepositoryError, match="alias|collision"):
        source.export_exact_tree(snapshot, destination)
    assert not destination.exists()


def test_export_refuses_preexisting_symlinked_and_source_owned_destinations(tmp_path):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)

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
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    def timeout_cat_file(_reader):
        raise source.ProofStaleError(
            "proof_stale: synthetic cat-file read timeout"
        )

    monkeypatch.setattr(source._DeadlinePipeReader, "_fill", timeout_cat_file)
    with pytest.raises(source.ProofStaleError) as raised:
        source.export_exact_tree(snapshot, tmp_path / "timed-out-export")
    assert raised.value.code == "proof_stale"


def test_batch_blob_request_checks_deadline_before_spawning_git(tmp_path, monkeypatch):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    oid = base._git(repo, "rev-parse", "HEAD:tracked.txt").decode("ascii")
    entries = tuple(
        source._TreeEntry(
            path=f"path-{index}".encode(),
            mode=0o100644,
            object_type=b"blob",
            oid=oid,
        )
        for index in range(2)
    )
    def expiring_remaining(_deadline):
        raise source.ProofStaleError("proof_stale: synthetic deadline")

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("cat-file spawned after the absolute deadline")

    monkeypatch.setattr(source, "_remaining", expiring_remaining)
    monkeypatch.setattr(source.subprocess, "Popen", unexpected_git)
    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source._materialize_blobs(
            identity,
            entries,
            -1,
            deadline=time.monotonic() + 1.0,
        )


def test_export_rejects_intermediate_parent_symlink_substitution(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    stable = tmp_path / "stable"
    race_component = stable / "race-component"
    destination_parent = race_component / "destination-parent"
    destination_parent.mkdir(parents=True)
    backup = stable / "race-component-original"
    attacker = tmp_path / "attacker"
    (attacker / "destination-parent").mkdir(parents=True)
    destination = destination_parent / "exported"
    real_open = source.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        raw = os.fsencode(path)
        if not swapped and raw in {
            os.fsencode(destination_parent),
            b"race-component",
        }:
            race_component.rename(backup)
            race_component.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(source.os, "open", racing_open)
    with pytest.raises(source.SourceBoundaryError, match="parent|changed|unsafe"):
        source.export_exact_tree(snapshot, destination)
    assert swapped is True
    assert not (attacker / "destination-parent" / "exported").exists()
