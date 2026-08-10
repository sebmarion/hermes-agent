from __future__ import annotations

import hashlib
import importlib
import os
import pickle
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
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
    return source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )


def test_snapshot_fixture_uses_production_operation_budget(tmp_path, monkeypatch):
    source = _source()
    repo = tmp_path / "repo"
    identity = object()
    observed: dict[str, float] = {}

    monkeypatch.setattr(source, "resolve_repo_identity", lambda _workspace: identity)

    def capture(repo_identity, deadline):
        assert repo_identity is identity
        observed["deadline"] = deadline
        return object()

    monkeypatch.setattr(source, "capture_source_snapshot", capture)

    before = time.monotonic()
    _snapshot(repo)
    after = time.monotonic()

    assert (
        before + source.DEFAULT_SOURCE_OPERATION_SECONDS
        <= observed["deadline"]
        <= after + source.DEFAULT_SOURCE_OPERATION_SECONDS
    )


def _fsmonitor_index_bytes(repo: Path) -> bytes:
    index_path = Path(
        _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
        ).decode("utf-8")
    )
    value = index_path.read_bytes()
    assert b"FSMN" in value
    return value


def _fsmn_payload(value: bytes) -> tuple[int, int]:
    signature = value.index(b"FSMN")
    size = int.from_bytes(value[signature + 4 : signature + 8], "big")
    start = signature + 8
    return start, start + size


def _fsmn_ewah_offset(value: bytes) -> int:
    start, end = _fsmn_payload(value)
    version = int.from_bytes(value[start : start + 4], "big")
    assert version == 2
    token_end = value.index(b"\0", start + 4, end)
    return token_end + 1 + 4


def _rewrite_sha1_index_checksum(value: bytearray) -> bytes:
    value[-20:] = hashlib.sha1(value[:-20]).digest()
    return bytes(value)


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


def test_default_source_operation_budget_has_real_checkout_headroom():
    source = _source()
    assert source.DEFAULT_SOURCE_OPERATION_SECONDS >= 20.0


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
    assert hasattr(source, "_capture_source_snapshot_in_process")
    first = source._capture_source_snapshot_in_process(
        source.resolve_repo_identity(str(repo)), time.monotonic() + 5.0,
    )
    (repo / ".bytecode-fingerprint").write_bytes(b"changed but ignored")
    second = source._capture_source_snapshot_in_process(
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


def test_special_file_scan_enforces_entry_limit_while_iterating(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    for index in range(8):
        (repo / f"ambient-{index}.txt").write_bytes(b"ambient\n")
    real_scandir = source.os.scandir
    root_raw = os.fsencode(repo)
    root_yields = 0

    class GuardedScandir:
        def __init__(self, path):
            self._is_root = os.fsencode(path) == root_raw
            self._context = real_scandir(path)

        def __enter__(self):
            self._iterator = iter(self._context.__enter__())
            return self

        def __exit__(self, *args):
            return self._context.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal root_yields
            entry = next(self._iterator)
            if self._is_root:
                root_yields += 1
                if root_yields > 3:
                    raise AssertionError(
                        "directory was buffered past the trusted entry limit"
                    )
            return entry

    monkeypatch.setattr(source, "_MAX_PROTECTED_PATHS", 1)
    monkeypatch.setattr(source.os, "scandir", GuardedScandir)

    with pytest.raises(source.UnsupportedRepositoryError, match="metadata|limit"):
        source._scan_nonignored_specials(
            source.resolve_repo_identity(str(repo)),
            deadline=time.monotonic() + 2.0,
        )

    assert root_yields <= 3


def test_snapshot_records_repo_head_ref_common_dir_and_full_oid(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()

    identity = source.resolve_repo_identity(str(nested))
    snapshot = source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )
    expected = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")

    assert identity.worktree_raw == os.fsencode(repo.resolve())
    assert identity.common_dir_raw == os.fsencode((repo / ".git").resolve())
    assert identity.repository_id
    assert snapshot.head_oid == expected
    assert snapshot.head_ref == b"refs/heads/" + _git(repo, "branch", "--show-current")
    assert snapshot.head_symbolic is True
    assert snapshot.head_raw == b"ref: " + snapshot.head_ref + b"\n"
    assert snapshot.capture_implementation_sha256 == (
        source._CAPTURE_IMPLEMENTATION_SHA256
    )


def test_trusted_git_invocation_ignores_path_substitution(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "fake-git-executed"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"printf redirected > {str(marker)!r}\n"
        "printf 'forged\\n'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    _code, output = source._run_git(
        os.fsencode(repo),
        "rev-parse",
        "--is-inside-work-tree",
        deadline=time.monotonic() + 2.0,
    )
    assert output == b"true\n"
    assert not marker.exists()
    identity = source.resolve_repo_identity(str(repo))
    snapshot = source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )
    destination = tmp_path / "exported"
    source.export_exact_tree(snapshot, destination)
    assert (destination / "tracked.txt").read_bytes() == b"committed\n"
    assert not marker.exists()


def test_captured_commit_export_survives_ambient_head_move_without_weakening_strong_export(
    tmp_path,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)

    (repo / "tracked.txt").write_bytes(b"later head\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "advance ambient head")

    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source.export_exact_tree(snapshot, tmp_path / "strong-export")

    destination = tmp_path / "captured-export"
    source.export_captured_commit_tree(snapshot, destination)
    assert (destination / "tracked.txt").read_bytes() == b"committed\n"
    assert not (destination / ".git").exists()


def test_captured_commit_export_rejects_forged_snapshot_fields(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)
    forged_manifest = replace(
        snapshot.protected_manifest,
        digest="0" * 64,
    )
    forged = replace(snapshot, protected_manifest=forged_manifest)

    with pytest.raises(source.SourceBoundaryError, match="snapshot"):
        source.export_captured_commit_tree(forged, tmp_path / "forged-export")


def test_captured_commit_validation_and_materialization_have_distinct_budgets(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    snapshot = _snapshot(repo)

    class MaterializationReached(RuntimeError):
        pass

    observed: dict[str, float] = {}

    def slow_validation(_snapshot, _authority, *, deadline):
        assert deadline > time.monotonic()
        observed["validation_deadline"] = deadline
        time.sleep(0.35)
        return ()

    def require_fresh_materialization_budget(_repo, _destination, *, deadline):
        assert deadline > observed["validation_deadline"]
        raise MaterializationReached

    monkeypatch.setattr(source, "_DEFAULT_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(source, "_validate_captured_snapshot", slow_validation)
    monkeypatch.setattr(
        source, "_prepare_destination", require_fresh_materialization_budget,
    )

    with pytest.raises(MaterializationReached):
        source.export_captured_commit_tree(snapshot, tmp_path / "exported")


def test_trusted_git_invocation_disables_configured_fsmonitor_hook(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    marker = tmp_path / "fsmonitor-executed"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {str(marker)!r}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))

    snapshot = _snapshot(repo)
    destination = tmp_path / "exported"
    source.export_exact_tree(snapshot, destination)

    assert snapshot.head_oid
    assert (destination / "tracked.txt").read_bytes() == b"committed\n"
    assert not marker.exists()


def test_git_environment_removes_all_config_injection_variables(monkeypatch):
    source = _source()
    injected = {
        "GIT_CONFIG": "/tmp/hostile-config",
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=/tmp/hostile-hook'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "/tmp/hostile-hook",
        "GIT_NAMESPACE": "hostile",
        "GIT_SHALLOW_FILE": "/tmp/hostile-shallow",
        "GIT_EXEC_PATH": "/tmp/hostile-exec-path",
        "DEVELOPER_DIR": "/tmp/hostile-developer",
        "DYLD_INSERT_LIBRARIES": "/tmp/hostile.dylib",
        "LD_PRELOAD": "/tmp/hostile.so",
        "PYTHONPATH": "/tmp/hostile-python",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)

    trusted = source._git_environment()

    assert set(trusted) <= {
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "HOME",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
    }
    assert not any(
        key == "DEVELOPER_DIR"
        or key.startswith(("DYLD_", "GIT_", "LD_", "PYTHON"))
        and key not in {"GIT_NO_REPLACE_OBJECTS", "GIT_OPTIONAL_LOCKS"}
        for key in trusted
    )


def test_fixed_system_verifier_and_git_ignore_hostile_developer_dir(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    expected_head = _git(
        repo, "rev-parse", "--verify", "HEAD^{commit}",
    ).decode("ascii")
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "missing-developer-dir"))

    identity = source.resolve_repo_identity(str(repo))
    snapshot = source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )

    assert snapshot.head_oid == expected_head
    assert source._get_capture_authority().git_path == b"/usr/bin/git"


def test_non_darwin_strong_capture_fails_but_legacy_plan_is_candidate_only(
    tmp_path, monkeypatch,
):
    source = _source()
    from agent.bestplan_state import BestplanStore, compute_baseline_fingerprint

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("ignored-runtime/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore runtime")
    ignored = repo / "ignored-runtime"
    ignored.mkdir()
    (ignored / "cache.bin").write_bytes(b"ambient ignored bytes\n")
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    marker = tmp_path / "attacker-git-ran"
    attacker_git = attacker_bin / "git"
    attacker_git.write_text(
        f"#!/bin/sh\n: > {marker}\nexit 99\n", encoding="utf-8",
    )
    attacker_git.chmod(0o755)
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY_PRESEEDED", False)
    monkeypatch.setattr(source.sys, "platform", "linux")
    monkeypatch.setenv("PATH", str(attacker_bin))
    monkeypatch.setenv("SYSTEMROOT", r"C:\Users\attacker\root")
    monkeypatch.setenv("PROGRAMFILES", r"C:\Users\attacker\programs")

    with pytest.raises(source.SourceBoundaryError, match="unsupported"):
        source.resolve_repo_identity(str(repo))
    fingerprint = compute_baseline_fingerprint(str(repo))
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    plan_id = store.create_plan(
        "portable legacy plan",
        _plan(str(repo)),
        session_id="portable",
        workspace=str(repo),
    )

    record = store.get_plan(plan_id)
    assert fingerprint.startswith("legacy-v1:")
    assert record["baseline_fingerprint"] == fingerprint
    assert record["baseline_revision"] is None
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO/process proof required")
@pytest.mark.live_system_guard_bypass
def test_public_legacy_timeout_reaps_git_blocked_on_fifo_index(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    index_path = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
        .decode("utf-8")
    )
    index_path.rename(index_path.with_suffix(".saved"))
    os.mkfifo(index_path)
    script = """
import sys
import time
from agent import bestplan_source as source

try:
    source.capture_legacy_v1_fingerprint(sys.argv[1], time.monotonic() + 0.8)
except source.ProofStaleError as exc:
    print(exc.code)
else:
    raise AssertionError("FIFO-backed index did not expire")
"""
    client = subprocess.Popen(
        [sys.executable, "-c", script, str(repo)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    git_pid: int | None = None

    def process_snapshot() -> dict[int, tuple[int, str]]:
        output = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        snapshot: dict[int, tuple[int, str]] = {}
        for line in output.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3:
                snapshot[int(fields[0])] = (int(fields[1]), fields[2])
        return snapshot

    try:
        observation_deadline = time.monotonic() + 3.0
        while client.poll() is None and time.monotonic() < observation_deadline:
            snapshot = process_snapshot()
            descendants = {client.pid}
            changed = True
            while changed:
                changed = False
                for pid, (parent_pid, _command) in snapshot.items():
                    if parent_pid in descendants and pid not in descendants:
                        descendants.add(pid)
                        changed = True
            for pid in descendants:
                command = snapshot.get(pid, (0, ""))[1]
                if "/usr/bin/git --no-pager -c core.fsmonitor=false" in command:
                    git_pid = pid
                    break
            if git_pid is not None:
                break
            time.sleep(0.01)
        stdout, stderr = client.communicate(timeout=5.0)
        assert client.returncode == 0, stderr
        assert stdout.strip() == "proof_stale"
        assert git_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(git_pid, 0)
    finally:
        if client.poll() is None:
            client.kill()
            client.communicate(timeout=2.0)
        if git_pid is not None:
            try:
                os.kill(git_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass


def _write_filter_marker_script(path: Path, marker: Path, *, passthrough: bool) -> None:
    tail = "cat\n" if passthrough else "exit 1\n"
    path.write_text(
        f"#!/bin/sh\n: > {marker}\n" + tail,
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_public_legacy_fingerprint_never_executes_clean_filter(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    marker = tmp_path / "clean-filter-ran"
    filter_script = tmp_path / "clean-filter"
    _write_filter_marker_script(filter_script, marker, passthrough=True)
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "add filter attributes")
    _git(repo, "config", "filter.evil.clean", str(filter_script))
    (repo / "tracked.txt").write_bytes(b"modified worktree bytes\n")

    _workspace, fingerprint = source.capture_legacy_v1_fingerprint(
        str(repo), time.monotonic() + 5.0,
    )

    assert fingerprint.startswith("legacy-v1:")
    assert not marker.exists()


def test_public_legacy_fingerprint_ignores_all_conversion_drivers(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    markers = {
        name: tmp_path / f"{name}-ran"
        for name in ("clean", "smudge", "process", "textconv")
    }
    scripts = {name: tmp_path / name for name in markers}
    for name, script in scripts.items():
        _write_filter_marker_script(
            script, markers[name], passthrough=name != "process",
        )
    (repo / ".gitattributes").write_text(
        "*.txt filter=evil diff=evil\n", encoding="utf-8",
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "add conversion attributes")
    for name in ("clean", "smudge", "process"):
        _git(repo, "config", f"filter.evil.{name}", str(scripts[name]))
    _git(repo, "config", "diff.evil.textconv", str(scripts["textconv"]))
    (repo / "tracked.txt").write_bytes(b"modified worktree bytes\n")

    error = None
    try:
        _workspace, fingerprint = source.capture_legacy_v1_fingerprint(
            str(repo), time.monotonic() + 5.0,
        )
    except source.SourceBoundaryError as exc:
        error = exc
        fingerprint = ""

    assert error is None
    assert fingerprint.startswith("legacy-v1:")
    assert not {name for name, marker in markers.items() if marker.exists()}


def test_git_environment_uses_the_verified_platform_helper_path():
    source = _source()
    authority = replace(
        source._get_capture_authority(time.monotonic() + 3.0),
        helper_path="trusted-platform-path",
    )

    assert source._git_environment(authority)["PATH"] == "trusted-platform-path"


def test_legacy_git_roots_are_fixed_and_strong_capture_is_darwin_only():
    source = _source()

    assert source.strong_source_capture_supported(
        os_name="posix", platform="darwin",
    ) is True
    assert source.strong_source_capture_supported(
        os_name="posix", platform="linux",
    ) is False
    assert source.strong_source_capture_supported(
        os_name="nt", platform="win32",
    ) is False
    assert source._legacy_v1_git_path(
        os_name="posix", platform="linux",
    ) == "/usr/bin/git"
    assert source._legacy_v1_git_path(
        os_name="nt", platform="win32",
    ) == r"C:\Program Files\Git\cmd\git.exe"


@pytest.mark.skipif(os.name != "posix", reason="executable fixture is POSIX-only")
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_legacy_git_output_caps_stop_a_blocked_producer(
    tmp_path, monkeypatch, stream_name,
):
    source = _source()
    producer = tmp_path / "legacy-git-producer"
    producer.write_text(
        "#!/usr/bin/python3\n"
        "import sys, time\n"
        "stream = getattr(sys, sys.argv[-1]).buffer\n"
        "size = 4096 if sys.argv[-1] == 'stdout' else 2 * 1024 * 1024\n"
        "stream.write(b'x' * size)\n"
        "stream.flush()\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    monkeypatch.setattr(source, "_legacy_v1_git_path", lambda: str(producer))

    started = time.monotonic()
    with pytest.raises(source.UnsupportedRepositoryError, match="exceeds"):
        source._run_legacy_v1_git_output(
            os.fsencode(tmp_path),
            stream_name,
            deadline=time.monotonic() + 1.5,
            max_output_bytes=1024,
            owns_process_tree=True,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
@pytest.mark.live_system_guard_bypass
def test_legacy_git_timeout_reaps_a_signal_ignoring_descendant(
    tmp_path, monkeypatch,
):
    source = _source()
    producer = tmp_path / "legacy-git-descendant"
    leader_pid_file = tmp_path / "leader.pid"
    child_pid_file = tmp_path / "child.pid"
    producer.write_text(
        "#!/usr/bin/python3\n"
        "import os, signal, sys\n"
        "leader_path, child_path = sys.argv[-2:]\n"
        "with open(leader_path, 'w', encoding='ascii') as stream:\n"
        "    stream.write(str(os.getpid()))\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    with open(child_path, 'w', encoding='ascii') as stream:\n"
        "        stream.write(str(os.getpid()))\n"
        "    os.close(0); os.close(1); os.close(2)\n"
        "    while True: signal.pause()\n"
        "while True: signal.pause()\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    monkeypatch.setattr(source, "_legacy_v1_git_path", lambda: str(producer))

    try:
        with pytest.raises(source.ProofStaleError, match="proof_stale"):
            source._run_legacy_v1_git_output(
                os.fsencode(tmp_path),
                str(leader_pid_file),
                str(child_pid_file),
                deadline=time.monotonic() + 0.6,
                max_output_bytes=1024,
                owns_process_tree=True,
            )
        for pid_file in (leader_pid_file, child_pid_file):
            pid = int(pid_file.read_text(encoding="ascii"))
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for pid_file in (leader_pid_file, child_pid_file):
            if not pid_file.exists():
                continue
            pid = int(pid_file.read_text(encoding="ascii"))
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass


def test_bestplan_modules_import_without_path_git_uses_fixed_system_git(tmp_path):
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    environment = dict(os.environ)
    environment["PATH"] = str(empty_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import agent.bestplan_state; "
                "from agent import bestplan_source as source; "
                "print('imported'); "
                "identity = source.resolve_repo_identity('.'); "
                "print(source._get_capture_authority().git_path.decode())"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["imported", "/usr/bin/git"]


def test_missing_fixed_authority_runtime_fails_as_coded_source_error(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    previous = source._CAPTURE_AUTHORITY
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)
    monkeypatch.setattr(
        source,
        "_authority_verifier_argv",
        lambda: ["/definitely/missing/bestplan-authority-verifier"],
    )
    try:
        with pytest.raises(source.SourceBoundaryError) as raised:
            source.resolve_repo_identity(str(repo))
        assert raised.value.code == "source_unavailable"
    finally:
        source._CAPTURE_AUTHORITY = previous


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
@pytest.mark.live_system_guard_bypass
def test_first_authority_verifier_obeys_capture_deadline_and_reaps_descendant(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    leader_pid_file = tmp_path / "authority-leader.pid"
    child_pid_file = tmp_path / "authority-child.pid"
    blocker = """
import os
import signal
import sys

with open(sys.argv[1], "w", encoding="ascii") as stream:
    stream.write(str(os.getpid()))
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[2], "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    os.close(0)
    os.close(1)
    os.close(2)
    while True:
        signal.pause()
while True:
    signal.pause()
"""
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        blocker,
        str(leader_pid_file),
        str(child_pid_file),
    ]
    previous = source._CAPTURE_AUTHORITY
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)
    monkeypatch.setattr(
        source, "_authority_verifier_argv", lambda: command, raising=False,
    )

    try:
        with pytest.raises(source.ProofStaleError, match="proof_stale"):
            source.capture_source_snapshot(identity, time.monotonic() + 0.5)
        leader_pid = int(leader_pid_file.read_text(encoding="ascii"))
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        for pid in (leader_pid, child_pid):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        source._CAPTURE_AUTHORITY = previous
        for path in (leader_pid_file, child_pid_file):
            if not path.exists():
                continue
            pid = int(path.read_text(encoding="ascii"))
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass


def test_resolve_repo_identity_bounds_first_authority_verification(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    blocker = [sys.executable, "-I", "-S", "-c", "import time;time.sleep(30)"]
    previous = source._CAPTURE_AUTHORITY
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)
    monkeypatch.setattr(source, "_DEFAULT_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(
        source, "_authority_verifier_argv", lambda: blocker, raising=False,
    )

    started = time.monotonic()
    try:
        with pytest.raises(source.ProofStaleError, match="proof_stale"):
            source.resolve_repo_identity(str(repo))
        assert time.monotonic() - started < 2.0
    finally:
        source._CAPTURE_AUTHORITY = previous


def test_resolve_repo_identity_does_not_run_blocking_realpath_in_parent(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    source._get_capture_authority(time.monotonic() + 3.0)
    repo_raw = os.fsencode(repo)

    def blocked_parent_realpath(path, *args, **kwargs):
        if os.fsencode(path) == repo_raw:
            time.sleep(0.6)
        return path

    monkeypatch.setattr(source.os.path, "realpath", blocked_parent_realpath)
    monkeypatch.setattr(source, "_DEFAULT_DEADLINE_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source.resolve_repo_identity(str(repo))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5


def test_relative_repo_resolution_does_not_read_parent_cwd(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    source._get_capture_authority(time.monotonic() + 3.0)
    monkeypatch.chdir(repo)
    real_getcwd = source.os.getcwd
    calls = 0

    def blocked_parent_getcwd():
        nonlocal calls
        calls += 1
        time.sleep(1.2)
        return real_getcwd()

    monkeypatch.setattr(source.os, "getcwd", blocked_parent_getcwd)
    monkeypatch.setattr(source, "_DEFAULT_DEADLINE_SECONDS", 2.0)
    started = time.monotonic()
    identity = source.resolve_repo_identity(".")
    elapsed = time.monotonic() - started

    assert calls == 0
    assert elapsed < 1.0
    assert identity.worktree_raw == os.fsencode(repo)


def test_trusted_git_preexec_identity_check_never_blocks_in_parent(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    source.resolve_repo_identity(str(repo))

    def parent_hash_is_forbidden(*_args, **_kwargs):
        raise AssertionError("trusted executable was rehashed in the parent")

    monkeypatch.setattr(source, "_stable_file_identity", parent_hash_is_forbidden)
    identity = source.resolve_repo_identity(str(repo))
    assert identity.worktree_raw == os.fsencode(repo.resolve())


def test_capture_authority_lock_contention_obeys_deadline(monkeypatch):
    source = _source()
    previous = source._CAPTURE_AUTHORITY
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)
    assert source._AUTHORITY_LOCK.acquire(timeout=1.0)
    try:
        with pytest.raises(source.ProofStaleError, match="proof_stale"):
            source._get_capture_authority(time.monotonic() + 0.05)
    finally:
        source._AUTHORITY_LOCK.release()
        source._CAPTURE_AUTHORITY = previous


def test_capture_authority_binds_exact_module_interpreter_and_git_identity():
    source = _source()
    authority = source._get_capture_authority(time.monotonic() + 3.0)

    for path, digest, device, inode in (
        (
            authority.module_path,
            authority.module_sha256,
            authority.module_device,
            authority.module_inode,
        ),
        (
            authority.interpreter_path,
            authority.interpreter_sha256,
            authority.interpreter_device,
            authority.interpreter_inode,
        ),
        (
            authority.git_path,
            authority.git_sha256,
            authority.git_device,
            authority.git_inode,
        ),
    ):
        assert path == os.path.realpath(path)
        actual_digest, actual_device, actual_inode = source._stable_file_identity(path)
        assert (actual_digest, actual_device, actual_inode) == (
            digest,
            device,
            inode,
        )

    values = {
        "module_path": authority.module_path,
        "module_sha256": authority.module_sha256,
        "module_device": authority.module_device,
        "module_inode": authority.module_inode,
        "interpreter_path": authority.interpreter_path,
        "interpreter_sha256": authority.interpreter_sha256,
        "interpreter_device": authority.interpreter_device,
        "interpreter_inode": authority.interpreter_inode,
        "git_path": authority.git_path,
        "git_sha256": authority.git_sha256,
        "git_device": authority.git_device,
        "git_inode": authority.git_inode,
    }
    for field in values:
        changed = dict(values)
        original = changed[field]
        if isinstance(original, bytes):
            changed[field] = original + b"-changed"
        elif isinstance(original, str):
            changed[field] = ("0" if original[0] != "0" else "1") + original[1:]
        else:
            changed[field] = original + 1
        assert source._make_capture_authority(**changed).implementation_sha256 != (
            authority.implementation_sha256
        )


@pytest.mark.parametrize("bad_value", [True, "1", 1.5])
def test_authority_response_identity_types_are_strict(bad_value):
    source = _source()
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    payload = source._authority_identity_payload(authority)
    payload["git"]["device"] = bad_value

    with pytest.raises(source.SourceBoundaryError, match="invalid metadata"):
        source._parse_authority_identity(payload)


@pytest.mark.parametrize("response", [b"{", b"x" * (64 * 1024 + 1)])
def test_authority_verifier_rejects_malformed_and_oversize_response(
    tmp_path, monkeypatch, response,
):
    source = _source()
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    response_path = tmp_path / "authority-response"
    response_path.write_bytes(response)
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
        str(response_path),
    ]
    monkeypatch.setattr(source, "_authority_verifier_argv", lambda: command)

    with pytest.raises(source.SourceBoundaryError, match="invalid|limit|exceeds"):
        source._run_authority_verifier(
            deadline=time.monotonic() + 3.0,
            expected=authority,
        )


def test_unsupported_authority_host_fails_before_spawn_but_preseed_is_usable(
    monkeypatch,
):
    source = _source()
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    monkeypatch.setattr(source.sys, "platform", "unsupported-test-host")
    monkeypatch.setattr(source, "_CAPTURE_AUTHORITY", None)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("unsupported authority host attempted to spawn")

    monkeypatch.setattr(source.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(source.SourceBoundaryError, match="unsupported"):
        source._get_capture_authority(time.monotonic() + 1.0)

    source._CAPTURE_AUTHORITY = authority
    assert source._get_capture_authority(time.monotonic() + 1.0) == authority


def test_trusted_executable_identity_maps_unavailable_binary_to_boundary_error(
    monkeypatch,
):
    source = _source()

    def unavailable(_path, *, require_executable):
        raise OSError("synthetic executable disappearance")

    monkeypatch.setattr(source, "_stable_file_identity", unavailable)
    with pytest.raises(source.SourceBoundaryError, match="executable"):
        source._assert_trusted_file_identity(
            source._CAPTURE_GIT_PATH,
            source._CAPTURE_GIT_SHA256,
            source._CAPTURE_GIT_DEVICE,
            source._CAPTURE_GIT_INODE,
            require_executable=True,
        )


def test_capture_bootstrap_verifies_module_bytes_before_execution(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    marker = tmp_path / "replacement-executed"
    replacement = tmp_path / "bestplan_source.py"
    replacement.write_text(
        "import sys\n"
        f"open({str(marker)!r}, 'wb').close()\n"
        "class SourceBoundaryError(ValueError):\n"
        "    code = 'source_unavailable'\n"
        "class RepoIdentity:\n"
        "    pass\n"
        "_CAPTURE_IMPLEMENTATION_SHA256 = sys.argv[2]\n"
        "def _capture_source_snapshot_in_process(repo, deadline):\n"
        "    raise SourceBoundaryError('replacement accepted')\n",
        encoding="utf-8",
    )
    interpreter = os.fsdecode(source._CAPTURE_INTERPRETER_PATH)
    interpreter_stat = os.stat(source._CAPTURE_INTERPRETER_PATH)
    command = [
        interpreter,
        "-I",
        "-S",
        "-c",
        source._CAPTURE_HELPER_BOOTSTRAP,
        str(replacement),
        source._CAPTURE_IMPLEMENTATION_SHA256,
        source._CAPTURE_MODULE_SHA256,
        str(source._CAPTURE_MODULE_DEVICE),
        str(source._CAPTURE_MODULE_INODE),
        interpreter,
        str(interpreter_stat.st_dev),
        str(interpreter_stat.st_ino),
        source._CAPTURE_INTERPRETER_SHA256,
        os.fsdecode(source._CAPTURE_GIT_PATH),
        str(source._CAPTURE_GIT_DEVICE),
        str(source._CAPTURE_GIT_INODE),
        source._CAPTURE_GIT_SHA256,
    ]
    result = subprocess.run(
        command,
        input=pickle.dumps(
            (source._CAPTURE_IMPLEMENTATION_SHA256, identity, 1.0),
            protocol=5,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3.0,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_capture_bootstrap_rejects_executable_content_mismatch_before_module(
    tmp_path,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    marker = tmp_path / "mismatched-executable-module-ran"
    replacement = tmp_path / "bestplan_source.py"
    replacement.write_text(
        "import sys\n"
        f"open({str(marker)!r}, 'wb').close()\n"
        "class SourceBoundaryError(ValueError):\n"
        "    code = 'source_unavailable'\n"
        "class RepoIdentity:\n"
        "    pass\n"
        "_CAPTURE_IMPLEMENTATION_SHA256 = sys.argv[2]\n",
        encoding="utf-8",
    )
    interpreter = os.fsdecode(source._CAPTURE_INTERPRETER_PATH)
    interpreter_stat = os.stat(source._CAPTURE_INTERPRETER_PATH)
    git_path = Path(shutil.which("git") or "").resolve()
    git_stat = git_path.stat()
    command = [
        interpreter,
        "-I",
        "-S",
        "-c",
        source._CAPTURE_HELPER_BOOTSTRAP,
        str(replacement),
        source._CAPTURE_IMPLEMENTATION_SHA256,
        hashlib.sha256(replacement.read_bytes()).hexdigest(),
        str(replacement.stat().st_dev),
        str(replacement.stat().st_ino),
        interpreter,
        str(interpreter_stat.st_dev),
        str(interpreter_stat.st_ino),
        "0" * 64,
        str(git_path),
        str(git_stat.st_dev),
        str(git_stat.st_ino),
        hashlib.sha256(git_path.read_bytes()).hexdigest(),
    ]
    result = subprocess.run(
        command,
        input=pickle.dumps(
            (source._CAPTURE_IMPLEMENTATION_SHA256, identity, 1.0),
            protocol=5,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3.0,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


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


def test_create_plan_does_not_resolve_workspace_in_the_parent(
    tmp_path, monkeypatch,
):
    source = _source()
    from agent import bestplan_state

    repo = _init_repo(tmp_path / "repo")
    store = bestplan_state.BestplanStore(
        db_path=tmp_path / "state" / "state.db",
    )
    captured_snapshot = _snapshot(repo)
    captured_repo = captured_snapshot.repo
    real_resolve = bestplan_state.Path.resolve

    def blocked_parent_resolve(path, *args, **kwargs):
        time.sleep(0.6)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(bestplan_state.Path, "resolve", blocked_parent_resolve)
    monkeypatch.setattr(
        bestplan_state, "resolve_repo_identity", lambda _workspace: captured_repo,
    )
    monkeypatch.setattr(
        bestplan_state,
        "capture_source_snapshot",
        lambda _repo, _deadline: captured_snapshot,
    )

    started = time.monotonic()
    plan_id = store.create_plan(
        "bounded",
        _plan(str(repo)),
        session_id="bounded",
        workspace=str(repo),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert store.get_plan(plan_id)["baseline_revision"] == captured_snapshot.head_oid


def test_create_plan_relative_workspace_does_not_read_parent_cwd(
    tmp_path, monkeypatch,
):
    source = _source()
    from agent import bestplan_state

    repo = _init_repo(tmp_path / "repo")
    store = bestplan_state.BestplanStore(
        db_path=tmp_path / "state" / "state.db",
    )
    captured_snapshot = _snapshot(repo)
    captured_repo = captured_snapshot.repo
    calls = 0

    def blocked_parent_getcwd():
        nonlocal calls
        calls += 1
        time.sleep(1.2)
        return str(repo)

    monkeypatch.setattr(bestplan_state.os, "getcwd", blocked_parent_getcwd)
    monkeypatch.setattr(
        bestplan_state, "resolve_repo_identity", lambda _workspace: captured_repo,
    )
    monkeypatch.setattr(
        bestplan_state,
        "capture_source_snapshot",
        lambda _repo, _deadline: captured_snapshot,
    )

    started = time.monotonic()
    plan_id = store.create_plan(
        "bounded relative",
        _plan(str(repo)),
        session_id="bounded-relative",
        workspace=".",
    )
    elapsed = time.monotonic() - started

    assert calls == 0
    assert elapsed < 0.5
    assert store.get_plan(plan_id)["workspace"] == str(repo)


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


def test_legacy_create_plan_canonicalizes_only_in_the_bounded_helper(
    tmp_path, monkeypatch,
):
    from agent import bestplan_state

    workspace = "/tmp/hermes-bestplan-legacy-canonical-fixture"
    expected = os.path.realpath(workspace)
    store = bestplan_state.BestplanStore(
        db_path=tmp_path / "state" / "state.db",
    )

    def forbidden_parent_resolve(*_args, **_kwargs):
        raise AssertionError("legacy workspace resolved in the parent process")

    def forbidden_parent_boundary(*_args, **_kwargs):
        raise AssertionError("legacy Git boundary inspected in the parent process")

    monkeypatch.setattr(bestplan_state.Path, "resolve", forbidden_parent_resolve)
    monkeypatch.setattr(
        bestplan_state,
        "_has_local_git_boundary",
        forbidden_parent_boundary,
        raising=False,
    )

    plan_id = store.create_plan(
        "legacy bounded",
        _plan(workspace),
        session_id="legacy-bounded",
        workspace=workspace,
        baseline_fingerprint="synthetic-test-baseline",
    )

    record = store.get_plan(plan_id)
    assert record["workspace"] == expected
    assert record["baseline_revision"] is None


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


def test_manifest_fails_closed_when_raw_path_exceeds_bound(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "long-untracked-name.txt").write_bytes(b"ambient\n")
    monkeypatch.setattr(source, "_MAX_PATH_BYTES", 8, raising=False)

    with pytest.raises(source.UnsupportedRepositoryError, match="path|metadata|limit"):
        source.capture_protected_manifest(source.resolve_repo_identity(str(repo)))


def test_same_size_same_mtime_tracked_mutation_is_never_missed(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "update-index", "--refresh")
    identity = source.resolve_repo_identity(str(repo))
    before = source.capture_protected_manifest(identity)
    tracked = repo / "tracked.txt"
    original = tracked.stat()
    assert len(b"committed\n") == len(b"tampered!\n")
    tracked.write_bytes(b"tampered!\n")
    os.utime(tracked, ns=(original.st_atime_ns, original.st_mtime_ns))

    after = source.capture_protected_manifest(identity)

    assert after.digest != before.digest
    assert b"tracked.txt" in after.protected_paths


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


def test_clean_fsmonitor_valid_entry_is_manifested_but_not_protected(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.fsmonitor", "true")
    _git(repo, "update-index", "--fsmonitor-valid", "tracked.txt")

    manifest = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )
    flag = {entry.path: entry for entry in manifest.index_flags}[b"tracked.txt"]
    assert flag.fsmonitor_valid is True
    assert b"tracked.txt" not in manifest.protected_paths


def test_fsmonitor_valid_decoder_supports_index_v4(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked-two.txt").write_bytes(b"two\n")
    _git(repo, "add", "tracked-two.txt")
    _git(repo, "commit", "-qm", "second path")
    _git(repo, "update-index", "--index-version=4")
    _git(repo, "config", "core.fsmonitor", "true")
    _git(repo, "update-index", "--fsmonitor-valid", "tracked-two.txt")

    manifest = source.capture_protected_manifest(
        source.resolve_repo_identity(str(repo))
    )

    flags = {entry.path: entry for entry in manifest.index_flags}
    assert flags[b"tracked-two.txt"].fsmonitor_valid is True


def test_fsmonitor_decoder_rejects_unsupported_extension_version(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.fsmonitor", "true")
    _git(repo, "update-index", "--fsmonitor-valid", "tracked.txt")
    value = bytearray(_fsmonitor_index_bytes(repo))
    start, _end = _fsmn_payload(value)
    value[start : start + 4] = (3).to_bytes(4, "big")

    with pytest.raises(source.SourceBoundaryError, match="fsmonitor.*version"):
        source._parse_index_fsmonitor_valid_paths(
            _rewrite_sha1_index_checksum(value),
            index_paths=(b"tracked.txt",),
            object_format="sha1",
            deadline=time.monotonic() + 2.0,
        )


def test_fsmonitor_decoder_rejects_malformed_bitmap_size(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.fsmonitor", "true")
    _git(repo, "update-index", "--fsmonitor-valid", "tracked.txt")
    value = bytearray(_fsmonitor_index_bytes(repo))
    ewah_offset = _fsmn_ewah_offset(value)
    size_offset = ewah_offset - 4
    declared = int.from_bytes(value[size_offset:ewah_offset], "big")
    value[size_offset:ewah_offset] = (declared + 1).to_bytes(4, "big")

    with pytest.raises(source.SourceBoundaryError, match="fsmonitor.*size"):
        source._parse_index_fsmonitor_valid_paths(
            _rewrite_sha1_index_checksum(value),
            index_paths=(b"tracked.txt",),
            object_format="sha1",
            deadline=time.monotonic() + 2.0,
        )


def test_fsmonitor_decoder_rejects_bitmap_larger_than_index(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.fsmonitor", "true")
    _git(repo, "update-index", "--fsmonitor-valid", "tracked.txt")
    value = bytearray(_fsmonitor_index_bytes(repo))
    ewah_offset = _fsmn_ewah_offset(value)
    value[ewah_offset : ewah_offset + 4] = (2).to_bytes(4, "big")

    with pytest.raises(source.SourceBoundaryError, match="fsmonitor.*entries"):
        source._parse_index_fsmonitor_valid_paths(
            _rewrite_sha1_index_checksum(value),
            index_paths=(b"tracked.txt",),
            object_format="sha1",
            deadline=time.monotonic() + 2.0,
        )


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
    snapshot = source._capture_source_snapshot_in_process(
        identity, time.monotonic() + 1.0,
    )
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
        source._capture_source_snapshot_in_process(
            identity, time.monotonic() + 1.0,
        )
    assert raised.value.code == "proof_stale"
    assert 2 <= calls <= 16


def test_same_count_index_path_churn_retries_to_proof_stale(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    real_read = source._read_stable_small_file
    current = "tracked.txt"

    def churn_after_raw_index_read(path, **kwargs):
        nonlocal current
        value = real_read(path, **kwargs)
        replacement = "replacement.txt" if current == "tracked.txt" else "tracked.txt"
        _git(repo, "mv", current, replacement)
        current = replacement
        return value

    monkeypatch.setattr(source, "_MAX_STABILIZATION_READS", 4)
    monkeypatch.setattr(source, "_read_stable_small_file", churn_after_raw_index_read)

    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source._capture_source_snapshot_in_process(
            identity, time.monotonic() + 10.0,
        )


def test_index_count_churn_is_retryable_capture_change(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    real_read = source._read_stable_small_file
    changed = False

    def add_after_raw_index_read(path, **kwargs):
        nonlocal changed
        value = real_read(path, **kwargs)
        if not changed:
            (repo / "added.txt").write_bytes(b"added\n")
            _git(repo, "add", "added.txt")
            changed = True
        return value

    monkeypatch.setattr(source, "_read_stable_small_file", add_after_raw_index_read)

    with pytest.raises(source._CaptureChanged, match="index"):
        source.capture_protected_manifest(identity, deadline=time.monotonic() + 5.0)


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
        source._capture_source_snapshot_in_process(
            identity, time.monotonic() + 10.0,
        )
    assert raised.value.code == "proof_stale"

    calls = 0
    store = BestplanStore(db_path=tmp_path / "state" / "state.db")
    from agent import bestplan_state

    monkeypatch.setattr(
        bestplan_state,
        "capture_source_snapshot",
        source._capture_source_snapshot_in_process,
    )
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


def test_effective_worktree_and_global_partial_clone_config_fail_closed(
    tmp_path, monkeypatch,
):
    source = _source()

    worktree_config = _init_repo(tmp_path / "worktree-config")
    _git(worktree_config, "config", "extensions.worktreeConfig", "true")
    _git(
        worktree_config,
        "config",
        "--worktree",
        "extensions.partialClone",
        "origin",
    )
    with pytest.raises(source.UnsupportedRepositoryError, match="partial|promisor"):
        source.assert_supported_repository(
            source.resolve_repo_identity(str(worktree_config))
        )

    global_config = _init_repo(tmp_path / "global-config")
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    (isolated_home / ".gitconfig").write_text(
        '[remote "origin"]\n\tpromisor = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(isolated_home))
    with pytest.raises(source.UnsupportedRepositoryError, match="partial|promisor"):
        source.assert_supported_repository(
            source.resolve_repo_identity(str(global_config))
        )


def test_persisted_sparse_index_fails_closed_after_config_is_cleared(tmp_path):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    (repo / "keep").mkdir()
    (repo / "omit").mkdir()
    (repo / "keep" / "kept.txt").write_bytes(b"kept\n")
    (repo / "omit" / "omitted.txt").write_bytes(b"omitted\n")
    _git(repo, "add", "keep/kept.txt", "omit/omitted.txt")
    _git(repo, "commit", "-qm", "directories")
    _git(repo, "sparse-checkout", "init", "--cone", "--sparse-index")
    _git(repo, "sparse-checkout", "set", "keep")
    for key in ("core.sparseCheckout", "core.sparseCheckoutCone", "index.sparse"):
        _git(repo, "config", "--worktree", key, "false")
        _git(repo, "config", "--local", key, "false")
        assert _git(repo, "config", "--bool", "--get", key) == b"false"

    index_path = Path(
        _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
        ).decode("utf-8")
    )
    assert b"sdir" in index_path.read_bytes()
    with pytest.raises(source.UnsupportedRepositoryError, match="sparse"):
        source.assert_supported_repository(source.resolve_repo_identity(str(repo)))


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


def test_recapture_brackets_identity_resolution_with_authority_verification(
    tmp_path, monkeypatch,
):
    source = _source()
    snapshot = _snapshot(_init_repo(tmp_path / "repo"))
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    events: list[str] = []

    def verify_before(*, deadline):
        assert deadline > time.monotonic()
        events.append("verify-before")
        return authority

    def resolve_in_helper(_authority, operation, request_value, deadline):
        assert operation == "resolve"
        assert request_value == snapshot.repo.workspace
        assert deadline > time.monotonic()
        events.append("resolve-helper")
        return snapshot.repo

    def forbidden_parent_resolve(*_args, **_kwargs):
        raise AssertionError("recapture resolved repository identity in the parent")

    def capture(_repo, _deadline):
        events.append("capture")
        return snapshot

    def verify_after(_authority, *, deadline):
        assert deadline > time.monotonic()
        events.append("verify-after")

    monkeypatch.setattr(source, "_verify_public_authority", verify_before)
    monkeypatch.setattr(source, "_run_source_helper", resolve_in_helper)
    monkeypatch.setattr(source, "_resolve_repo_identity", forbidden_parent_resolve)
    monkeypatch.setattr(source, "capture_source_snapshot", capture)
    monkeypatch.setattr(source, "_verify_public_authority_after", verify_after)

    assert source.recapture_matches(snapshot) is True
    assert events == [
        "verify-before", "resolve-helper", "capture", "verify-after",
    ]


def test_public_capture_verifies_authority_after_result_validation(
    tmp_path, monkeypatch,
):
    source = _source()
    snapshot = _snapshot(_init_repo(tmp_path / "repo"))
    authority = source._get_capture_authority(time.monotonic() + 3.0)
    events: list[str] = []

    def verify_before(*, deadline):
        events.append("verify-before")
        return authority

    def run_helper(_authority, operation, request_value, deadline):
        assert operation == "capture"
        assert request_value == snapshot.repo
        assert deadline > time.monotonic()
        events.append("helper")
        return snapshot

    def verify_after(_authority, *, deadline):
        assert deadline > time.monotonic()
        events.append("verify-after")

    monkeypatch.setattr(source, "_verify_public_authority", verify_before)
    monkeypatch.setattr(source, "_run_source_helper", run_helper)
    monkeypatch.setattr(source, "_verify_public_authority_after", verify_after)

    assert source.capture_source_snapshot(
        snapshot.repo, time.monotonic() + 3.0,
    ) == snapshot
    assert events == ["verify-before", "helper", "verify-after"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_public_capture_kills_and_reaps_blocked_spawn_helper(tmp_path, monkeypatch):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    fifo = tmp_path / "blocking-filesystem-operation"
    pid_file = tmp_path / "helper.pid"
    os.mkfifo(fifo)

    assert hasattr(source, "_capture_helper_argv"), (
        "public capture must use a pinned spawned helper"
    )
    blocker = (
        "import os,sys;"
        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);"
        "os.write(fd,str(os.getpid()).encode());os.close(fd);"
        "os.open(sys.argv[2],os.O_RDONLY)"
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        blocker,
        str(pid_file),
        str(fifo),
    ]
    processes = []
    real_popen = source.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(source, "_capture_helper_argv", lambda _authority: command)
    monkeypatch.setattr(source.subprocess, "Popen", recording_popen)
    with pytest.raises(source.ProofStaleError) as raised:
        source.capture_source_snapshot(identity, time.monotonic() + 0.5)
    assert raised.value.code == "proof_stale"
    capture_processes = [process for process in processes if process.args == command]
    assert len(capture_processes) == 1
    assert capture_processes[0].poll() is not None
    helper_pid = int(pid_file.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(helper_pid, 0)


def test_public_capture_fails_closed_if_helper_containment_cannot_attach(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    assert hasattr(source, "_attach_capture_helper_containment")
    command = [sys.executable, "-I", "-S", "-c", "import time;time.sleep(30)"]
    processes = []
    real_popen = source.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    real_attach = source._attach_capture_helper_containment

    def unavailable(process):
        if process.args == command:
            raise OSError("synthetic containment unavailable")
        return real_attach(process)

    monkeypatch.setattr(source, "_capture_helper_argv", lambda _authority: command)
    monkeypatch.setattr(source, "_attach_capture_helper_containment", unavailable)
    monkeypatch.setattr(source.subprocess, "Popen", recording_popen)
    with pytest.raises(source.SourceBoundaryError, match="containment"):
        source.capture_source_snapshot(identity, time.monotonic() + 2.0)
    capture_processes = [process for process in processes if process.args == command]
    assert len(capture_processes) == 1
    assert capture_processes[0].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_public_capture_fails_closed_if_process_group_cleanup_is_denied(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))

    def denied(_process_group, _signal):
        raise PermissionError("synthetic killpg denial")

    monkeypatch.setattr(source.os, "killpg", denied)
    with pytest.raises(source.ProofStaleError, match="process group"):
        source.capture_source_snapshot(identity, time.monotonic() + 5.0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_capture_rejects_a_helper_outside_its_owned_session_group(monkeypatch):
    source = _source()

    class Process:
        pid = 12345

    monkeypatch.setattr(source.os, "getpgid", lambda _pid: Process.pid + 1)
    with pytest.raises(source.SourceBoundaryError, match="process group"):
        source._capture_posix_process_group(Process())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_public_capture_fails_closed_if_group_extinction_cannot_be_proven(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    real_killpg = source.os.killpg

    def persistent_group(process_group, sig):
        if sig == 0:
            return None
        return real_killpg(process_group, sig)

    monkeypatch.setattr(source, "_CAPTURE_CLEANUP_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(source.os, "killpg", persistent_group)
    with pytest.raises(source.ProofStaleError, match="extinction|process group"):
        source.capture_source_snapshot(identity, time.monotonic() + 5.0)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="POSIX process-group semantics required",
)
@pytest.mark.live_system_guard_bypass
def test_public_capture_kills_descendant_after_helper_leader_exits(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    child_pid_file = tmp_path / "descendant.pid"
    blocker = """
import os
import signal
import sys

child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    os.close(0)
    os.close(1)
    os.close(2)
    while True:
        signal.pause()
signal.pause()
"""
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        blocker,
        str(child_pid_file),
    ]
    monkeypatch.setattr(source, "_capture_helper_argv", lambda _authority: command)

    with pytest.raises(source.ProofStaleError, match="proof_stale"):
        source.capture_source_snapshot(identity, time.monotonic() + 0.5)
    child_pid = int(child_pid_file.read_text(encoding="ascii"))

    def descendant_running() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="ascii").split()
            return len(fields) < 3 or fields[2] != "Z"
        return True

    try:
        assert not descendant_running()
    finally:
        if descendant_running():
            os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))

@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="POSIX process-group semantics required",
)
@pytest.mark.live_system_guard_bypass
def test_public_capture_sweeps_descendant_after_valid_helper_error(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    child_pid_file = tmp_path / "error-descendant.pid"
    blocker = """
import os
import pickle
import signal
import sys

child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    os.close(0)
    os.close(1)
    os.close(2)
    while True:
        signal.pause()
payload = ("error", sys.argv[2], "ProofStaleError", "proof_stale", "synthetic")
sys.stdout.buffer.write(pickle.dumps(payload, protocol=5))
sys.stdout.buffer.flush()
"""
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        blocker,
        str(child_pid_file),
        source._CAPTURE_IMPLEMENTATION_SHA256,
    ]
    monkeypatch.setattr(source, "_capture_helper_argv", lambda _authority: command)

    with pytest.raises(source.ProofStaleError, match="synthetic"):
        source.capture_source_snapshot(identity, time.monotonic() + 2.0)
    child_pid = int(child_pid_file.read_text(encoding="ascii"))

    def descendant_running() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="ascii").split()
            return len(fields) < 3 or fields[2] != "Z"
        return True

    try:
        assert not descendant_running()
    finally:
        if descendant_running():
            os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="POSIX process-group semantics required",
)
@pytest.mark.live_system_guard_bypass
def test_public_capture_sweeps_descendant_before_success_return(
    tmp_path, monkeypatch,
):
    source = _source()
    repo = _init_repo(tmp_path / "repo")
    identity = source.resolve_repo_identity(str(repo))
    expected = source.capture_source_snapshot(
        identity,
        time.monotonic() + source.DEFAULT_SOURCE_OPERATION_SECONDS,
    )
    response_file = tmp_path / "success-response.pickle"
    response_file.write_bytes(
        pickle.dumps(
            ("ok", source._CAPTURE_IMPLEMENTATION_SHA256, expected),
            protocol=5,
        )
    )
    child_pid_file = tmp_path / "success-descendant.pid"
    blocker = """
import os
import signal
import sys

child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    os.close(0)
    os.close(1)
    os.close(2)
    while True:
        signal.pause()
with open(sys.argv[2], "rb") as stream:
    sys.stdout.buffer.write(stream.read())
sys.stdout.buffer.flush()
"""
    command = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        blocker,
        str(child_pid_file),
        str(response_file),
    ]
    monkeypatch.setattr(source, "_capture_helper_argv", lambda _authority: command)

    actual = source.capture_source_snapshot(identity, time.monotonic() + 2.0)
    child_pid = int(child_pid_file.read_text(encoding="ascii"))

    def descendant_running() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        assert actual == expected
        assert not descendant_running()
    finally:
        if descendant_running():
            os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
