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


def test_export_quarantines_staging_content_changed_while_later_blob_streams(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    (repo / "a.txt").write_bytes(b"committed a\n")
    (repo / "z.bin").write_bytes(b"z" * (source._BUFFER_SIZE * 2 + 1))
    base._git(repo, "add", "a.txt", "z.bin")
    base._git(repo, "commit", "-qm", "streamed tree")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    real_write = source._write_export_file
    z_streaming = threading.Event()
    mutation_finished = threading.Event()
    mutation_errors: list[BaseException] = []

    class PausingReader:
        def __init__(self, reader):
            self.reader = reader

        def iter_exact(self, size):
            first = True
            for chunk in self.reader.iter_exact(size):
                yield chunk
                if first:
                    first = False
                    z_streaming.set()
                    if not mutation_finished.wait(timeout=5.0):
                        raise AssertionError("staging mutator did not finish")

    def pause_later_blob(parent_fd, name, reader, size, mode, *, deadline):
        if name == b"z.bin":
            reader = PausingReader(reader)
        return real_write(
            parent_fd, name, reader, size, mode, deadline=deadline,
        )

    def overwrite_materialized_file():
        try:
            if not z_streaming.wait(timeout=5.0):
                raise AssertionError("later blob did not start streaming")
            staging = list(tmp_path.glob(".exported.bestplan-staging-*"))
            if len(staging) != 1:
                raise AssertionError(f"unexpected staging paths: {staging!r}")
            (staging[0] / "a.txt").write_bytes(b"foreign aa!\n")
        except BaseException as exc:
            mutation_errors.append(exc)
        finally:
            mutation_finished.set()

    monkeypatch.setattr(source, "_write_export_file", pause_later_blob)
    mutator = threading.Thread(target=overwrite_materialized_file, daemon=True)
    mutator.start()
    try:
        with pytest.raises(source.SourceBoundaryError, match="quarantined") as raised:
            source.export_exact_tree(snapshot, destination)
    finally:
        mutation_finished.set()
        mutator.join(timeout=5.0)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert not mutator.is_alive()
    assert mutation_errors == []
    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.bestplan-staging-*"))
    assert len(quarantines) == 1
    assert str(quarantines[0]) in str(raised.value)
    assert "concurrent changes" in str(raised.value)
    assert (quarantines[0] / "a.txt").read_bytes() == b"foreign aa!\n"


def test_export_requires_two_complete_observations_after_first_scan_mutation(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    real_scan = source._verify_exported_tree
    scan_attempts = 0

    def mutate_after_first_complete_scan(*args, **kwargs):
        nonlocal scan_attempts
        scan_attempts += 1
        result = real_scan(*args, **kwargs)
        if scan_attempts == 1:
            (destination / "tracked.txt").write_bytes(b"forged!!!\n")
        return result

    monkeypatch.setattr(
        source, "_verify_exported_tree", mutate_after_first_complete_scan,
    )
    with pytest.raises(source.SourceBoundaryError, match="quarantined") as raised:
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert scan_attempts >= 2
    assert not destination.exists()
    assert len(quarantines) == 1
    assert "concurrent changes" in str(raised.value)
    assert (quarantines[0] / "tracked.txt").read_bytes() == b"forged!!!\n"


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_path",
        "missing_path",
        "regular_mode",
        "directory_mode",
        "root_mode",
        "regular_type",
        "symlink_target",
    ],
)
def test_export_final_verifier_checks_exact_tree_shape(
    tmp_path, monkeypatch, mutation,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    (nested / "payload.txt").write_bytes(b"nested committed\n")
    (repo / "committed-link").symlink_to("tracked.txt")
    base._git(repo, "add", "nested/payload.txt", "committed-link")
    base._git(repo, "commit", "-qm", "tree verification shapes")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    real_root_verifier = source._verify_published_destination
    mutated = False

    def mutate_after_root_identity(prepared, *, deadline):
        nonlocal mutated
        real_root_verifier(prepared, deadline=deadline)
        if mutated:
            return
        mutated = True
        if mutation == "extra_path":
            (destination / "foreign-extra").write_bytes(b"foreign bytes\n")
        elif mutation == "missing_path":
            (destination / "nested" / "payload.txt").unlink()
        elif mutation == "regular_mode":
            (destination / "tracked.txt").chmod(0o755)
        elif mutation == "directory_mode":
            (destination / "nested").chmod(0o700)
        elif mutation == "root_mode":
            destination.chmod(0o700)
        elif mutation == "regular_type":
            (destination / "tracked.txt").unlink()
            (destination / "tracked.txt").mkdir()
        elif mutation == "symlink_target":
            (destination / "committed-link").unlink()
            (destination / "committed-link").symlink_to("foreign-target")
        else:
            raise AssertionError(f"unknown mutation: {mutation}")

    monkeypatch.setattr(
        source, "_verify_published_destination", mutate_after_root_identity,
    )
    with pytest.raises(source.SourceBoundaryError, match="quarantined") as raised:
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert mutated is True
    assert not destination.exists()
    assert len(quarantines) == 1
    assert "concurrent changes" in str(raised.value)
    quarantined = quarantines[0]
    if mutation == "extra_path":
        assert (quarantined / "foreign-extra").read_bytes() == b"foreign bytes\n"
    elif mutation == "missing_path":
        assert not (quarantined / "nested" / "payload.txt").exists()
    elif mutation == "regular_mode":
        assert stat.S_IMODE((quarantined / "tracked.txt").stat().st_mode) == 0o755
    elif mutation == "directory_mode":
        assert stat.S_IMODE((quarantined / "nested").stat().st_mode) == 0o700
    elif mutation == "root_mode":
        assert stat.S_IMODE(quarantined.stat().st_mode) == 0o700
    elif mutation == "regular_type":
        assert (quarantined / "tracked.txt").is_dir()
    elif mutation == "symlink_target":
        assert os.readlink(quarantined / "committed-link") == "foreign-target"


def test_export_failure_leaves_no_final_destination_and_retry_succeeds(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    (repo / "second.txt").write_bytes(b"second committed file\n")
    base._git(repo, "add", "second.txt")
    base._git(repo, "commit", "-qm", "second file")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    real_write = source._write_export_file
    writes = 0

    def fail_second_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise source.SourceBoundaryError("synthetic second-file failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(source, "_write_export_file", fail_second_write)
    with pytest.raises(source.SourceBoundaryError, match="second-file"):
        source.export_exact_tree(snapshot, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.bestplan-staging-*"))

    monkeypatch.setattr(source, "_write_export_file", real_write)
    source.export_exact_tree(snapshot, destination)
    assert (destination / "tracked.txt").read_bytes() == b"committed\n"
    assert (destination / "second.txt").read_bytes() == b"second committed file\n"


def test_export_post_publish_authority_failure_removes_owned_destination(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    authority = source._get_capture_authority(time.monotonic() + 3.0)

    monkeypatch.setattr(source, "recapture_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        source,
        "_verify_public_authority",
        lambda *, deadline: authority,
    )

    def fail_only_after_publish(_authority, *, deadline):
        assert deadline > time.monotonic()
        if destination.exists():
            raise source.SourceBoundaryError(
                "trusted authority changed after atomic publication"
            )

    monkeypatch.setattr(
        source, "_verify_public_authority_after", fail_only_after_publish,
    )
    with pytest.raises(
        source.SourceBoundaryError, match="after atomic publication",
    ) as raised:
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert not destination.exists()
    assert not list(tmp_path.glob(".exported.bestplan-staging-*"))
    assert len(quarantines) == 1
    assert "(unchanged)" in str(raised.value)
    assert (quarantines[0] / "tracked.txt").read_bytes() == b"committed\n"


def test_export_post_publish_failure_preserves_concurrent_foreign_addition(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    authority = source._get_capture_authority(time.monotonic() + 3.0)

    monkeypatch.setattr(source, "recapture_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        source,
        "_verify_public_authority",
        lambda *, deadline: authority,
    )

    def add_foreign_file_then_fail(_authority, *, deadline):
        assert deadline > time.monotonic()
        if destination.exists():
            (destination / "foreign-sentinel").write_bytes(b"foreign bytes\n")
            raise source.SourceBoundaryError("synthetic post-publish failure")

    monkeypatch.setattr(
        source, "_verify_public_authority_after", add_foreign_file_then_fail,
    )
    with pytest.raises(source.SourceBoundaryError, match="quarantined") as raised:
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert not destination.exists()
    assert len(quarantines) == 1
    assert str(quarantines[0]) in str(raised.value)
    assert "concurrent changes" in str(raised.value)
    assert (quarantines[0] / "foreign-sentinel").read_bytes() == b"foreign bytes\n"


def test_quarantine_unchanged_classification_requires_two_observations(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    real_scan = source._verify_exported_tree
    quarantine_scan_attempts = 0

    monkeypatch.setattr(source, "recapture_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        source,
        "_verify_public_authority",
        lambda *, deadline: authority,
    )

    def fail_only_after_publish(_authority, *, deadline):
        assert deadline > time.monotonic()
        if destination.exists():
            raise source.SourceBoundaryError("synthetic post-publish failure")

    def mutate_after_first_quarantine_scan(*args, **kwargs):
        nonlocal quarantine_scan_attempts
        quarantine_scan_attempts += 1
        result = real_scan(*args, **kwargs)
        quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
        if quarantines:
            if quarantine_scan_attempts == 1:
                (quarantines[0] / "tracked.txt").write_bytes(b"forged!!!\n")
        return result

    monkeypatch.setattr(
        source, "_verify_public_authority_after", fail_only_after_publish,
    )
    monkeypatch.setattr(
        source, "_verify_exported_tree", mutate_after_first_quarantine_scan,
    )
    with pytest.raises(source.SourceBoundaryError, match="quarantined") as raised:
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert quarantine_scan_attempts >= 2
    assert not destination.exists()
    assert len(quarantines) == 1
    assert "concurrent changes" in str(raised.value)
    assert (quarantines[0] / "tracked.txt").read_bytes() == b"forged!!!\n"


def test_quarantine_classifier_rechecks_regular_path_after_hashing(
    tmp_path, monkeypatch,
):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "exported"
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    exported_identity: tuple[int, int] | None = None
    target_fstats = 0
    real_fstat = source.os.fstat

    monkeypatch.setattr(source, "recapture_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        source,
        "_verify_public_authority",
        lambda *, deadline: authority,
    )

    def capture_identity_then_fail(_authority, *, deadline):
        nonlocal exported_identity
        assert deadline > time.monotonic()
        if destination.exists():
            info = os.lstat(destination / "tracked.txt")
            exported_identity = (info.st_dev, info.st_ino)
            raise source.SourceBoundaryError("synthetic post-publish failure")

    def replace_name_after_final_fd_check(fd):
        nonlocal target_fstats
        result = real_fstat(fd)
        if exported_identity == (result.st_dev, result.st_ino):
            target_fstats += 1
            if target_fstats == 2:
                quarantines = list(
                    tmp_path.glob(".exported.bestplan-quarantine-*")
                )
                assert len(quarantines) == 1
                target = quarantines[0] / "tracked.txt"
                target.unlink()
                target.write_bytes(b"foreign replacement\n")
        return result

    monkeypatch.setattr(
        source, "_verify_public_authority_after", capture_identity_then_fail,
    )
    monkeypatch.setattr(source.os, "fstat", replace_name_after_final_fd_check)
    with pytest.raises(source.SourceBoundaryError, match="concurrent changes"):
        source.export_exact_tree(snapshot, destination)

    quarantines = list(tmp_path.glob(".exported.bestplan-quarantine-*"))
    assert target_fstats == 2
    assert not destination.exists()
    assert len(quarantines) == 1
    assert (quarantines[0] / "tracked.txt").read_bytes() == b"foreign replacement\n"


def test_export_brackets_a_failure_before_destination_preparation(
    tmp_path, monkeypatch,
):
    source = base._source()
    snapshot = base._snapshot(base._init_repo(tmp_path / "repo"))
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    events: list[str] = []

    monkeypatch.setattr(source, "_assert_export_host_supported", lambda: "darwin")

    def verify_before(*, deadline):
        assert deadline > time.monotonic()
        events.append("verify-before")
        return authority

    def stale_recapture(_snapshot, *, deadline):
        assert deadline > time.monotonic()
        events.append("recapture")
        return False

    def verify_after(_authority, *, deadline):
        assert deadline > time.monotonic()
        events.append("verify-after")

    monkeypatch.setattr(source, "_verify_public_authority", verify_before)
    monkeypatch.setattr(source, "recapture_matches", stale_recapture)
    monkeypatch.setattr(source, "_verify_public_authority_after", verify_after)

    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source.export_exact_tree(snapshot, tmp_path / "exported")

    assert events == ["verify-before", "recapture", "verify-after"]


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_NOFILE is POSIX-only")
def test_failed_deep_export_cleanup_stays_below_low_process_limit(tmp_path):
    repo = base._init_repo(tmp_path / "repo")
    directory = repo
    for index in range(130):
        directory /= f"d{index:03d}"
        directory.mkdir()
    (directory / "payload.txt").write_bytes(b"deep payload\n")
    base._git(repo, "add", ".")
    base._git(repo, "commit", "-qm", "deep committed tree")
    destination = tmp_path / "exported-deep-low-fd"
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

def fail_after_publish(_authority, *, deadline):
    if os.path.lexists(destination):
        raise source.SourceBoundaryError("synthetic post-publish failure")

source._verify_public_authority_after = fail_after_publish
try:
    source.export_exact_tree(snapshot, destination)
except source.SourceBoundaryError:
    pass
else:
    raise AssertionError("synthetic verifier failure was not raised")
assert not os.path.lexists(destination)
parent = os.path.dirname(destination)
leaf = os.path.basename(destination)
assert not any(
    name.startswith("." + leaf + ".bestplan-staging-")
    for name in os.listdir(parent)
)
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
    assert not destination.exists()


def test_export_atomic_publish_preserves_raced_destination(tmp_path, monkeypatch):
    source = base._source()
    repo = base._init_repo(tmp_path / "repo")
    snapshot = base._snapshot(repo)
    destination = tmp_path / "raced-export"
    real_publish = source._publish_staging_no_replace

    def race_destination(prepared, *, backend, deadline):
        os.mkdir(prepared.final_leaf, dir_fd=prepared.parent_fds[-1])
        final_fd = os.open(
            prepared.final_leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=prepared.parent_fds[-1],
        )
        try:
            sentinel = os.open(
                b"attacker-sentinel",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=final_fd,
            )
            os.close(sentinel)
        finally:
            os.close(final_fd)
        return real_publish(prepared, backend=backend, deadline=deadline)

    monkeypatch.setattr(source, "_publish_staging_no_replace", race_destination)
    with pytest.raises(source.SourceBoundaryError, match="destination|publication"):
        source.export_exact_tree(snapshot, destination)

    assert (destination / "attacker-sentinel").is_file()
    assert not (destination / "tracked.txt").exists()
    assert not list(tmp_path.glob(".raced-export.bestplan-staging-*"))
